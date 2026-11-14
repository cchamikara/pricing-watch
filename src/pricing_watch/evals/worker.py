"""The evaluator worker: polls Langfuse for finished traces, scores each one once, and writes a
coverage score per run.

A root observation reaches Langfuse only when its span ends, so a visible root means a finished
trace; the worker still waits SETTLE after its end for children still in the Collector's queue.
Roots arrive out of start order (a long incident ends after shorter ones that started later),
so each poll looks back LOOKBACK before the newest scored start, and a done set stops re-scoring.
"""

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pricing_watch.evals import Score, Skip
from pricing_watch.evals.loader import ContractError, fetch_trace
from pricing_watch.evals.proposal_safety import PROPOSAL_SAFETY
from pricing_watch.evals.scores import ensure_score_configs, write_score
from pricing_watch.evals.tripwires import TRIPWIRES
from pricing_watch.tracing import contract
from pricing_watch.tracing.hello import HELLO

log = logging.getLogger(__name__)

EVALUATORS = (*TRIPWIRES, *PROPOSAL_SAFETY)
COVERAGE = "evaluator_coverage"
ENVIRONMENTS = ("sim", "ci")  # never heartbeat: it would dilute every average
SETTLE = timedelta(seconds=30)
# shortcut: a trace that ends more than an hour after a later one was scored is never seen;
# raise it if incidents ever run that long
LOOKBACK = timedelta(hours=1)


@dataclass
class State:
    since: dict[str, str] = field(default_factory=dict)  # environment -> newest start scored
    done: dict[str, list[str]] = field(default_factory=dict)  # trace id -> [environment, start]
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)  # coverage counts per run

    @classmethod
    def load(cls, path: Path) -> "State":
        return cls(**json.loads(path.read_text())) if path.exists() else cls()

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self)))
        tmp.replace(path)  # atomic: a crash mid-write keeps the old state


def poll(
    client: Any,
    config_ids: dict[str, str],
    state: State,
    now: datetime,
    environments: tuple[str, ...] = ENVIRONMENTS,
) -> Counter[str]:
    """Score every settled, unscored trace; returns applied and skipped counts per evaluator."""
    tally: Counter[str] = Counter()
    touched = set()
    for env in environments:
        since = datetime.fromisoformat(state.since[env]) if env in state.since else now
        pages = client.iter_observation_pages(is_root_observation=True, environment=env,
                                              from_start_time=since - LOOKBACK,
                                              fields="core,basic,metadata", limit=1000)  # fmt: skip
        for root in sorted((o for page in pages for o in page.data), key=lambda o: o.start_time):
            if (root.trace_id in state.done or root.name == HELLO
                    or root.end_time is None or now - root.end_time < SETTLE):  # fmt: skip
                continue
            run = _score(client, config_ids, env, root, state, tally)
            if run:
                touched.add(run)
            state.done[root.trace_id] = [env, root.start_time.isoformat()]
            if env not in state.since or root.start_time > datetime.fromisoformat(state.since[env]):
                state.since[env] = root.start_time.isoformat()
    for key in touched:
        _write_coverage(client, config_ids, state.runs[key])
    cutoff = {env: datetime.fromisoformat(since) - LOOKBACK for env, since in state.since.items()}
    state.done = {t: [env, start] for t, (env, start) in state.done.items()
                  if datetime.fromisoformat(start) >= cutoff[env]}  # fmt: skip
    return tally


def _score(
    client: Any, config_ids: dict[str, str], env: str, root: Any, state: State, tally: Counter[str]
) -> str | None:
    """Score one trace; returns its run's key, or None if the root doesn't name its run."""
    meta = root.metadata or {}
    key = None
    if {"run_name", "scenario_id", "seed"} <= meta.keys():
        key = f"{env}|{meta['run_name']}|{meta['scenario_id']}|{meta['seed']}"
        state.runs.setdefault(key, {
            "environment": env, "run_name": meta["run_name"], "scenario_id": meta["scenario_id"],
            "seed": int(meta["seed"]), "first": root.start_time.isoformat(), "traces": 0,
            "errors": 0, "applied": {},
        })  # fmt: skip
    run = state.runs.get(key) if key else None
    if run:
        run["traces"] += 1
    try:
        view = fetch_trace(client, root.trace_id)
    except ContractError as e:  # loud, and counted in the run's coverage comment
        log.error("trace %s breaks the trace contract: %s", root.trace_id, e)
        tally["contract_errors"] += 1
        if run:
            run["errors"] += 1
        return key
    for evaluator in EVALUATORS:
        result = evaluator.evaluate(view)
        if isinstance(result, Skip):
            tally[f"{evaluator.name} skipped"] += 1
            continue
        write_score(client, config_ids, trace_id=root.trace_id, evaluator=evaluator.name,
                    version=evaluator.version, score=result, timestamp=root.start_time,
                    environment=env)  # fmt: skip
        tally[f"{evaluator.name} applied"] += 1
        if run:
            run["applied"][evaluator.name] = run["applied"].get(evaluator.name, 0) + 1
    return key


def _write_coverage(client: Any, config_ids: dict[str, str], run: dict[str, Any]) -> None:
    """On the run's anchor trace: the share of evaluators that scored at least one trace, with
    each one's count in the comment. Rewritten in place as the run's traces come in."""
    applied, traces = run["applied"], run["traces"]
    anchor = contract.RunTrace.model_validate(run).anchor()  # its keys name the run
    counts = ", ".join(f"{e.name} {applied.get(e.name, 0)}/{traces}" for e in EVALUATORS)
    score = Score(sum(1 for e in EVALUATORS if applied.get(e.name)) / len(EVALUATORS),
                  f"{counts}; contract errors {run['errors']}")  # fmt: skip
    write_score(client, config_ids, trace_id=anchor, evaluator=COVERAGE, version=1, score=score,
                timestamp=datetime.fromisoformat(run["first"]),
                environment=run["environment"])  # fmt: skip


def run_worker(client: Any, state_path: Path, interval_s: float, once: bool) -> None:
    config_ids = ensure_score_configs(client)
    while True:
        state = State.load(state_path)
        tally = poll(client, config_ids, state, datetime.now(UTC))  # noqa: TID251 bounds the query
        client.flush()  # the scores are sent before the state says they were
        state.save(state_path)
        if tally:
            log.info("scored: %s", ", ".join(f"{k} {v}" for k, v in sorted(tally.items())))
        if once:
            return
        time.sleep(interval_s)
