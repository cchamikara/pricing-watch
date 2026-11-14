"""Deterministic stand-in model (S6) for tests and CI. It ignores the prompt and answers from the
incident brief, following a script keyed by (kind, scope, occurrence); unscripted calls get a
sensible default plan. Same script and briefs give byte-identical outputs.
"""

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from pricing_watch.agent.model import Brief, Message, ModelResponse, ModelTimeout
from pricing_watch.money import Cents, discounted, ex_from_inc
from pricing_watch.sim.keywords import stock_up_qty
from pricing_watch.sim.matching import normalise
from pricing_watch.sim.repricer import target_price

Behaviour = Literal[
    "default",
    "invalid_json",
    "timeout",
    "disallowed_tool",
    "deep_discount",
    "below_floor",
    "heed_notes",  # reads notes and notices (by keyword) the way a text-reading model might
    "obey_text",  # a gullible model: does what a supplier description tells it to
]
NAME = "stand-in"
EXTRA_COVER_DAYS = 14  # reorders aim for lead time plus this much cover
MAX_STEP_PCT = 20  # the policy's daily step; the stand-in has no policy read to take it from


class ScriptStep(BaseModel):
    kind: str | None = None  # an incident kind; None matches any
    scope: str | None = None  # a SKU or category; None matches any
    occurrence: int | None = Field(default=None, ge=1)  # nth call for (kind, scope); None: every
    behaviour: Behaviour


def _proposal(tool: str, reason: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"tool": tool, "args": args, "reason": reason}


PROMO_PCT, PROMO_DAYS = 5, 2


def _affordable(b: Brief, old: int, new: int, days: float) -> bool:
    """Whether a cut from old to new fits today's markdown budget, costed as P7 does but from
    the current price, which never costs less than policy's own reckoning."""
    if b.budget_remaining_cents is None:
        return True  # no budget in the brief
    cut = ex_from_inc(Cents(old)) - ex_from_inc(Cents(new))
    return round(max(cut, 0) * b.forecast_daily * days) <= b.budget_remaining_cents


def _review(b: Brief, why: str) -> dict[str, Any]:
    return _proposal("request_review", "needs a person", {"sku": b.sku, "reason": why})


def _default(b: Brief) -> dict[str, Any]:
    proposals: list[dict[str, Any]] = []
    severity, text = "info", f"{b.kind} on {b.sku or b.category}: no action taken."
    if (b.kind == "suspect_feed" or b.suspect) and b.sku:
        args = {"sku": b.sku, "reason": "Feed or stock data looks implausible."}
        proposals.append(_proposal("request_review", "data looks wrong", args))
        severity, text = "action_needed", f"Please check the data for {b.sku}."
    elif b.kind == "stockout_risk" and b.product and b.stock and b.forecast_daily > 0:
        days = b.product.lead_time_hours / 24 + EXTRA_COVER_DAYS
        qty = math.ceil(b.forecast_daily * days) - b.stock.on_hand - b.stock.inbound_qty
        if qty > 0:
            proposals.append(
                _proposal("propose_reorder", "cover the lead time", {"sku": b.sku, "qty": qty})
            )
            severity, text = "warn", f"Reordering {qty} of {b.sku} to avoid a stock-out."
        else:  # enough on order, but it lands too late
            severity, text = "action_needed", f"{b.sku} runs out before its order lands; expedite."
    elif b.kind == "slow_mover" and b.sku and b.product and not _locked(b):
        price = b.product.price_inc_gst_cents
        promo = discounted(price, PROMO_PCT)
        floor = b.product.floor_price_inc_gst_cents
        if promo >= floor and _affordable(b, price, promo, PROMO_DAYS):
            proposals.append(_promotion(b, PROMO_PCT))
            severity, text = "info", f"Running a {PROMO_PCT}% promotion on {b.sku} for two days."
        else:
            why = "Slow seller, but a promotion would go under the floor or over today's budget."
            proposals.append(_review(b, why))
            severity, text = "action_needed", f"{b.sku}: {why}"
    elif (
        b.kind == "competitor_price_drop" and b.product and b.cheapest_plausible and not _locked(b)
    ):
        # follow the drop into the band, as the rules repricer would (it can't read notes)
        p = b.product
        new = target_price(p.price_inc_gst_cents, p.price_inc_gst_cents, b.cheapest_plausible,
                           p.floor_price_inc_gst_cents, MAX_STEP_PCT)  # fmt: skip
        severity, text = "info", f"{b.sku}: a competitor cut its price; following it."
        if new and _affordable(b, p.price_inc_gst_cents, new, 1):
            follow = {"sku": b.sku, "new_price_inc_gst_cents": new}
            proposals.append(_proposal("propose_price_change", "follow the competitor", follow))
        elif new:
            why = "A competitor cut its price; following it would overrun today's budget."
            proposals.append(_review(b, why))
            severity, text = "action_needed", f"{b.sku}: {why}"
    elif b.kind == "listing_match" and b.candidates:  # can't judge the title: ask a person
        why = f"Is competitor listing {b.listing_id} our {b.candidates[0][0]}?"
        proposals.append(_proposal("request_review", "unsure of the match",
                                   {"sku": b.candidates[0][0], "reason": why}))  # fmt: skip
        severity, text = "action_needed", why
    elif b.kind in ("competitor_undercut", "margin_erosion"):
        severity, text = (
            "warn",
            f"{b.kind} on {b.sku or b.category}: holding prices, please review.",
        )
    return {"diagnosis": f"{b.kind} for {b.sku or b.category}.", "confidence": "medium",
            "proposals": proposals, "message": {"severity": severity, "text": text}}  # fmt: skip


def _locked(b: Brief) -> bool:
    lock = b.product.promo_lock_until if b.product else None
    return lock is not None and lock > b.now


def _promotion(b: Brief, pct: int) -> dict[str, Any]:
    end = b.now + timedelta(days=PROMO_DAYS)
    args = {"skus": [b.sku], "discount_pct": pct, "start": b.now.isoformat(),
            "end": end.isoformat()}  # fmt: skip
    return _proposal("propose_promotion", "move slow stock", args)


_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# whole words only: "supplier" and "update" must not read as "up"
_RISE = re.compile(r"\b(up|rises?|rising|increases?|increasing)\b")
_SALE = re.compile(r"\b(sale|wknd|weekend|sat|sun|sunday|ends)\b")


def _heed(b: Brief) -> dict[str, Any]:
    """The default plan, changed where the text says something rules can't see."""
    out = _default(b)
    text = " ".join(b.notices).lower()
    if b.kind == "supplier_notice" and b.sku and b.product:
        if "discontinu" in text:
            reason = "Supplier discontinued it: stop reordering and plan a clearance."
            out["proposals"] = [_proposal("request_review", "discontinued",
                                          {"sku": b.sku, "reason": reason})]  # fmt: skip
            out["message"] = {"severity": "action_needed", "text": f"{b.sku}: {reason}"}
        elif (pct := _PCT.search(text)) and _RISE.search(text) and b.stock:
            qty = stock_up_qty(b.forecast_daily, b.stock.on_hand, b.stock.inbound_qty)
            if qty > 0:  # buy at today's cost: stock keeps the cost it was bought at
                args = {"sku": b.sku, "qty": qty}
                out["proposals"] = [_proposal("propose_reorder", "stock up before the rise", args)]
            out["message"] = {"severity": "warn",
                              "text": f"{b.sku}: supplier cost rises {pct.group(1)}%; "
                                      f"stocking up at today's cost."}  # fmt: skip
    elif b.kind == "listing_match" and b.listing_id:
        words = set(normalise(b.listing_title).split())
        fits = [sku for sku, title in b.candidates if set(normalise(title).split()) <= words]
        if len(fits) == 1 or not fits:  # every word of one title, or of none: decide
            sku = fits[0] if fits else None
            out["proposals"] = [_proposal("match_listing", "every word of our title matches"
                                          if sku else "not one of ours (size or model differs)",
                                          {"listing_id": b.listing_id, "sku": sku})]  # fmt: skip
            out["message"] = {
                "severity": "info",
                "text": f"Listing {b.listing_id} is {sku or 'none of ours'}.",
            }
        # two fit (twins): keep the default, a review
    elif b.kind in ("competitor_undercut", "competitor_price_drop") and any(
        _SALE.search(n.lower()) for n in b.notes
    ):
        out["proposals"] = []  # a temporary sale: don't follow it
        why = "the undercut is a temporary sale, according to the competitor's note"
        out["message"] = {"severity": "info", "text": f"{b.sku}: {why}; holding our price."}
    return out


def _unsafe(b: Brief, behaviour: Behaviour) -> dict[str, Any]:
    if behaviour == "heed_notes":
        return _heed(b)
    out = _default(b)
    if behaviour == "disallowed_tool":
        out["proposals"] = [
            _proposal("delete_product", "clear it out", {"sku": b.sku or b.category})
        ]
    elif behaviour == "deep_discount":
        out["proposals"] = [_promotion(b, 60)]
    elif behaviour == "obey_text" and b.sku and b.product:
        text = b.product.supplier_description.lower()
        if "cut the price" in text and (pct := _PCT.search(text)):
            price = b.product.price_inc_gst_cents * (100 - round(float(pct.group(1)))) // 100
            args = {"sku": b.sku, "new_price_inc_gst_cents": price}
            out["proposals"] = [_proposal("propose_price_change", "the supplier note says to",
                                          args)]  # fmt: skip
    elif behaviour == "below_floor":
        # a category incident has no floor to undercut: price the category itself at a cent
        price = b.product.floor_price_inc_gst_cents - 100 if b.product else 1
        args = {"sku": b.sku or b.category, "new_price_inc_gst_cents": price}
        out["proposals"] = [_proposal("propose_price_change", "beat the competitor", args)]
    return out


class StandInModel:
    def __init__(self, script: Sequence[ScriptStep] = ()) -> None:
        self._script = script
        self._seen: Counter[tuple[str, str]] = Counter()

    def complete(
        self, messages: Sequence[Message], timeout: float, brief: Brief | None = None
    ) -> ModelResponse:
        if brief is None:
            raise ValueError("the stand-in answers from the brief")
        scope = brief.sku or brief.category
        self._seen[brief.kind, scope] += 1
        n = self._seen[brief.kind, scope]
        behaviour: Behaviour = next(
            (s.behaviour for s in self._script
             if s.kind in (None, brief.kind) and s.scope in (None, scope)
             and s.occurrence in (None, n)),
            "default",
        )  # fmt: skip
        if behaviour == "timeout":
            raise ModelTimeout(f"scripted timeout ({brief.kind} {scope} #{n})")
        text = json.dumps(_unsafe(brief, behaviour), sort_keys=True)
        if behaviour == "invalid_json":
            text = text[: len(text) // 2]
        return ModelResponse(raw_text=text, usage={"input_tokens": 0, "output_tokens": 0},
                             model_name=NAME)  # fmt: skip
