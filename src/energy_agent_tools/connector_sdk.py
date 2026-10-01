"""Connector validation and operator-selected plugin loading."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

from jsonschema import Draft202012Validator

from .models import Json
from .registry import Registry


def validate_registry(registry: Registry) -> Json:
    errors = []
    for name, tool in registry.tools.items():
        Draft202012Validator.check_schema(tool.input_schema)
        if name not in registry.handlers or tool.toolkit not in registry.toolkits:
            errors.append({"tool": name, "error": "missing_handler_or_toolkit"})
        if not tool.version or not tool.capabilities or not tool.actions:
            errors.append({"tool": name, "error": "incomplete_contract"})
        if tool.input_schema.get("type") != "object":
            errors.append({"tool": name, "error": "arguments_must_be_object"})
    return {
        "ok": not errors,
        "tools": len(registry.tools),
        "toolkits": len(registry.toolkits),
        "errors": errors,
        "qualification": "Structural validation only; connector tests and provider verification are required.",
    }


def load_plugins(registry: Registry, names: list[str]) -> None:
    available = {
        entry.name: entry
        for entry in importlib.metadata.entry_points(group="energy_agent_tools.connectors")
    }
    for name in names:
        if name not in available:
            raise ValueError("Configured connector plugin is not installed")
        available[name].load()(registry)


def scaffold(path: Path, toolkit: str) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", toolkit):
        raise ValueError("Toolkit identifier must use lowercase letters, numbers and underscores")
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"{toolkit}.py"
    source = """from energy_agent_tools.models import Action, DataKind, EnergyResult, Tool, Toolkit, schema


def register(registry):
    registry.add_toolkit(Toolkit(id="TOOLKIT", name="TOOLKIT", description="Reviewed local connector", runtime="native", status="experimental", version="1.0.0"))
    async def calculate(arguments, context):
        return EnergyResult(data={"energy_kwh": arguments["power_kw"] * arguments["hours"]}, kind=DataKind.CALCULATED, unit="kWh", source="TOOLKIT", assumptions=["Constant average power over the supplied duration."])
    registry.add(Tool(name="TOOLKIT.calculate_energy", toolkit="TOOLKIT", description="Calculate energy from explicit average power and duration", input_schema=schema({"power_kw": {"type": "number", "minimum": 0}, "hours": {"type": "number", "exclusiveMinimum": 0}}, ["power_kw", "hours"]), capabilities=["calculate_energy"], actions={Action.CALCULATE}, result_kind=DataKind.CALCULATED, result_unit="kWh"), calculate)
""".replace("TOOLKIT", toolkit)
    with target.open("x") as stream:
        stream.write(source)
    return target
