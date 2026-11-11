"""The model's reply (S6): one JSON object, or the incident falls back to rules.

`tool` is any string and `args` any dict on purpose. A disallowed tool or bad arguments must
reach P1/P2 and be recorded as a refusal; failing here would hide the unsafe proposal.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from pricing_watch.agent.model import ModelError
from pricing_watch.policy.schemas import Severity


class ProposalOut(BaseModel):
    tool: str
    args: dict[str, Any]
    reason: str = ""  # no cap: a long reason must not hide the proposal from policy


class MessageOut(BaseModel):
    severity: Severity
    text: str = Field(min_length=1, max_length=2000)


class ModelOutput(BaseModel):
    diagnosis: str = Field(max_length=600)
    confidence: Literal["low", "medium", "high"]
    proposals: list[ProposalOut]  # no cap either; MAX_TOKENS bounds the reply
    message: MessageOut


class InvalidOutput(ModelError):
    pass


def parse_output(raw_text: str) -> ModelOutput:
    text = raw_text.strip()
    if text.startswith("```"):  # tolerate one Markdown code fence around the object
        text = text.split("\n", 1)[-1].removesuffix("```").strip()
    try:
        return ModelOutput.model_validate_json(text)
    except ValidationError as e:
        raise InvalidOutput(str(e)[:500]) from e
