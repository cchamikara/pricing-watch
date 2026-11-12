"""Model provider seam (S6). The incident loop sees only `ModelProvider`; any failure is a typed
error, so the loop can fall back to the rules plan instead of crashing.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, TypedDict

import anthropic
import httpx2  # google-genai's HTTP client; its errors are the transport failures
from anthropic.types.beta import BetaMessageParam
from google import genai
from google.genai import errors, types

from pricing_watch.agent.tools import ProductRead, StockRead

CLAUDE_MODEL = "claude-opus-5-5"
GEMINI_MODEL = "gemini-3.5-flash"  # optional provider; 3.8-flash was overloaded on the free tier
MAX_TOKENS = 16000


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ModelResponse:
    raw_text: str
    usage: dict[str, int]  # input_tokens, output_tokens
    model_name: str  # the model that actually answered


@dataclass(frozen=True)
class Brief:
    """Server-side incident facts. Real models read the prompt; the stand-in reads this."""

    kind: str
    category: str
    sku: str | None  # None: a category-wide incident
    now: datetime
    product: ProductRead | None = None
    stock: StockRead | None = None
    forecast_daily: float = 0.0
    suspect: bool = False  # the incident's SKU has suspect data (P9 refuses acting on it)
    notes: tuple[str, ...] = ()  # competitor listing notes, as text
    notices: tuple[str, ...] = ()  # supplier notices, as text


class ModelError(Exception):
    """The provider failed or declined; the caller falls back to rules."""


class ModelTimeout(ModelError):
    pass


class ModelProvider(Protocol):
    def complete(
        self, messages: Sequence[Message], timeout: float, brief: Brief | None = None
    ) -> ModelResponse: ...


class ClaudeProvider:
    def __init__(
        self, model: str = CLAUDE_MODEL, api_key: str | None = None, base_url: str | None = None
    ) -> None:
        # no SDK retries: `timeout` bounds the whole call, and a failure falls back to rules
        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url, max_retries=0)
        self._model = model

    def complete(
        self, messages: Sequence[Message], timeout: float, brief: Brief | None = None
    ) -> ModelResponse:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns: list[BetaMessageParam] = [
            {"role": "user", "content": m["content"]} if m["role"] == "user"
            else {"role": "assistant", "content": m["content"]}
            for m in messages if m["role"] != "system"
        ]  # fmt: skip
        try:
            response = self._client.beta.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=turns,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # a safety decline reruns on a fallback model
                timeout=timeout,
            )
        except anthropic.APITimeoutError as e:
            raise ModelTimeout(f"no answer within {timeout}s") from e
        except anthropic.AnthropicError as e:
            raise ModelError(str(e)) from e
        except TypeError as e:  # the SDK's signal that no API key or auth token was found
            raise ModelError(f"no credentials: {e}") from e
        if response.stop_reason == "refusal":
            raise ModelError(f"refused: {response.stop_details}")
        text = "".join(b.text for b in response.content if b.type == "text")
        usage = {"input_tokens": response.usage.input_tokens,
                 "output_tokens": response.usage.output_tokens}  # fmt: skip
        return ModelResponse(raw_text=text, usage=usage, model_name=response.model)


class GeminiProvider:
    def __init__(
        self, model: str = GEMINI_MODEL, api_key: str | None = None, base_url: str | None = None
    ) -> None:
        self._model = model
        # attempts=1: no SDK retries, so `timeout` bounds the whole call (as for Claude)
        self._options = types.HttpOptions(
            base_url=base_url, retry_options=types.HttpRetryOptions(attempts=1)
        )
        try:  # reads GEMINI_API_KEY or GOOGLE_API_KEY when api_key is None
            self._client: genai.Client | None = genai.Client(
                api_key=api_key, http_options=self._options
            )
            self._no_client = ""
        except ValueError as e:  # no key: fail on the call, so the loop falls back to rules
            self._client, self._no_client = None, str(e)

    def complete(
        self, messages: Sequence[Message], timeout: float, brief: Brief | None = None
    ) -> ModelResponse:
        if self._client is None:
            raise ModelError(f"no credentials: {self._no_client}")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [
            types.Content(role="model" if m["role"] == "assistant" else "user",
                          parts=[types.Part(text=m["content"])])
            for m in messages if m["role"] != "system"
        ]  # fmt: skip
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=MAX_TOKENS,
            http_options=self._options.model_copy(update={"timeout": int(timeout * 1000)}),  # ms
        )
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except httpx2.TimeoutException as e:
            raise ModelTimeout(f"no answer within {timeout}s") from e
        except (errors.APIError, httpx2.HTTPError) as e:
            raise ModelError(str(e)) from e
        if not response.text:  # blocked by safety settings, or no text part
            reason = response.candidates[0].finish_reason if response.candidates else None
            raise ModelError(f"no text in the reply (finish reason {reason})")
        usage = response.usage_metadata
        return ModelResponse(
            raw_text=response.text,
            usage={"input_tokens": (usage and usage.prompt_token_count) or 0,
                   "output_tokens": (usage and usage.candidates_token_count) or 0},
            model_name=response.model_version or self._model,
        )  # fmt: skip
