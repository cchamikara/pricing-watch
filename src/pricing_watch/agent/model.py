"""Model provider seam (S6). The incident loop sees only `ModelProvider`; any failure is a typed
error, so the loop can fall back to the rules plan instead of crashing.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

import anthropic
from anthropic.types.beta import BetaMessageParam

CLAUDE_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ModelResponse:
    raw_text: str
    usage: dict[str, int]  # input_tokens, output_tokens
    model_name: str  # the model that actually answered


class ModelError(Exception):
    """The provider failed or declined; the caller falls back to rules."""


class ModelTimeout(ModelError):
    pass


class ModelProvider(Protocol):
    def complete(self, messages: Sequence[Message], timeout: float) -> ModelResponse: ...


class ClaudeProvider:
    def __init__(
        self, model: str = CLAUDE_MODEL, api_key: str | None = None, base_url: str | None = None
    ) -> None:
        # no SDK retries: `timeout` bounds the whole call, and a failure falls back to rules
        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url, max_retries=0)
        self._model = model

    def complete(self, messages: Sequence[Message], timeout: float) -> ModelResponse:
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
