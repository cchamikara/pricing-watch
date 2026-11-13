"""Tracing seam. Code emits observations through a Tracer; Langfuse plugs in behind it."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from pricing_watch.tracing.contract import TraceInfo

ObservationType = Literal["agent", "tool", "generation", "guardrail", "span"]


class Observation(Protocol):
    def set_output(self, output: object) -> None: ...

    def set_attributes(self, **attributes: object) -> None: ...


class Tracer(Protocol):
    def observation(
        self, name: str, type: ObservationType, input: object = None,
        trace: "TraceInfo | None" = None,  # a root starting its own trace; None: a child
    ) -> AbstractContextManager[Observation]: ...  # fmt: skip


@dataclass
class Node:
    """One recorded observation. Also serves as the throwaway handle NoopTracer yields."""

    name: str
    type: ObservationType
    input: object
    output: object = None
    attributes: dict[str, object] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    trace: "TraceInfo | None" = None

    def set_output(self, output: object) -> None:
        self.output = output

    def set_attributes(self, **attributes: object) -> None:
        self.attributes.update(attributes)


class NoopTracer:
    @contextmanager
    def observation(
        self, name: str, type: ObservationType, input: object = None,
        trace: "TraceInfo | None" = None,
    ) -> Iterator[Observation]:  # fmt: skip
        yield Node(name, type, input)


class InMemoryTracer:
    """Records observations as a tree; its output becomes evaluator fixtures later."""

    def __init__(self) -> None:
        self.roots: list[Node] = []
        # shortcut: one stack per tracer, so it assumes one incident at a time; switch to
        # contextvars if incidents ever run concurrently.
        self._stack: list[Node] = []

    @contextmanager
    def observation(
        self, name: str, type: ObservationType, input: object = None,
        trace: "TraceInfo | None" = None,
    ) -> Iterator[Observation]:  # fmt: skip
        node = Node(name, type, input, trace=trace)
        (self._stack[-1].children if self._stack else self.roots).append(node)
        self._stack.append(node)
        try:
            yield node
        finally:
            self._stack.pop()
