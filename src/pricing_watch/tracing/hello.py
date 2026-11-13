"""Check the Langfuse stack end to end: send a hello trace to each environment, then ask
the API whether it arrived there. Keys and URL come from LANGFUSE_* variables (deploy/.env)."""

import time

from langfuse import Langfuse, propagate_attributes

from pricing_watch.tracing.langfuse_tracer import langfuse_client

ENVIRONMENTS = ("sim", "ci", "heartbeat")


def send_hello(lf: Langfuse, environment: str) -> str:
    """One `hello` span in the given environment; returns its trace id."""
    with (
        propagate_attributes(environment=environment),  # outside, so the root span carries it
        lf.start_as_current_observation(as_type="span", name="hello") as span,
    ):
        span.update(output={"environment": environment})
        trace_id = lf.get_current_trace_id()
    if trace_id is None:
        raise RuntimeError("Langfuse gave the hello span no trace id; is tracing disabled?")
    return trace_id


def arrived(lf: Langfuse, trace_id: str, environment: str, wait_s: float = 60) -> bool:
    """Whether the trace shows up under that environment within wait_s (ingestion is async)."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        found = lf.api.observations.get_many(trace_id=trace_id, environment=environment)
        if found.data:
            return True
        time.sleep(2)
    return False


def check(environments: tuple[str, ...] = ENVIRONMENTS) -> dict[str, bool]:
    lf = langfuse_client()  # the hello traces take the app's route: through the Collector
    if not lf.auth_check():
        raise SystemExit("Langfuse rejected the keys; run with: uv run --env-file deploy/.env")
    sent = {env: send_hello(lf, env) for env in environments}
    lf.flush()
    return {env: arrived(lf, trace_id, env) for env, trace_id in sent.items()}
