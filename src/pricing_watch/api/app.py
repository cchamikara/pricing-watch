"""The HTTP API and approval page, over a run's database (the file `sim run --db` writes).

The simulated "now" is the run's last hour, as for the CLI's approvals: a decision re-checks
policy against the state the run ended in (S7). A decision also scores the incident's trace,
so a person's call shows next to the agent's in Langfuse.

    uv run --env-file deploy/.env pricing-watch serve --db run.db
"""

import hashlib
import hmac
import html
import time
from collections.abc import Callable, Iterator
from datetime import UTC
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from pricing_watch.approvals.queue import decide, pending
from pricing_watch.evals import Score
from pricing_watch.evals.scores import ensure_score_configs, write_score
from pricing_watch.models import (
    Incident,
    PolicyDecision,
    Product,
    Proposal,
    SalesTick,
    StockLevel,
    make_engine,
)
from pricing_watch.money import discounted, floor_price
from pricing_watch.policy.config import PolicyConfig, load_policy
from pricing_watch.policy.rules import DbStateView
from pricing_watch.sim.repricer import is_fresh, latest_prices
from pricing_watch.sim.run import run_scenario
from pricing_watch.sim.scenario import ScenarioConfig, load_scenario
from pricing_watch.tracing import NoopTracer, Tracer
from pricing_watch.tracing.contract import RunTrace

SCENARIOS = Path(__file__).parents[3] / "scenarios"
SIGNATURE_MAX_AGE_S = 300  # an older signed request is a replay
APPROVAL_SCORE = "approval_decision"


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""
    reviewer: str = "api"


def create_app(
    db: Path,
    client: Any = None,  # a Langfuse client: trace links and the decision score; None: neither
    *,
    tracer: Tracer | None = None,
    policy: PolicyConfig | None = None,  # the run's scenario policy; None: the default policy
    environment: str = "sim",
    ui_url: str | None = None,  # Langfuse as a browser reaches it, for links
    webhook_secret: str | None = None,
    start_run: Callable[[ScenarioConfig, str], None] | None = None,
    config_ids: dict[str, str] | None = None,
) -> FastAPI:
    app = FastAPI(title="pricing-watch")
    tracer = tracer or NoopTracer()
    policy = policy or load_policy()
    ids = config_ids

    def link(trace_id: str) -> str | None:
        """A trace link for a browser. The client may know Langfuse by a name only Docker
        resolves (langfuse-web), so links are rebuilt on ui_url when it is set."""
        if client is None or not trace_id:
            return None
        url: str = client.get_trace_url(trace_id=trace_id)
        return f"{ui_url.rstrip('/')}{url[url.index('/project/') :]}" if ui_url else url

    def session() -> Iterator[Session]:
        if not db.exists():
            raise HTTPException(503, f"no run database at {db}; create one with sim run --db")
        engine = make_engine(f"sqlite:///{db}")
        with Session(engine) as s:
            yield s
        engine.dispose()

    Db = Annotated[Session, Depends(session)]

    def now(s: Session) -> Any:
        t = s.scalar(select(func.max(SalesTick.sim_time)))
        if t is None:
            raise HTTPException(503, "the run database has no simulated time")
        return t

    def queue(s: Session) -> list[dict[str, Any]]:
        rows = []
        for a in pending(s):
            p, d = s.get_one(Proposal, a.proposal_id), s.get_one(PolicyDecision, a.proposal_id)
            rows.append({"proposal_id": p.id, "incident_id": p.incident_id, "tool": p.tool,
                         "args": p.args, "reason": p.reason, "policy": d.detail,
                         "rule_ids": d.rule_ids})  # fmt: skip
        return rows

    def decided(s: Session, proposal_id: int, body: DecisionIn) -> dict[str, Any]:
        nonlocal ids
        try:
            action = decide(s, tracer, policy, proposal_id, approve=body.approve,
                            reviewer=body.reviewer, note=body.note, t=now(s))  # fmt: skip
        except NoResultFound as e:
            raise HTTPException(404, f"no approval for proposal {proposal_id}") from e
        except ValueError as e:  # already decided
            raise HTTPException(409, str(e)) from e
        s.commit()
        executed = action is not None
        proposal = s.get_one(Proposal, proposal_id)
        incident = s.get(Incident, proposal.incident_id) if proposal.incident_id else None
        if client is not None and incident is not None and incident.trace_id:
            ids = ids or ensure_score_configs(client)
            outcome = "executed" if executed else ("not executed: policy re-check refused it"
                                                   if body.approve else "rejected")  # fmt: skip
            comment = f"{body.reviewer}: {outcome}" + (f". {body.note}" if body.note else "")
            # one score per proposal: its id stands in for the evaluator version in the score id
            write_score(client, ids, trace_id=incident.trace_id, evaluator=APPROVAL_SCORE,
                        version=proposal_id, score=Score(1.0 if body.approve else 0.0, comment),
                        timestamp=incident.opened_at.replace(tzinfo=UTC),
                        environment=environment)  # fmt: skip
            client.flush()
        status = "approved" if body.approve else "rejected"
        return {"proposal_id": proposal_id, "status": status, "executed": executed}

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/store/state")
    def store_state(s: Db) -> dict[str, Any]:
        t = now(s)
        view = DbStateView(s, t)
        products = [{"sku": p.sku, "category": p.category,
                     "price_inc_gst_cents": p.price_inc_gst_cents,
                     "on_hand": s.get_one(StockLevel, p.sku).on_hand}
                    for p in s.scalars(select(Product).order_by(Product.sku))]  # fmt: skip
        budgets = {c: b - view.markdown_spent_cents(c)
                   for c, b in policy.markdown_budget_cents.items()}  # fmt: skip
        return {"sim_time": t.isoformat(), "products": products,
                "budget_remaining_cents": budgets}  # fmt: skip

    @app.get("/incidents")
    def incidents(s: Db, status: str | None = None, kind: str | None = None) -> list[Any]:
        q = select(Incident).order_by(Incident.opened_at, Incident.id)
        if status == "open":
            q = q.where(Incident.closed_at.is_(None))
        elif status == "closed":
            q = q.where(Incident.closed_at.is_not(None))
        if kind:
            q = q.where(Incident.kind == kind)
        return [{"id": i.id, "kind": i.kind, "category": i.category, "sku": i.sku,
                 "opened_at": i.opened_at.isoformat(), "closed": i.closed_at is not None,
                 "answer_source": i.answer_source,
                 "trace_url": link(i.trace_id)}
                for i in s.scalars(q)]  # fmt: skip

    @app.get("/approvals")
    def approvals(s: Db, status: str = "pending") -> list[dict[str, Any]]:
        if status != "pending":
            raise HTTPException(400, "only the pending queue is listed")
        return queue(s)

    @app.post("/approvals/{proposal_id}/decision")
    def decision(s: Db, proposal_id: int, body: DecisionIn) -> dict[str, Any]:
        return decided(s, proposal_id, body)

    @app.post("/approvals/{proposal_id}/form")
    def decision_form(
        s: Db, request: Request, proposal_id: int, decision: Annotated[str, Form()],
        note: Annotated[str, Form()] = "",
    ) -> RedirectResponse:  # fmt: skip
        # no login, so refuse a form another site submits through the browser (CSRF); browsers
        # send Origin on every cross-site post
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            raise HTTPException(403, "this form only accepts posts from its own page")
        body = DecisionIn(approve=decision == "approve", note=note, reviewer="page")
        result = decided(s, proposal_id, body)
        done = f"{result['status']}{' and executed' if result['executed'] else ''}"
        return RedirectResponse(f"/?done={proposal_id}+{done}", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def page(s: Db, done: str | None = None, source: str | None = None) -> str:
        t = now(s)
        view = DbStateView(s, t)
        rows = sorted(queue(s), key=lambda r: (r["incident_id"] is None, r["proposal_id"]))
        if source in ("agent", "rules"):
            rows = [r for r in rows if (r["incident_id"] is not None) == (source == "agent")]
        cards = [_card(s, view, r, link, policy.auto_apply_pct) for r in rows]
        return _page(cards, t.isoformat(sep=" ", timespec="minutes"), done, source)

    @app.post("/scenarios/{scenario_id}/run", status_code=202)
    async def run(scenario_id: str, request: Request, tasks: BackgroundTasks) -> dict[str, str]:
        if not webhook_secret:
            raise HTTPException(503, "scenario runs are off: no LANGFUSE_WEBHOOK_SECRET")
        at = _verify(request.headers.get("x-langfuse-signature"), await request.body(),
                     webhook_secret)  # fmt: skip
        if scenario_id not in {p.stem for p in SCENARIOS.glob("*.yaml")}:  # no paths from users
            raise HTTPException(404, f"no scenario {scenario_id!r}")
        config, run_name = load_scenario(SCENARIOS / f"{scenario_id}.yaml"), f"webhook-{at}"
        tasks.add_task(start_run or _runner(tracer, environment), config, run_name)
        return {"scenario": scenario_id, "run_name": run_name}

    return app


def _verify(header: str | None, body: bytes, secret: str) -> int:
    """Langfuse's x-langfuse-signature: "t=<unix time>,v1=<hex HMAC-SHA256 of 't.body'>"."""
    try:
        t_part, v_part = (header or "").split(",", 1)
        at, given = int(t_part.split("=", 1)[1]), bytes.fromhex(v_part.split("=", 1)[1])
    except (IndexError, ValueError) as e:
        raise HTTPException(401, "missing or malformed x-langfuse-signature") from e
    expected = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).digest()
    if not hmac.compare_digest(given, expected):
        raise HTTPException(401, "bad signature")
    if abs(time.time() - at) > SIGNATURE_MAX_AGE_S:  # noqa: TID251  # real time: replays
        raise HTTPException(401, "signature too old")
    return at


def _runner(tracer: Tracer, environment: str) -> Callable[[ScenarioConfig, str], None]:
    def start(config: ScenarioConfig, run_name: str) -> None:
        run_scenario(config, tracer, trace_run=RunTrace(environment=environment,
                                                        run_name=run_name))  # fmt: skip

    return start


def _short(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _change(old: int, new: int) -> str:
    pct = (new - old) * 100 / old
    return f"{'−' if pct < 0 else '+'}{abs(pct):.1f}%"


def _product(s: Session, view: DbStateView, sku: str) -> dict[str, Any] | None:
    product = s.get(Product, sku)
    if product is None:  # an unknown SKU: policy refuses it anyway (P3)
        return None
    on_hand, inbound = view.stock(sku)
    t = view.now
    quotes = [{"competitor": r.competitor_id, "price": r.price_inc_gst_cents,
               "hours": int((t - r.published_at).total_seconds() // 3600),
               "stale": not is_fresh(r, t), "plausible": r.plausible}
              for r in sorted(latest_prices(s, sku),
                              key=lambda r: r.price_inc_gst_cents)]  # fmt: skip
    return {"sku": sku, "category": product.category,
            "description": product.supplier_description, "price": product.price_inc_gst_cents,
            "floor": floor_price(product.cost_ex_gst_cents, product.margin_floor_pct),
            "cost": product.cost_ex_gst_cents, "on_hand": on_hand, "inbound": inbound,
            "forecast": view.forecast_daily(sku), "discount": view.discount_pct(sku),
            "quotes": quotes}  # fmt: skip


def _card(
    s: Session,
    view: DbStateView,
    row: dict[str, Any],
    link: Callable[[str], str | None],
    auto_apply_pct: int,
) -> dict[str, Any]:
    """What a person needs to decide: the change in dollars, against our price, floor, stock and
    the competitors, and where the proposal came from."""
    args, tool = row["args"], row["tool"]
    skus = args.get("skus") or ([args["sku"]] if args.get("sku") else [])
    products = [p for sku in skus if (p := _product(s, view, sku))]
    change, market = "", ""
    if tool == "propose_price_change" and products:
        old, new = products[0]["price"], args["new_price_inc_gst_cents"]
        change = f"Price {_money(old)} → {_money(new)} ({_change(old, new)})"
        live = [q for q in products[0]["quotes"] if q["plausible"] and not q["stale"]]
        if live:  # quotes are cheapest first; the market may have moved since it was proposed
            cheapest = live[0]
            gap = abs(new - cheapest["price"])
            side = "below" if new < cheapest["price"] else "above"
            market = (
                f"{_money(gap)} {side} the cheapest competitor now "
                f"({cheapest['competitor']} at {_money(cheapest['price'])})"
            )
    elif tool == "propose_promotion":
        pct, days = args["discount_pct"], args["start"][:16].replace("T", " ")
        change = (f"{pct}% off from {days} to {args['end'][:16].replace('T', ' ')}: "
                  + ", ".join(f"{p['sku']} {_money(p['price'])} → "
                              f"{_money(discounted(p['price'], pct))}"
                              for p in products))  # fmt: skip
    elif tool == "propose_reorder" and products:
        p = products[0]
        cover = ((p["on_hand"] + p["inbound"] + args["qty"]) / p["forecast"]
                 if p["forecast"] else None)  # fmt: skip
        change = (f"Reorder {args['qty']} units"
                  + (f": about {cover:.0f} days of cover after" if cover else ""))  # fmt: skip
    why = (
        f"Needs a person (P10): a change of more than {auto_apply_pct}% waits for approval."
        if "P10-approval-tier" in row["rule_ids"]
        else row["policy"]
    )
    incident = s.get(Incident, row["incident_id"]) if row["incident_id"] else None
    trace_id = incident.trace_id if incident is not None else ""
    trace = link(trace_id)
    return row | {"change": change or tool.replace("_", " "), "market": market, "why": why,
                  "products": products,
                  "incident": incident, "trace_url": trace}  # fmt: skip


TOOLS = {"propose_price_change": "Price change", "propose_promotion": "Promotion",
         "propose_reorder": "Reorder", "request_review": "Review",
         "match_listing": "Listing match"}  # fmt: skip


def _page(cards: list[dict[str, Any]], sim_time: str, done: str | None,
          source: str | None) -> str:  # fmt: skip
    e = html.escape

    def product(p: dict[str, Any]) -> str:
        quotes = (
            "".join(
                f"<tr><td>{e(q['competitor'])}</td><td>{_money(q['price'])}</td>"
                f"<td>{q['hours']} h ago{' · <b>stale</b>' if q['stale'] else ''}"
                f"{' · <b>implausible</b>' if not q['plausible'] else ''}</td></tr>"
                for q in p["quotes"]
            )
            or '<tr><td colspan="3">No competitor lists it.</td></tr>'
        )
        promo = f" ({p['discount']}% promotion running)" if p["discount"] else ""
        return f"""
  <h3>{e(p["sku"])} <small>{e(p["category"])} · {e(_short(p["description"]))}</small></h3>
  <table>
    <tr><th>Our price</th><td>{_money(p["price"])}{promo}</td></tr>
    <tr><th>Floor</th><td>{_money(p["floor"])}
      <small>(cost {_money(p["cost"])} ex GST)</small></td></tr>
    <tr><th>Stock</th><td>{p["on_hand"]} on hand, {p["inbound"]} on order;
      sells about {p["forecast"]:.1f} a day</td></tr>
  </table>
  <table class="quotes"><tr><th>Competitor</th><th>Price</th><th>Published</th></tr>
    {quotes}</table>"""

    def card(c: dict[str, Any]) -> str:
        inc = c["incident"]
        origin = (f"agent · {e(inc.kind)} opened {inc.opened_at:%Y-%m-%d %H:%M}" if inc
                  else "rules · routine reprice")  # fmt: skip
        link = f' · <a href="{e(c["trace_url"])}">trace</a>' if c["trace_url"] else ""
        title = TOOLS.get(c["tool"], c["tool"])
        return f"""
<section>
  <h2>#{c["proposal_id"]} · {e(title)}</h2>
  <p class="origin">{origin}{link}</p>
  <p class="change">{e(c["change"])}</p>
  {f'<p class="market">{e(c["market"])}</p>' if c["market"] else ""}
  {"".join(product(p) for p in c["products"])}
  <p><b>Why proposed:</b> {e(c["reason"])}<br><b>Policy:</b> {e(c["why"])}</p>
  <form method="post" action="/approvals/{c["proposal_id"]}/form">
    <label>Note <input name="note" maxlength="500"></label>
    <button name="decision" value="approve">Approve</button>
    <button name="decision" value="reject">Reject</button>
  </form>
</section>"""

    body = "".join(card(c) for c in cards) or "<p>No proposals are waiting here.</p>"
    flash = f'<p role="status" class="done">Proposal {e(done)}.</p>' if done else ""
    tabs = " · ".join(f'<a href="/{q}"{" aria-current=page" if source == v else ""}>{n}</a>'
                      for n, v, q in (("All", None, ""), ("Agent", "agent", "?source=agent"),
                                      ("Rules", "rules", "?source=rules")))  # fmt: skip
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Approvals · pricing-watch</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 48rem; margin: 2rem auto;
          padding: 0 1rem; color: #222; }}
  section {{ border: 1px solid #ccc; border-radius: 6px; padding: 0 1rem 1rem; margin: 1rem 0; }}
  h2 {{ margin-bottom: 0; }} h3 {{ margin: .8rem 0 .3rem; }} small {{ color: #666; }}
  .origin {{ margin-top: 0; color: #555; }} .change {{ font-size: 1.15rem; font-weight: 600;
          margin-bottom: 0; }} .market {{ margin-top: .2rem; color: #8a4b00; }}
  table {{ border-collapse: collapse; margin: .3rem 0; }}
  th, td {{ text-align: left; padding: .15rem .8rem .15rem 0; vertical-align: top; }}
  .quotes th {{ border-bottom: 1px solid #ddd; }}
  .done {{ background: #eef7ee; padding: .5rem; }} [aria-current] {{ font-weight: 700; }}
  button {{ margin-left: .5rem; padding: .3rem .8rem; }}
</style></head>
<body><h1>Approvals</h1>
<p>Simulated time {e(sim_time)}. A decision re-checks policy first. Prices include GST.</p>
<p>{tabs} · {len(cards)} waiting</p>
{flash}{body}
</body></html>"""
