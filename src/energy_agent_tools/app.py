from __future__ import annotations

import json
from pathlib import Path

from .capabilities import CapabilityBinding
from .connectors import (
    analytics,
    datasets,
    dss,
    engineering,
    extended,
    http,
    local,
    networks,
    weather_history,
)
from .models import Asset, ConnectedAccount, Json, Site
from .registry import Registry
from .runtime import EnergyAgent


def build_agent(
    root: Path, config: Json | None = None, *, data_root: Path | None = None
) -> EnergyAgent:
    if config is None and (root / "profile.json").is_file():
        config = load_config(root / "profile.json")
    config = config or {}
    allowed = {
        "user_id",
        "site_id",
        "accounts",
        "sites",
        "assets",
        "data_root",
        "mcp_servers",
        "bindings",
        "vault",
        "hosting",
        "plugins",
    }
    if set(config) - allowed:
        raise ValueError("Unknown configuration fields")
    if data_root is None and config.get("data_root"):
        data_root = Path(config["data_root"]).resolve()
    registry = Registry()
    http.register(registry)
    from .connectors import tesla_energy

    tesla_energy.register(registry)
    weather_history.register(registry)
    engineering.register(registry)
    analytics.register(registry)
    networks.register(registry)
    dss.register(registry)
    extended.register(registry, data_root=data_root)
    local.register(registry)
    from .connector_sdk import load_plugins

    load_plugins(registry, config.get("plugins", []))
    if data_root:
        local.register_csv(registry, data_root)
        datasets.register(registry, data_root)
    auth_store = None
    accounts = [ConnectedAccount.model_validate(a) for a in config.get("accounts", [])]
    if config.get("vault"):
        import os

        from .auth import AuthStore

        vault = config["vault"]
        if set(vault) - {"master_key_env", "master_key_file"}:
            raise ValueError("Unknown vault configuration field")
        if "master_key_env" in vault and "master_key_file" in vault:
            raise ValueError("Choose one vault key source")
        if "master_key_file" in vault:
            key_path = root / vault["master_key_file"]
            if key_path.is_symlink() or not key_path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Vault key file must be inside the private state directory")
            key = key_path.read_text().strip()
        else:
            key = os.environ.get(vault.get("master_key_env", "ENERGY_AUTH_MASTER_KEY"))
        if not key:
            raise ValueError("Vault master key is absent from the environment")
        auth_store = AuthStore(root / "vault", key.encode())
        for user_id in (
            {s["user_id"] for s in config.get("sites", [])}
            | {a.user_id for a in accounts}
            | {config.get("user_id", "local")}
        ):
            accounts.extend(
                account for account in auth_store.accounts(user_id) if account.workspace_id is None
            )
        accounts = list({a.id: a for a in accounts}.values())
    return EnergyAgent(
        registry,
        root,
        accounts=accounts,
        auth_store=auth_store,
        defer_unknown_bindings=bool(config.get("mcp_servers")),
        bindings=(
            []
            if config.get("mcp_servers")
            else [CapabilityBinding.model_validate(b) for b in config.get("bindings", [])]
        ),
        sites=[Site.model_validate(s) for s in config.get("sites", [])],
        assets=[Asset.model_validate(a) for a in config.get("assets", [])],
    )


async def configure_mcp(agent: EnergyAgent, config: Json) -> None:
    """Import reviewed remote schemas before validating their capability bindings."""
    if not config.get("mcp_servers"):
        return
    from .capabilities import CapabilityResolver
    from .connectors.mcp_bridge import import_mcp

    original = agent.registry
    staged = Registry()
    staged.toolkits = dict(original.toolkits)
    staged.tools = dict(original.tools)
    staged.handlers = dict(original.handlers)
    for spec in config["mcp_servers"]:
        await import_mcp(staged, **spec)
    agent.registry = staged
    try:
        resolver = CapabilityResolver(
            agent, [CapabilityBinding.model_validate(b) for b in config.get("bindings", [])]
        )
    finally:
        agent.registry = original
    original.toolkits = staged.toolkits
    original.tools = staged.tools
    original.handlers = staged.handlers
    original._indexed_count = -1
    agent.resolver = resolver


def load_config(path: Path | None) -> Json:
    if path is None:
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Configuration must be an object")
    return data
