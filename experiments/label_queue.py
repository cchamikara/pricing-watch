"""Build the hand-labelling queue: about 50 incident notices, spread across scenarios and answer
sources, in a Langfuse annotation queue that asks the judge's four questions.

A label made there is a human-sourced score (source ANNOTATION) under the judge's own score name,
so calibration compares the two by source. Running it again adds only notices not yet queued.

    uv run --env-file deploy/.env python -m experiments.label_queue --runs gemini-a,gemini-b
"""

import argparse
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pricing_watch.evals.judge import DIMENSIONS, facts
from pricing_watch.evals.loader import ContractError, fetch_trace
from pricing_watch.evals.scores import ensure_score_configs
from pricing_watch.evals.worker import ENVIRONMENTS
from pricing_watch.tracing import contract

QUEUE = "notice-labels"
DESCRIPTION = """Label each incident's closing notice (the notify_merchandiser step) against what
really happened in the trace: each policy:N step says whether a proposal was executed (allowed),
is waiting for a person (needs_approval), or was refused. Pass or fail each question:
1. clarity: understood in one read; names the product or category; plain words.
2. severity_fit: action_needed only when a person must act; warn when the agent acted on
   something worth knowing; info when nothing needs attention.
3. says_what_was_done: says what really happened (executed, waiting, refused), or that nothing
   was done.
4. says_what_needs_a_person: if a person must act, says what they need to do; else pass."""

Key = tuple[str, str]  # (scenario, answer source)


def stratified(candidates: list[tuple[Key, str]], size: int) -> list[str]:
    """Answer sources take turns, so the model's own notices get their share even when the
    fallback answered most incidents (a rate-limited free tier); within a source, scenarios take
    turns; within a scenario, oldest first."""
    by_source: dict[str, list[tuple[Key, str]]] = defaultdict(list)
    for key, trace_id in candidates:
        by_source[key[1]].append((key, trace_id))
    streams = [iter(_round_robin(by_source[source])) for source in sorted(by_source)]
    picked: list[str] = []
    while streams and len(picked) < size:
        for stream in list(streams):
            if (pick := next(stream, None)) is None:
                streams.remove(stream)
            elif len(picked) < size:
                picked.append(pick)
    return picked


def _round_robin(candidates: list[tuple[Key, str]]) -> list[str]:
    groups: dict[Key, list[str]] = defaultdict(list)
    for key, trace_id in candidates:
        groups[key].append(trace_id)
    queues, out = [groups[k] for k in sorted(groups)], []
    while any(queues):
        out += [q.pop(0) for q in queues if q]
    return out


def candidates(client: Any, runs: set[str], since: datetime) -> list[tuple[Key, str]]:
    """Every judgeable incident of these runs, oldest first."""
    found = []
    for env in ENVIRONMENTS:
        pages = client.iter_observation_pages(is_root_observation=True, environment=env,
                                              name=contract.INCIDENT.name, from_start_time=since,
                                              fields="core,basic,metadata", limit=1000)  # fmt: skip
        roots = [o for page in pages for o in page.data
                 if (o.metadata or {}).get("run_name") in runs]  # fmt: skip
        for root in sorted(roots, key=lambda o: o.start_time):
            try:
                view = fetch_trace(client, root.trace_id)
            except ContractError:  # an older run's shape: it can't be judged either
                continue
            if view.incident is not None and facts(view) is not None:
                key = (view.trace.scenario_id, view.incident.answer_source)
                found.append((key, root.trace_id))
    return found


def queue_id(client: Any, config_ids: dict[str, str]) -> str:
    page = 1
    while True:
        found = client.api.annotation_queues.list_queues(page=page, limit=100)
        for q in found.data:
            if q.name == QUEUE:
                return str(q.id)
        if page >= found.meta.total_pages:
            break
        page += 1
    created = client.api.annotation_queues.create_queue(
        name=QUEUE, description=DESCRIPTION,
        score_config_ids=[config_ids[f"judge_{d}"] for d in DIMENSIONS],
    )  # fmt: skip
    return str(created.id)


def queued(client: Any, queue: str) -> set[str]:
    ids, page = set(), 1
    while True:
        found = client.api.annotation_queues.list_queue_items(queue, page=page, limit=100)
        ids |= {item.object_id for item in found.data}
        if page >= found.meta.total_pages:
            return ids
        page += 1


def main() -> None:
    from pricing_watch.tracing.langfuse_tracer import langfuse_client

    parser = argparse.ArgumentParser(prog="experiments.label_queue")
    parser.add_argument("--runs", required=True, help="comma-separated run names to sample from")
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--days", type=int, default=30, help="how far back to look")
    args = parser.parse_args()

    client = langfuse_client()
    since = datetime.now(UTC) - timedelta(days=args.days)  # noqa: TID251  # bounds the query only
    found = candidates(client, set(args.runs.split(",")), since)
    picked = stratified(found, args.size)
    queue = queue_id(client, ensure_score_configs(client))
    already = queued(client, queue)
    for trace_id in picked:
        if trace_id not in already:
            client.api.annotation_queues.create_queue_item(queue, object_id=trace_id,
                                                           object_type="TRACE")  # fmt: skip
    groups = sorted({k for k, t in found if t in set(picked)})
    print(
        f"{QUEUE}: {len(picked)} notices from {len(found)} ({len(groups)} scenario/source "
        f"groups), {len(set(picked) - already)} added; label them in Langfuse: Annotation Queues"
    )


if __name__ == "__main__":
    main()
