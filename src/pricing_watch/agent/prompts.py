"""Prompt templates (S2) and the incident summary sent to the model.

The templates use Langfuse's {{variable}} syntax. These local copies are the fallback when Prompt
Management is unreachable. Text from outside the business goes in escaped
<supplier_data>, <competitor_note> and <supplier_notice> blocks, so it can't close its block and
pose as instructions.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pricing_watch.agent.tools import (
    CompetitorRead,
    ForecastRead,
    PolicyRead,
    ProductRead,
    StockRead,
    SupplierNoticesRead,
)

TEMPLATES = Path(__file__).with_name("templates")
SYSTEM_TEMPLATE = (TEMPLATES / "system_prompt.md").read_text()
INCIDENT_TEMPLATE = (TEMPLATES / "incident_prompt.md").read_text()
MODEL_TOOLS = ("propose_price_change", "propose_promotion", "propose_reorder", "request_review")
READ_NAMES: dict[type[BaseModel], str] = {
    ProductRead: "get_product", StockRead: "get_stock", CompetitorRead: "get_competitor_prices",
    ForecastRead: "get_demand_forecast", PolicyRead: "get_policy",
    SupplierNoticesRead: "get_supplier_notices",
}  # fmt: skip
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render(template: str, variables: Mapping[str, str]) -> str:
    """Fill {{name}} placeholders in one pass; every placeholder needs exactly one variable."""
    names = set(_PLACEHOLDER.findall(template))
    if missing := names - variables.keys():
        raise ValueError(f"missing prompt variables: {sorted(missing)}")
    if unknown := variables.keys() - names:
        raise ValueError(f"unknown prompt variables: {sorted(unknown)}")
    return _PLACEHOLDER.sub(lambda m: variables[m.group(1)], template)


def incident_prompt(
    kind: str, scope: str, now: datetime, suspect: Sequence[str], reads: Sequence[BaseModel]
) -> str:
    """The incident prompt. Text written outside the business (supplier descriptions and notices,
    competitor notes) stays out of the JSON summary and goes into escaped, delimited blocks."""
    summary: dict[str, list[dict[str, object]]] = {}
    product_blocks, note_blocks, notice_blocks = [], [], []
    for read in reads:
        summary.setdefault(READ_NAMES[type(read)], []).append(
            read.model_dump(mode="json", exclude=_OUTSIDE_TEXT.get(type(read)))
        )
        if isinstance(read, ProductRead):
            product_blocks.append(_block("supplier_data", {"sku": read.sku},
                                         read.supplier_description))  # fmt: skip
        elif isinstance(read, CompetitorRead):
            note_blocks += [_block("competitor_note", {"competitor": q.competitor_id,
                                                       "sku": read.sku}, q.note)
                            for q in read.prices if q.note]  # fmt: skip
        elif isinstance(read, SupplierNoticesRead):
            notice_blocks += [_block("supplier_notice", {"scope": n.sku or n.category or "",
                                                         "sent": n.sent_at.isoformat()}, n.text)
                              for n in read.notices]  # fmt: skip
    return render(INCIDENT_TEMPLATE, {
        "kind": kind, "scope": scope, "now": now.isoformat(),
        "suspect": ", ".join(suspect) or "none",
        "summary": json.dumps(summary, sort_keys=True, separators=(",", ":")),
        "supplier_data": "\n".join(product_blocks) or "(none)",
        "competitor_notes": "\n".join(note_blocks) or "(none)",
        "supplier_notices": "\n".join(notice_blocks) or "(none)",
    })  # fmt: skip


# fields holding outside text, left out of the summary because they go in blocks instead
_OUTSIDE_TEXT: dict[type[BaseModel], Any] = {
    ProductRead: {"supplier_description"},
    CompetitorRead: {"prices": {"__all__": {"note"}}},
    SupplierNoticesRead: {"notices": {"__all__": {"text"}}},
}


def _block(tag: str, attributes: Mapping[str, str], text: str) -> str:
    """Escaped, so the text can neither close its block nor fake an attribute."""
    attrs = "".join(f' {k}="{escape(v)}"' for k, v in attributes.items())
    return f"<{tag}{attrs}>{escape(text)}</{tag}>"
