"""Sync the scenario catalogue to the Langfuse dataset: one item per scenario, keyed by its ID.

An item is written only when its scenario changed, so the dataset's version history moves only
when a scenario does, and a CI run pinned to a timestamp sees exactly the scenarios of that time.
Scenarios without tags are local demos and stay out. Removing a scenario file leaves its item.

    uv run --env-file deploy/.env python -m experiments.sync_dataset
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from pricing_watch.sim.scenario import ScenarioConfig, load_scenario

DATASET = "pricing-watch-scenarios"
SCENARIOS = Path(__file__).parents[1] / "scenarios"


def catalogue() -> list[ScenarioConfig]:
    configs = [load_scenario(path) for path in sorted(SCENARIOS.glob("*.yaml"))]
    return [c for c in configs if c.tags]


def item_id(scenario_id: str) -> str:
    return f"scenario:{scenario_id}"


def _item(config: ScenarioConfig) -> dict[str, Any]:
    return {
        "input": config.model_dump(mode="json", exclude={"expected_output", "tags"}),
        "expected_output": config.expected_output.model_dump(mode="json"),
        "metadata": {"tags": config.tags},
    }


def sync(client: Any, configs: list[ScenarioConfig]) -> int:
    """Upsert each scenario's item if it changed; returns how many were written."""
    client.create_dataset(name=DATASET, description="pricing-watch scenario catalogue")
    stored = {i.id: i for i in client.get_dataset(DATASET).items}
    written = 0
    for config in configs:
        want, have = _item(config), stored.get(item_id(config.id))
        if have is not None and all(getattr(have, k) == v for k, v in want.items()):
            continue
        client.create_dataset_item(dataset_name=DATASET, id=item_id(config.id), **want)
        written += 1
    return written


def pinned(client: Any, at: datetime) -> Any:
    """The dataset as it stood at `at`, so a CI run doesn't move when a scenario is edited."""
    return client.get_dataset(DATASET, version=at)


if __name__ == "__main__":
    from pricing_watch.tracing.langfuse_tracer import langfuse_client

    lf = langfuse_client()
    configs = catalogue()
    written = sync(lf, configs)
    lf.flush()
    unchanged = len(configs) - written
    print(f"{DATASET}: {len(configs)} scenarios, {written} written, {unchanged} unchanged")
