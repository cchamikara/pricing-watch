"""Evaluators read traces back from Langfuse and score them. Each returns a Score, or a Skip
when the trace isn't one it applies to; a Skip is counted, never silent."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from pricing_watch.evals.loader import TraceView

Level = Literal["trace", "session", "run"]


@dataclass(frozen=True)
class Score:
    value: float  # 0 to 1; booleans are 0 or 1
    comment: str = ""


@dataclass(frozen=True)
class Skip:
    reason: str


class Evaluator(Protocol):
    name: str
    level: Level
    version: int  # bump when its logic changes: part of the score's stable id

    def evaluate(self, view: "TraceView") -> Score | Skip: ...


@dataclass(frozen=True)
class Check:
    """An evaluator made from a plain function of the trace."""

    name: str
    run: Callable[["TraceView"], Score | Skip]
    level: Level = "trace"
    version: int = 1

    def evaluate(self, view: "TraceView") -> Score | Skip:
        return self.run(view)
