from datetime import date
from pathlib import Path

import pytest

from debt_payoff_planner.core.store import Planner


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(Planner, "today", lambda self: date(2026, 10, 15))


@pytest.fixture
def planner(tmp_path):
    return Planner(tmp_path / "data", default_budget="600")


@pytest.fixture
def examples():
    return Path(__file__).parents[1] / "examples"
