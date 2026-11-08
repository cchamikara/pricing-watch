"""The versioned policy config. Semantics of each threshold are documented in config.yaml."""

from datetime import date
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_PATH = Path(__file__).with_name("config.yaml")


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: date
    auto_apply_pct: int = Field(gt=0)
    max_daily_step_pct: int = Field(gt=0, le=100)
    max_days_of_cover: int = Field(gt=0)
    markdown_budget_cents: dict[str, int]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.auto_apply_pct >= self.max_daily_step_pct:
            raise ValueError("auto_apply_pct must be below max_daily_step_pct")
        if any(v < 0 for v in self.markdown_budget_cents.values()):
            raise ValueError("markdown budgets must be non-negative")
        return self


def load_policy(path: Path = DEFAULT_PATH) -> PolicyConfig:
    return PolicyConfig.model_validate(yaml.safe_load(path.read_text()))
