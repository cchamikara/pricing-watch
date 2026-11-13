"""The tracing seam backed by Langfuse. Keys and URL come from the LANGFUSE_* variables.

Observation types map one to one. Attributes become Langfuse metadata, one key each, except a
generation's model and token usage, which go to Langfuse's own fields so it can show and price
them. An exception marks the observation as an error before it propagates.
"""

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from langfuse import Langfuse, propagate_attributes
from langfuse.types import TraceContext
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from pricing_watch.tracing import Observation, ObservationType
from pricing_watch.tracing.contract import TraceInfo, trace_id


def langfuse_client(**options: Any) -> Langfuse:
    """API calls (prompts, lookups) go to LANGFUSE_BASE_URL; spans go only to the local Collector
    (OTEL_COLLECTOR_ENDPOINT), which queues them on disk and forwards them, so an outage or a
    restart doesn't lose them. The SDK still applies its masking hook before they leave."""
    endpoint = os.environ.get("OTEL_COLLECTOR_ENDPOINT")
    if not endpoint:
        raise SystemExit("OTEL_COLLECTOR_ENDPOINT is not set: spans go through the Collector "
                         "only; run with uv run --env-file deploy/.env")  # fmt: skip
    return Langfuse(span_exporter=OTLPSpanExporter(endpoint=endpoint), **options)


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
            name, version = attributes.get("prompt_name"), attributes.get("prompt_version")
            if name and version:  # link to the prompt version; this span is the active one
                with propagate_attributes(prompt={"name": name, "version": version}):
                    pass
        self._span.update(metadata=attributes, **fields)


class LangfuseTracer:
    def __init__(self, client: Langfuse | None = None) -> None:
        self._lf = client or Langfuse()

    @contextmanager
    def observation(
        self, name: str, type: ObservationType, input: object = None,
        trace: TraceInfo | None = None,
    ) -> Iterator[Observation]:  # fmt: skip
        with ExitStack() as stack:
            context: TraceContext | None = None
            if trace:  # a root: its seeded trace id, session and trace attributes, from the start
                context = {"trace_id": trace_id(trace.trace_seed)}
                stack.enter_context(propagate_attributes(
                    session_id=trace.session_id, tags=trace.tags, trace_name=trace.trace_name,
                    metadata=trace.attributes.model_dump(exclude_none=True),
                ))  # fmt: skip
            span = stack.enter_context(self._lf.start_as_current_observation(
                as_type=type, name=name, input=input, trace_context=context))  # fmt: skip
            try:
                yield _Handle(span, generation=type == "generation")
            except BaseException as e:
                span.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}")
                raise

    def has_trace(self, trace_id: str) -> bool:
        """Whether Langfuse already holds this trace. Ingestion is asynchronous, so a trace sent
        in the last few seconds may not show yet."""
        return bool(self._lf.api.observations.get_many(trace_id=trace_id, limit=1).data)

    def flush(self) -> None:
        """Send whatever is buffered; call before the process exits."""
        self._lf.flush()
