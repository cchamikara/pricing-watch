"""Prompt templates (S2) and the incident summary sent to the model.

The templates use Langfuse's {{variable}} syntax. These local copies are the fallback when Prompt
Management is unreachable (wired in M4.5). Supplier text goes in escaped <supplier_data> blocks,
so it can't close its block and pose as instructions.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from html import escape
from pathlib import Path

from pydantic import BaseModel

from pricing_watch.agent.tools import (
    CompetitorRead,
    ForecastRead,
    PolicyRead,
    ProductRead,
    StockRead,
)

TEMPLATES = Path(__file__).with_name("templates")
SYSTEM_TEMPLATE = (TEMPLATES / "system_prompt.md").read_text()
INCIDENT_TEMPLATE = (TEMPLATES / "incident_prompt.md").read_text()
MODEL_TOOLS = ("propose_price_change", "propose_promotion", "propose_reorder", "request_review")
READ_NAMES: dict[type[BaseModel], str] = {
    ProductRead: "get_product", StockRead: "get_stock", CompetitorRead: "get_competitor_prices",
    ForecastRead: "get_demand_forecast", PolicyRead: "get_policy",
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
    summary: dict[str, list[dict[str, object]]] = {}
    blocks = []
    for read in reads:
        dumped = read.model_dump(mode="json", exclude={"supplier_description"})
        summary.setdefault(READ_NAMES[type(read)], []).append(dumped)
        if isinstance(read, ProductRead):
            blocks.append(f'<supplier_data sku="{escape(read.sku)}">'
                          f"{escape(read.supplier_description)}</supplier_data>")  # fmt: skip
    return render(INCIDENT_TEMPLATE, {
        "kind": kind, "scope": scope, "now": now.isoformat(),
        "suspect": ", ".join(suspect) or "none",
        "summary": json.dumps(summary, sort_keys=True, separators=(",", ":")),
        "supplier_data": "\n".join(blocks) or "(none)",
    })  # fmt: skip
