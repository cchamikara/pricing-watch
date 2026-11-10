"""Command line.

pricing-watch sim run --scenario scenarios/smoke-7d.yaml [--seed N] [--db run.db]
pricing-watch approvals list|approve ID|reject ID --db run.db [--note TEXT]
"""

import argparse
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from pricing_watch.approvals.queue import decide, pending
from pricing_watch.models import PolicyDecision, Proposal, SalesTick, make_engine
from pricing_watch.policy.config import load_policy
from pricing_watch.sim.run import run_scenario
from pricing_watch.sim.scenario import load_scenario
from pricing_watch.tracing import NoopTracer


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pricing-watch")
    groups = parser.add_subparsers(dest="group", required=True)
    sim = groups.add_parser("sim").add_subparsers(dest="command", required=True)
    run = sim.add_parser("run", help="run one scenario in rules mode (agent mode arrives in M3)")
    run.add_argument("--scenario", type=Path, required=True, help="path to a scenario YAML")
    run.add_argument("--seed", type=int, help="override the scenario's seed")
    run.add_argument("--db", type=Path, help="save the run to this new SQLite file")
    approvals = groups.add_parser("approvals").add_subparsers(dest="command", required=True)
    approvals.add_parser("list").add_argument("--db", type=Path, required=True)
    for verb in ("approve", "reject"):
        cmd = approvals.add_parser(verb)
        cmd.add_argument("proposal_id", type=int)
        cmd.add_argument("--db", type=Path, required=True)
        cmd.add_argument("--note", default="")
    args = parser.parse_args(argv)

    if args.group == "sim":
        _sim_run(args)
    elif args.command == "list":
        _approvals_list(args.db)
    else:
        _approvals_decide(args.db, args.proposal_id, args.command == "approve", args.note)


def _sim_run(args: argparse.Namespace) -> None:
    if args.db and args.db.exists():
        raise SystemExit(f"{args.db} already exists; pick a new file")
    config = load_scenario(args.scenario)
    if args.seed is not None:
        config = config.model_copy(update={"seed": args.seed})
    db_url = f"sqlite:///{args.db}" if args.db else "sqlite://"
    summary = run_scenario(config, db_url=db_url)
    print(f"scenario {config.id}  seed {config.seed}  {config.days} days  mode rules")
    print(f"gross margin ex GST  ${summary.gross_margin_ex_gst_cents / 100:,.2f}")
    print(f"stockout SKU-hours   {summary.stockout_sku_hours}")
    print(f"fingerprint          {summary.fingerprint[:16]}")
    print("sell-through")
    for sku, share in summary.sell_through.items():
        print(f"  {sku}  {share:6.1%}")


def _open(db: Path) -> Session:
    if not db.exists():
        raise SystemExit(f"{db} not found; create it with: pricing-watch sim run --db {db}")
    return Session(make_engine(f"sqlite:///{db}"))


def _approvals_list(db: Path) -> None:
    s = _open(db)
    rows = pending(s)
    if not rows:
        print("no pending approvals")
    for a in rows:
        p = s.get_one(Proposal, a.proposal_id)
        d = s.get_one(PolicyDecision, a.proposal_id)
        print(f"{p.id}  {p.tool}  {p.args}  {d.detail}  reason: {p.reason}")


def _approvals_decide(db: Path, proposal_id: int, approve: bool, note: str) -> None:
    s = _open(db)
    # decide at the run's last simulated hour, so the re-check sees the state the run ended in
    t = s.scalar(select(func.max(SalesTick.sim_time)))
    if t is None:
        raise SystemExit(f"{db} has no simulated time")
    try:
        action = decide(s, NoopTracer(), load_policy(), proposal_id,
                        approve=approve, reviewer="cli", note=note, t=t)  # fmt: skip
    except NoResultFound as e:
        raise SystemExit(f"no approval for proposal {proposal_id}") from e
    except ValueError as e:  # already decided, or a tool the executor can't run yet
        raise SystemExit(str(e)) from e
    s.commit()
    if not approve:
        print(f"rejected {proposal_id}")
    elif action is None:
        print(f"approved {proposal_id}, but the re-check refused it; nothing executed")
    else:
        print(f"approved and executed {proposal_id}: {action.result}")
