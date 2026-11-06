import importlib

import pytest

SUBPACKAGES = ["sim", "agent", "policy", "approvals", "tracing", "evals", "api"]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    importlib.import_module(f"pricing_watch.{name}")
