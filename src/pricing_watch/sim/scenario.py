"""Scenario config, loaded from scenarios/*.yaml. Fault times are hour offsets from the start."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pricing_watch.agent.standin import ScriptStep
from pricing_watch.policy.config import PolicyConfig
from pricing_watch.sim.faults import Fault, FaultKind
from pricing_watch.sim.suppliers import NoticeConfig


class FaultConfig(BaseModel):
    kind: FaultKind
    start_hour: int = Field(ge=0)
    hours: int = Field(gt=0)
    sku: str | None = None


class NearMissConfig(BaseModel):
    """A competitor listing that looks like one of ours but isn't: another size, a bundle."""

    competitor: str
    like_sku: str  # the product of ours it resembles
    title: str
    price_pct: int = Field(gt=0)  # its price, as a share of like_sku's catalogue price
    start_hour: int = Field(ge=0)


class ExpectedOutput(BaseModel):
    """What must hold after the run, as checks the scenario assertion evaluator can make."""

    model_config = ConfigDict(extra="forbid")

    no_price_change: list[str] = []  # SKUs whose shelf price never changes
    request_review_raised: list[str] = []  # SKUs the agent asks a person about
    reorder_before_stockout: list[str] = []  # SKUs reordered before they run out
    all_incidents_notified: bool = False
    budget_never_exceeded: bool = False
    no_action_matching_injection: bool = False  # nothing does what injected text asked
    no_wrong_listing_match: bool = False  # no competitor listing matched to the wrong product
    # score -> the lowest value it takes across the run, overriding the gate's default (1.0);
    # tripwires are never overridden
    expected_scores: dict[str, float] = {}


class ScenarioConfig(BaseModel):
    id: str
    seed: int
    days: int = Field(gt=0)
    start: datetime
    faults: list[FaultConfig] = []
    reviewer: Literal["approve_all", "reject_all"] = "approve_all"  # scripted, same tick
    mode: Literal["rules", "agent"] = "rules"  # agent: the incident loop runs alongside the rules
    model: Literal["stand-in", "claude", "gemini"] = "stand-in"
    model_script: list[ScriptStep] = []  # the stand-in's script; real models ignore it
    supplier_notices: list[NoticeConfig] = []  # text, plus the change it announces (hidden)
    near_misses: list[NearMissConfig] = []
    text_rules: bool = False  # the rules read text by keyword; default: they ignore it
    policy: dict[str, Any] = {}  # changes to the default policy, e.g. a tighter reorder cap
    expected_output: ExpectedOutput = ExpectedOutput()

    def policy_config(self, base: PolicyConfig) -> PolicyConfig:
        """The base policy with this scenario's changes; a mapping such as the budgets merges."""
        merged = base.model_dump()
        for key, value in self.policy.items():
            merged[key] = merged[key] | value if isinstance(value, dict) else value
        return PolicyConfig.model_validate(merged)

    def fault_schedule(self) -> list[Fault]:
        return [
            Fault(
                f.kind,
                self.start + timedelta(hours=f.start_hour),
                self.start + timedelta(hours=f.start_hour + f.hours),
                f.sku,
            )
            for f in self.faults
        ]


def load_scenario(path: Path) -> ScenarioConfig:
    return ScenarioConfig.model_validate(yaml.safe_load(path.read_text()))
