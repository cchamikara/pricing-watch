"""Agent against the rules baseline over every catalogue scenario and several seeds, in memory
with the stand-in (free). Each (scenario, seed) gets the three outcome scores; a scenario's
margin verdict over its seeds is the majority; the success criterion ("beats baseline in at
least half the scenarios, loses on none") is then applied to those verdicts.

The scenarios' own assertions are tuned to their seed and aren't checked here: outcomes compare
the agent with its baseline in whatever world a seed makes.

    uv run python -m experiments.multi_seed --seeds 1,2,3 [--outputs seeds.json]
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from experiments.gate import publish
from experiments.run import make_task
from experiments.sync_dataset import _item, catalogue
from pricing_watch.agent.prompts import LOCAL_PROMPTS
from pricing_watch.evals import Score
from pricing_watch.evals.outcomes import (
    beats_baseline,
    margin_vs_baseline,
    slow_mover_sell_through,
    stockout_hours_vs_baseline,
)
from pricing_watch.tracing import NoopTracer

log = logging.getLogger("experiments.run")  # the runner's per-scenario progress lines


def majority(values: list[float]) -> float:
    """1.0 when more than half are wins, 0.0 when more than half are losses, else 0.5."""
    if sum(v == 1.0 for v in values) * 2 > len(values):
        return 1.0
    if sum(v == 0.0 for v in values) * 2 > len(values):
        return 0.0
    return 0.5


def summarise(rows: list[dict[str, Any]]) -> tuple[dict[str, float], Score]:
    margins: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        margins[row["scenario"]].append(row["margin"])
    verdicts = {s: majority(ms) for s, ms in margins.items()}
    return verdicts, beats_baseline(list(verdicts.values()))


def run(seeds: list[int]) -> list[dict[str, Any]]:
    task = make_task(NoopTracer(), "offline", "offline", None, LOCAL_PROMPTS)
    rows = []
    for config in catalogue():
        for seed in seeds:
            item = SimpleNamespace(**_item(config))
            item.input = {**item.input, "seed": seed}
            out = task(item=item)
            agent, baseline = out["agent"], out["baseline"]
            slow = slow_mover_sell_through(agent, baseline, out["slow_movers"])
            rows.append({
                "scenario": config.id, "seed": seed,
                "margin": margin_vs_baseline(agent, baseline).value,
                "stockout": stockout_hours_vs_baseline(agent, baseline).value,
                "slow_movers": slow.value if isinstance(slow, Score) else None,
                "margin_pct": (agent["gross_margin_ex_gst_cents"]
                               / baseline["gross_margin_ex_gst_cents"] - 1) * 100,
                "stockout_hours": (agent["stockout_sku_hours"], baseline["stockout_sku_hours"]),
            })  # fmt: skip
    return rows


def table(rows: list[dict[str, Any]]) -> str:
    verdicts, beats = summarise(rows)
    mark = {1.0: "win", 0.5: "tie", 0.0: "loss", None: "-"}
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scenario[row["scenario"]].append(row)
    lines = ["| Scenario | Margin per seed (vs baseline) | Margin verdict | Stock-outs per seed "
             "| Slow movers per seed |", "| --- | --- | --- | --- | --- |"]  # fmt: skip
    for scenario, rs in by_scenario.items():
        margins = ", ".join(f"{mark[r['margin']]} ({r['margin_pct']:+.1f}%)" for r in rs)
        stockouts = ", ".join(mark[r["stockout"]] for r in rs)
        slow = ", ".join(mark[r["slow_movers"]] for r in rs)
        lines.append(f"| {scenario} | {margins} | **{mark[verdicts[scenario]]}** | {stockouts} "
                     f"| {slow} |")  # fmt: skip
    criterion = "met" if beats.value == 1.0 else "not met"
    return "\n".join(lines) + f"\n\nSuccess criterion {criterion}: margin {beats.comment}.\n"


def main() -> None:
    parser = argparse.ArgumentParser(prog="experiments.multi_seed")
    parser.add_argument("--seeds", default="1,2,3", help="comma-separated seeds")
    parser.add_argument("--outputs", type=Path, help="save every (scenario, seed) row as JSON")
    args = parser.parse_args()
    logging.basicConfig(format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    log.setLevel(logging.INFO)
    rows = run([int(s) for s in args.seeds.split(",")])
    if args.outputs:
        args.outputs.write_text(json.dumps(rows, indent=1))
    report = table(rows)
    publish(report)
    print(report)


if __name__ == "__main__":
    main()
