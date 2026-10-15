"""Local YAML settings and environment precedence."""

import os
from pathlib import Path

import yaml

from .core.store import Planner


def make_planner(data_dir: str | None = None, config: str | None = None) -> Planner:
    settings = {}
    source = config or os.getenv("DEBT_CONFIG")
    if source:
        path = Path(source).expanduser().resolve()
        settings = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(settings, dict) or set(settings) - {
            "data_dir",
            "timezone",
            "currency",
            "default_budget",
        }:
            raise ValueError("config must contain supported planner settings only")
        if settings.get("data_dir") and not Path(settings["data_dir"]).expanduser().is_absolute():
            settings["data_dir"] = str(path.parent / settings["data_dir"])
    settings["data_dir"] = (
        data_dir or os.getenv("DEBT_DATA_DIR") or settings.get("data_dir", "data")
    )
    return Planner(**settings)
