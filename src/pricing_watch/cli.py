"""Command line.

pricing-watch sim run --scenario scenarios/smoke-7d.yaml [--seed N] [--db run.db] [--model M]
pricing-watch sim compare --scenario scenarios/supplier-cost-rise.yaml [--seed N] [--model M]
pricing-watch approvals list|approve ID|reject ID --db run.db [--note TEXT]
pricing-watch langfuse hello   (with uv run --env-file deploy/.env)
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from pricing_watch.agent.prompts import LOCAL_PROMPTS, PROMPT_NAME, Prompts
from pricing_watch.approvals.queue import decide, pending
from pricing_watch.models import PolicyDecision, Proposal, SalesTick, make_engine
from pricing_watch.policy.config import load_policy
from pricing_watch.sim.run import AGENT_HOUR, WARMUP_DAYS, compare, run_scenario
from pricing_watch.sim.scenario import load_scenario
from pricing_watch.tracing import NoopTracer
from pricing_watch.tracing.contract import RunTrace

if TYPE_CHECKING:
    from pricing_watch.tracing.langfuse_tracer import LangfuseTracer


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pricing-watch")
    groups = parser.add_subparsers(dest="group", required=True)
    sim = groups.add_parser("sim").add_subparsers(dest="command", required=True)
    run = sim.add_parser("run", help="run one scenario (its mode: rules, or agent too)")
    run.add_argument("--scenario", type=Path, required=True, help="path to a scenario YAML")
    run.add_argument("--seed", type=int, help="override the scenario's seed")
    run.add_argument("--db", type=Path, help="save the run to this new SQLite file")
    run.add_argument("--model", choices=["stand-in", "claude", "gemini"],
                     help="override the scenario's model (agent mode only)")  # fmt: skip
    run.add_argument("--trace", action="store_true",
                     help="send the run to Langfuse; needs --env-file deploy/.env")  # fmt: skip
    run.add_argument("--run-name", default="dev",
                     help="a traced run's name; each is traced once per environment")  # fmt: skip
    run.add_argument("--content-off", action="store_true",
                     help="with --trace, send prompt and message text as [masked]")  # fmt: skip
    run.add_argument("--prompt-label", choices=["production", "ci"],
                     help="with --trace, the Langfuse prompt label (default: ci in the ci "
                          "environment, else production)")  # fmt: skip
    cmp = sim.add_parser("compare", help="run a scenario against every baseline, same seed")
    cmp.add_argument("--scenario", type=Path, required=True, help="path to a scenario YAML")
    cmp.add_argument("--seed", type=int, help="override the scenario's seed")
    cmp.add_argument("--model", choices=["stand-in", "claude", "gemini"],
                     help="the agent's model for the last run")  # fmt: skip
    approvals = groups.add_parser("approvals").add_subparsers(dest="command", required=True)
    approvals.add_parser("list").add_argument("--db", type=Path, required=True)
    for verb in ("approve", "reject"):
        cmd = approvals.add_parser(verb)
        cmd.add_argument("proposal_id", type=int)
        cmd.add_argument("--db", type=Path, required=True)
        cmd.add_argument("--note", default="")
    langfuse = groups.add_parser("langfuse").add_subparsers(dest="command", required=True)
    langfuse.add_parser("hello", help="send a hello trace to each environment; check it arrived")
    push = langfuse.add_parser("push-prompts", help="make the local templates the labelled prompt")
    push.add_argument("--label", choices=["production", "ci"], required=True)
    args = parser.parse_args(argv)

    if args.group == "langfuse" and args.command == "push-prompts":
        _push_prompts(args.label)
    elif args.group == "langfuse":
        _langfuse_hello()
    elif args.group == "sim" and args.command == "compare":
        _sim_compare(args)
    elif args.group == "sim":
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
    if args.model:
        config = config.model_copy(update={"model": args.model})
    db_url = f"sqlite:///{args.db}" if args.db else "sqlite://"
    if config.mode == "agent":  # one line per incident as it closes, so a long run isn't silent
        logging.basicConfig(format="%(message)s", stream=sys.stdout)  # libraries stay at WARNING
        logging.getLogger("pricing_watch").setLevel(logging.INFO)  # only our own progress lines
        print(
            f"running {config.id}: {config.days} days with {config.model}, after "
            f"{WARMUP_DAYS} warm-up days; incidents are checked daily at {AGENT_HOUR:02}:00"
        )
        print("sim time          kind                scope    answered by    model  severity"
              "      actions  ✓ ran ⏸ approval ✗ refused   notice")  # fmt: skip
    tracer, trace_run, prompts = None, None, LOCAL_PROMPTS
    if args.trace:
        environment = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "sim")
        tracer, prompts = _langfuse_tracer(environment, args.prompt_label, args.content_off)
        trace_run = RunTrace(environment=environment, run_name=args.run_name,
                             scenario_id=config.id, seed=config.seed)  # fmt: skip
        if tracer.has_trace(trace_run.anchor()):  # the same ids again would duplicate its traces
            raise SystemExit(f"run '{args.run_name}' of {config.id} seed {config.seed} is already "
                             f"in Langfuse ({environment}); pick another --run-name, or delete "
                             "those traces first")  # fmt: skip
    try:
        summary = run_scenario(config, tracer=tracer, db_url=db_url, trace_run=trace_run,
                               prompts=prompts)  # fmt: skip
    finally:  # even when the run crashes: those spans explain why
        if tracer:
            tracer.flush()
    agent = f" ({config.model})" if config.mode == "agent" else ""
    print(
        f"scenario {config.id}  seed {config.seed}  {config.days} days  mode {config.mode}{agent}"
    )
    print(f"gross margin ex GST  ${summary.gross_margin_ex_gst_cents / 100:,.2f}")
    print(f"stockout SKU-hours   {summary.stockout_sku_hours}")
    if config.mode == "agent":
        fallbacks = f"{summary.rules_fallbacks} by rules fallback"
        print(f"incidents            {summary.incidents} ({fallbacks})")
    print(f"fingerprint          {summary.fingerprint[:16]}")
    print("sell-through")
    for sku, share in summary.sell_through.items():
        print(f"  {sku}  {share:6.1%}")


def _sim_compare(args: argparse.Namespace) -> None:
    config = load_scenario(args.scenario)
    if args.seed is not None:
        config = config.model_copy(update={"seed": args.seed})
    if args.model:
        config = config.model_copy(update={"model": args.model})
    print(f"scenario {config.id}  seed {config.seed}  {config.days} days")
    runs = compare(config)
    base = runs["rules, ignoring text"].gross_margin_ex_gst_cents
    header = ("run", "gross margin ex GST", "vs rules", "stockout h", "incidents",
              "matches right/wrong/missed")  # fmt: skip
    print(f"{header[0]:22} {header[1]:>20} {header[2]:>12} {header[3]:>11} {header[4]:>10}  "
          f"{header[5]}")  # fmt: skip
    for label, sm in runs.items():
        delta = (sm.gross_margin_ex_gst_cents - base) / 100
        print(f"{label:22} {sm.gross_margin_ex_gst_cents / 100:>20,.2f} {delta:>+12,.2f} "
              f"{sm.stockout_sku_hours:>11} {sm.incidents:>10}  {sm.listings_matched_right}/"
              f"{sm.listings_matched_wrong}/{sm.listings_missed}")  # fmt: skip


def _langfuse_tracer(
    environment: str, label: str | None, content_off: bool
) -> tuple["LangfuseTracer", Prompts]:
    """The tracer, and the prompts fetched by label (the local templates if unavailable)."""
    from pricing_watch.tracing.langfuse_tracer import LangfuseTracer, langfuse_client
    from pricing_watch.tracing.masking import mask_content
    from pricing_watch.tracing.prompt_store import fetch_prompts

    mask = mask_content if content_off else None
    client = langfuse_client(environment=environment, mask_otel_spans=mask)
    label = label or ("ci" if environment == "ci" else "production")
    return LangfuseTracer(client), fetch_prompts(client, label)


def _push_prompts(label: str) -> None:
    from langfuse import Langfuse

    from pricing_watch.tracing.prompt_store import push_prompts

    print(f"{PROMPT_NAME} v{push_prompts(Langfuse(), label)} is labelled {label}")


def _langfuse_hello() -> None:
    from pricing_watch.tracing.hello import check  # imports the SDK only when asked

    results = check()
    for environment, ok in results.items():
        print(f"{environment:10} {'arrived' if ok else 'NOT FOUND after 60 s'}")
    if not all(results.values()):
        raise SystemExit(1)


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
    except ValueError as e:  # already decided
        raise SystemExit(str(e)) from e
    s.commit()
    if not approve:
        print(f"rejected {proposal_id}")
    elif action is None:
        print(f"approved {proposal_id}, but the re-check refused it; nothing executed")
    else:
        print(f"approved and executed {proposal_id}: {action.result}")
