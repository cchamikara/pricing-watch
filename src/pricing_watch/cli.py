"""Command line: pricing-watch sim run --scenario scenarios/smoke-7d.yaml [--seed N]."""

import argparse
from pathlib import Path

from pricing_watch.sim.run import run_scenario
from pricing_watch.sim.scenario import load_scenario


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pricing-watch")
    sim = parser.add_subparsers(dest="group", required=True).add_parser("sim")
    run = sim.add_subparsers(dest="command", required=True).add_parser(
        "run", help="run one scenario in rules mode (agent mode arrives in M3)"
    )
    run.add_argument("--scenario", type=Path, required=True, help="path to a scenario YAML")
    run.add_argument("--seed", type=int, help="override the scenario's seed")
    args = parser.parse_args(argv)

    config = load_scenario(args.scenario)
    if args.seed is not None:
        config = config.model_copy(update={"seed": args.seed})
    summary = run_scenario(config)
    print(f"scenario {config.id}  seed {config.seed}  {config.days} days  mode rules")
    print(f"gross margin ex GST  ${summary.gross_margin_ex_gst_cents / 100:,.2f}")
    print(f"stockout SKU-hours   {summary.stockout_sku_hours}")
    print(f"fingerprint          {summary.fingerprint[:16]}")
    print("sell-through")
    for sku, share in summary.sell_through.items():
        print(f"  {sku}  {share:6.1%}")
