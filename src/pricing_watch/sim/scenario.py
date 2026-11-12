"""Scenario config, loaded from scenarios/*.yaml. Fault times are hour offsets from the start."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from pricing_watch.agent.standin import ScriptStep
from pricing_watch.sim.faults import Fault, FaultKind
from pricing_watch.sim.suppliers import NoticeConfig


class FaultConfig(BaseModel):
    kind: FaultKind
    start_hour: int = Field(ge=0)
    hours: int = Field(gt=0)
    sku: str | None = None


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
    text_rules: bool = False  # the rules read text by keyword; default: they ignore it

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
