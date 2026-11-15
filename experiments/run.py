"""Run the agent over the scenario dataset as a Langfuse experiment.

Each item runs its scenario in agent mode, traced to Langfuse and recorded in memory, and a
rules-only baseline with the same seed (untraced: only its numbers matter). The checks need the
run's database and its traces, which only the task has, so the task makes them and the item
evaluators report them: each tripwire's and proposal-safety check's lowest score across the
scenario's traces, and whether its expected_output held. Two runs on the same dataset compare
side by side in Langfuse's dataset view.

    uv run --env-file deploy/.env python -m experiments.run --run-name pr-123 [--model claude]
"""

import argparse
import logging
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langfuse import Evaluation
from sqlalchemy.orm import Session

from experiments.sync_dataset import DATASET, pinned
from pricing_watch.agent.prompts import Prompts
from pricing_watch.evals.assertions import check_scenario, lowest_scores
from pricing_watch.evals.loader import from_tree, load_trace
from pricing_watch.evals.scores import SCORES, ensure_score_configs
from pricing_watch.models import make_engine
from pricing_watch.policy.config import load_policy
from pricing_watch.sim.run import run_scenario
from pricing_watch.sim.scenario import ScenarioConfig
from pricing_watch.tracing import InMemoryTracer, Observation, ObservationType, Tracer
from pricing_watch.tracing.contract import (
    AGENT_VERSION,
    EXPERIMENT_ENVIRONMENT,
    RunTrace,
    TraceInfo,
    trace_id,
)

log = logging.getLogger(__name__)
ASSERTIONS = "scenario_assertions"
RUN_EVALUATORS: list[Any] = []  # run-level comparisons with the baseline join here


class _Both:
    def __init__(self, handles: list[Observation]) -> None:
        self._handles = handles

    def set_output(self, output: object) -> None:
        for h in self._handles:
            h.set_output(output)

    def set_attributes(self, **attributes: object) -> None:
        for h in self._handles:
            h.set_attributes(**attributes)


class Tee:
    """Every observation to each tracer: Langfuse for people, memory for the checks."""

    def __init__(self, *tracers: Tracer) -> None:
        self._tracers = tracers

    @contextmanager
    def observation(
        self, name: str, type: ObservationType, input: object = None,
        trace: TraceInfo | None = None,
    ) -> Iterator[Observation]:  # fmt: skip
        with ExitStack() as stack:
            yield _Both([stack.enter_context(t.observation(name, type, input, trace))
                         for t in self._tracers])  # fmt: skip


def scenario_of(item: Any) -> ScenarioConfig:
    return ScenarioConfig.model_validate({**item.input, "expected_output": item.expected_output})


def make_task(
    tracer: Tracer, environment: str, run_name: str, model: str | None, prompts: Prompts
) -> Any:
    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        config = scenario_of(item)
        if model:
            config = config.model_copy(update={"model": model})
        log.info("%-20s started (%d days)", config.id, config.days)
        began = time.monotonic()
        memory = InMemoryTracer()
        trace_run = RunTrace(environment=environment, run_name=run_name)
        with tempfile.TemporaryDirectory() as tmp:
            db = f"sqlite:///{Path(tmp) / 'run.db'}"
            agent = run_scenario(config, Tee(tracer, memory), db, trace_run, prompts)
            start = config.start.replace(tzinfo=UTC)
            lows = lowest_scores(load_trace(from_tree(root, start)) for root in memory.roots)
            engine = make_engine(db)
            with Session(engine) as s:
                checked = check_scenario(s, config, lows)
            engine.dispose()
        baseline = run_scenario(config.model_copy(update={"mode": "rules"}))
        held = getattr(checked, "value", "skipped")
        log.info("%-20s done in %4.0fs  %4d traces  assertions %s", config.id,
                 time.monotonic() - began, len(memory.roots), held)  # fmt: skip
        return {
            "run_name": run_name,
            "trace_ids": [trace_id(r.trace.trace_seed) for r in memory.roots if r.trace],
            "lows": lows,
            "assertions": asdict(checked),  # value and comment, or a skip's reason
            "agent": asdict(agent),
            "baseline": asdict(baseline),
        }

    return task


def make_evaluators(config_ids: dict[str, str]) -> list[Any]:
    types = {s.name: s.data_type for s in SCORES}

    def safety(*, output: dict[str, Any], **_: Any) -> list[Evaluation]:
        return [Evaluation(name=name, value=value, comment="lowest across the scenario's traces",
                           data_type=types[name], config_id=config_ids[name])
                for name, value in output["lows"].items()]  # fmt: skip

    def assertions(*, output: dict[str, Any], **_: Any) -> list[Evaluation]:
        result = output["assertions"]
        if "value" not in result:  # the scenario asserts nothing
            return []
        return [Evaluation(name=ASSERTIONS, value=result["value"], comment=result["comment"],
                           data_type="NUMERIC", config_id=config_ids[ASSERTIONS])]  # fmt: skip

    return [safety, assertions]


def main() -> None:
    from pricing_watch.tracing.langfuse_tracer import LangfuseTracer, langfuse_client
    from pricing_watch.tracing.prompt_store import fetch_prompts

    parser = argparse.ArgumentParser(prog="experiments.run")
    parser.add_argument("--run-name", required=True, help="the experiment run; once per name")
    parser.add_argument("--model", choices=["stand-in", "claude", "gemini"],
                        help="override every scenario's model")  # fmt: skip
    parser.add_argument("--at", type=datetime.fromisoformat,
                        help="the dataset as it stood then (default: now)")  # fmt: skip
    parser.add_argument("--scenarios", help="comma-separated scenario ids (default: all)")
    parser.add_argument("--prompt-label", choices=["production", "ci"], default="ci")
    args = parser.parse_args()
    logging.basicConfig(format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    log.setLevel(logging.INFO)  # one line per scenario; the simulator's own lines stay quiet

    environment = EXPERIMENT_ENVIRONMENT  # where the SDK puts everything a task traces
    client = langfuse_client()
    at = args.at or datetime.now(UTC)  # noqa: TID251  # pins the dataset version only
    dataset = pinned(client, at)
    if args.scenarios:
        wanted = set(args.scenarios.split(","))
        if unknown := wanted - {i.input["id"] for i in dataset.items}:
            raise SystemExit(f"not in the dataset as of {at.isoformat()}: {sorted(unknown)}")
        dataset.items = [i for i in dataset.items if i.input["id"] in wanted]
    tracer = LangfuseTracer(client)
    for item in dataset.items:  # seeded trace ids: a second run under one name would merge
        config = scenario_of(item)
        anchor = RunTrace(environment=environment, run_name=args.run_name,
                          scenario_id=config.id, seed=config.seed).anchor()  # fmt: skip
        if tracer.has_trace(anchor):
            raise SystemExit(f"run {args.run_name!r} already traced {config.id}; pick another")
    prompts = fetch_prompts(client, args.prompt_label)
    # the SDK runs a plain task one item at a time: all 13 with the stand-in take about 90 s
    log.info("run %s: %d scenarios, one at a time", args.run_name, len(dataset.items))
    result = dataset.run_experiment(
        name=DATASET, run_name=args.run_name, description=f"dataset as of {at.isoformat()}",
        task=make_task(tracer, environment, args.run_name, args.model, prompts),
        evaluators=make_evaluators(ensure_score_configs(client)),
        run_evaluators=RUN_EVALUATORS,
        metadata={"agent_version": AGENT_VERSION, "model": args.model or "per scenario",
                  "prompt_version": str(prompts.version or "local"),
                  "policy_version": str(load_policy().policy_version)},
    )  # fmt: skip
    tracer.flush()
    print(result.format())


if __name__ == "__main__":
    main()
