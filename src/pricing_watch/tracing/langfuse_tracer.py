"""The tracing seam backed by Langfuse. Keys and URL come from the LANGFUSE_* variables.

Observation types map one to one. Attributes become Langfuse metadata, one key each, except a
generation's model and token usage, which go to Langfuse's own fields so it can show and price
them. An exception marks the observation as an error before it propagates.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langfuse import Langfuse

from pricing_watch.tracing import Observation, ObservationType

# our usage keys (the providers' names) -> the keys Langfuse prices tokens by
_USAGE_KEYS = {"input_tokens": "input", "output_tokens": "output"}


class _Handle:
    def __init__(self, span: Any, generation: bool) -> None:
        self._span, self._generation = span, generation

    def set_output(self, output: object) -> None:
        self._span.update(output=output)

    def set_attributes(self, **attributes: object) -> None:
        fields: dict[str, Any] = {}
        if self._generation:
            if (model := attributes.pop("model", None)) is not None:
                fields["model"] = model
            if isinstance(usage := attributes.pop("usage", None), dict):
                fields["usage_details"] = {_USAGE_KEYS.get(k, k): v for k, v in usage.items()}
        self._span.update(metadata=attributes, **fields)


class LangfuseTracer:
    def __init__(self, client: Langfuse | None = None) -> None:
        self._lf = client or Langfuse()

    @contextmanager
    def observation(
        self, name: str, type: ObservationType, input: object = None
    ) -> Iterator[Observation]:
        with self._lf.start_as_current_observation(as_type=type, name=name, input=input) as span:
            try:
                yield _Handle(span, generation=type == "generation")
            except BaseException as e:
                span.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}")
                raise

    def flush(self) -> None:
        """Send whatever is buffered; call before the process exits."""
        self._lf.flush()
