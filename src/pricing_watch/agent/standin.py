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

Behaviour = Literal[
    "default",
    "invalid_json",
    "timeout",
    "disallowed_tool",
    "deep_discount",
    "below_floor",
    "heed_notes",  # reads notes and notices (by keyword) the way a text-reading model might
]
NAME = "stand-in"
EXTRA_COVER_DAYS = 14  # reorders aim for lead time plus this much cover


class ScriptStep(BaseModel):
    kind: str | None = None  # an incident kind; None matches any
    scope: str | None = None  # a SKU or category; None matches any
    occurrence: int | None = Field(default=None, ge=1)  # nth call for (kind, scope); None: every
    behaviour: Behaviour


def _proposal(tool: str, reason: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"tool": tool, "args": args, "reason": reason}


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
    elif b.kind == "slow_mover" and b.sku and not _locked(b):
        proposals.append(_promotion(b, 5))
        severity, text = "info", f"Running a 5% promotion on {b.sku} for two days."
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
    end = b.now + timedelta(days=2)
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
        elif (pct := _PCT.search(text)) and _RISE.search(text):
            price = math.ceil(b.product.price_inc_gst_cents * (1 + float(pct.group(1)) / 100))
            args = {"sku": b.sku, "new_price_inc_gst_cents": price}
            out["proposals"] = [_proposal("propose_price_change", "pass on the cost rise", args)]
            out["message"] = {"severity": "warn",
                              "text": f"{b.sku}: supplier cost rises {pct.group(1)}%; "
                                      f"raising the price to keep the margin."}  # fmt: skip
    elif b.kind == "competitor_undercut" and any(_SALE.search(n.lower()) for n in b.notes):
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
