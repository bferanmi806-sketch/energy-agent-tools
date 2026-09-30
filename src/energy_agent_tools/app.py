from __future__ import annotations

import json
from pathlib import Path

from .connectors import engineering, http, local
from .models import Asset, ConnectedAccount, Json, Site
from .registry import Registry
from .runtime import EnergyAgent


def build_agent(
    root: Path, config: Json | None = None, *, data_root: Path | None = None
) -> EnergyAgent:
    config = config or {}
    allowed = {"user_id", "site_id", "accounts", "sites", "assets", "data_root", "mcp_servers"}
    if set(config) - allowed:
        raise ValueError("Unknown configuration fields")
    registry = Registry()
    http.register(registry)
    engineering.register(registry)
    local.register(registry)
    if data_root:
        local.register_csv(registry, data_root)
    return EnergyAgent(
        registry,
        root,
        accounts=[ConnectedAccount.model_validate(a) for a in config.get("accounts", [])],
        sites=[Site.model_validate(s) for s in config.get("sites", [])],
        assets=[Asset.model_validate(a) for a in config.get("assets", [])],
    )


def load_config(path: Path | None) -> Json:
    if path is None:
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Configuration must be an object")
    return data
