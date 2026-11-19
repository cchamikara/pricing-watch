"""Compare a real-model run with the last accepted one: no scenario may do worse by more than the
tolerance. Until the outcome scores exist, "worse" is the agent's own gross margin (lower) and
stock-out hours (higher), matched by scenario and seed; every repeat is compared on its own.

    uv run python -m experiments.compare outputs.json previous/outputs.json [--tolerance 0.05]

A missing previous file passes: the first accepted run has nothing to compare with.
"""

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.gate import publish

STOCKOUT_SLACK_HOURS = 1  # a few hours either way is noise, not a regression


def compare(
    now: list[dict[str, Any] | None], before: list[dict[str, Any] | None], tolerance: float
) -> tuple[bool, str]:
    accepted = {(o["scenario"], o["seed"]): o["agent"] for o in before if o}
    rows, passed = [], True
    for out in now:
        if out is None:
            continue  # it raised: the gate fails it
        was, agent = accepted.get((out["scenario"], out["seed"])), out["agent"]
        key = f"| {out['scenario']} | {out.get('repeat', 1)} |"
        if was is None:
            rows.append(f"{key} new | no accepted run yet |")
            continue
        margin, stockout = agent["gross_margin_ex_gst_cents"], agent["stockout_sku_hours"]
        worse = []
        if margin < (was_margin := was["gross_margin_ex_gst_cents"]) * (1 - tolerance):
            worse.append(f"margin ${margin / 100:,.0f} vs ${was_margin / 100:,.0f}")
        if stockout > was["stockout_sku_hours"] * (1 + tolerance) + STOCKOUT_SLACK_HOURS:
            worse.append(f"stock-out hours {stockout} vs {was['stockout_sku_hours']}")
        passed &= not worse
        verdict = "FAIL" if worse else "ok"
        rows.append(f"{key} {verdict} | {'; '.join(worse)} |")
    head = [f"Compared with the last accepted run (tolerance {tolerance:.0%})", "",
            "| Scenario | Repeat | Result | Why |", "| --- | --- | --- | --- |"]  # fmt: skip
    table = "\n".join([*head, *rows]) + "\n"
    publish(table)
    return passed, table


def main() -> None:
    parser = argparse.ArgumentParser(prog="experiments.compare")
    parser.add_argument("now", type=Path)
    parser.add_argument("before", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.05)
    args = parser.parse_args()
    before = json.loads(args.before.read_text()) if args.before.exists() else []
    passed, table = compare(json.loads(args.now.read_text()), before, args.tolerance)
    print(table)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
