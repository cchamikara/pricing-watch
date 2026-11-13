"""Exit checks against Langfuse: does it hold what the run sent, and what SQLite recorded?

A run's observations are found by the run_name and scenario_id every span carries in its
metadata. Ingestion is asynchronous, so counts are polled until they match or time runs out.
"""

import json
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from langfuse import Langfuse


def run_observations(
    client: Langfuse, environment: str, scenario_id: str, run_name: str, since: datetime
) -> list[Any]:
    found = []
    pages = client.iter_observation_pages(environment=environment, from_start_time=since,
                                          fields="core,basic,io,metadata", limit=1000)  # fmt: skip
    for page in pages:
        found += [o for o in page.data
                  if (o.metadata or {}).get("run_name") == run_name
                  and (o.metadata or {}).get("scenario_id") == scenario_id]  # fmt: skip
    return found


def refused_guardrails(observations: Sequence[Any]) -> int:
    """`policy:N` guardrails whose decision was refused: one per refused PolicyDecision row."""
    return sum(o.name.startswith("policy:") and _decision(o.output) == "refused"
               for o in observations)  # fmt: skip


def _decision(output: object) -> object:
    if isinstance(output, str):  # the API returns output as JSON text or already parsed
        try:
            output = json.loads(output)
        except ValueError:
            return None
    return output.get("decision") if isinstance(output, dict) else None


def wait_for(count: Callable[[], int], expected: int, timeout_s: float) -> int:
    """Poll until the count reaches expected, or timeout; the last count either way."""
    deadline = time.monotonic() + timeout_s
    while True:
        seen = count()
        if seen == expected or time.monotonic() > deadline:
            return seen
        time.sleep(5)
