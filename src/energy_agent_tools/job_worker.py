"""Private worker entry point for :mod:`energy_agent_tools.jobs`.

The command line is an internal protocol.  It accepts only manager-generated
paths and a UUID-shaped job identifier, reads a strict JSON envelope, and maps
the operation to a fixed local engineering handler.  Provider connectors,
plugins, arbitrary imports, user executables, and network tools are never
loaded in this process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from .connectors import engineering
from .jobs import SimulationOperation
from .models import EnergyError
from .registry import Registry
from .runtime import EnergyAgent

_MAX_INPUT_BYTES = 4_000_000
_MAX_OUTPUT_BYTES = 8_000_000
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_TOOLS: dict[SimulationOperation, str] = {
    SimulationOperation.HEAT_LOSS: "engineering.calculate_heat_loss",
    SimulationOperation.POWER_FLOW: "engineering.run_power_flow",
    SimulationOperation.BATTERY: "engineering.schedule_battery_charging",
    SimulationOperation.SOLAR: "engineering.estimate_solar_generation",
}


def _private_mode(path: Path, expected: int) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != expected:
        raise ValueError(f"private worker path has mode {oct(mode)}")


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="energy-agent-tools-job-worker")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--state-dir", required=True)
    parsed = parser.parse_args(argv)
    if not _JOB_ID.fullmatch(parsed.job_id):
        parser.error("--job-id must be a manager-generated identifier")
    return parsed


def _read_payload(input_path: Path) -> tuple[SimulationOperation, dict[str, Any]]:
    if input_path.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError("worker input exceeds the hard input limit")
    _private_mode(input_path, stat.S_IRUSR | stat.S_IWUSR)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "operation",
        "arguments",
    }:
        raise ValueError("worker input envelope is invalid")
    if payload["schema_version"] != 1 or not isinstance(payload["operation"], str):
        raise ValueError("worker input schema version is invalid")
    try:
        operation = SimulationOperation(payload["operation"])
    except ValueError as exc:
        raise ValueError("worker operation is not allowlisted") from exc
    arguments = payload["arguments"]
    if not isinstance(arguments, dict):
        raise ValueError("worker arguments must be an object")
    return operation, arguments


def _write_result(output_path: Path, result: dict[str, Any]) -> None:
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_OUTPUT_BYTES:
        result = {
            "ok": False,
            "error": {
                "code": "output_limit",
                "message": "The numerical worker result exceeds its hard output limit.",
            },
        }
        encoded = json.dumps(result, separators=(",", ":")).encode("utf-8")
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_bytes(encoded)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, output_path)
    os.chmod(output_path, stat.S_IRUSR | stat.S_IWUSR)


async def _execute(
    state_dir: Path, operation: SimulationOperation, arguments: dict[str, Any]
) -> dict[str, Any]:
    # Build the regular EnergyAgent lifecycle with a job-local root, but only
    # register the two local numerical toolkits.  In particular, HTTP, local
    # CSV, MCP, and plugin connectors are never imported by this worker.
    registry = Registry()
    engineering.register(registry)
    agent = EnergyAgent(registry, state_dir / "agent")
    try:
        session = agent.session("job-worker")
        response = await agent.execute(session, _TOOLS[operation], arguments)
        data = response.get("result", {}).get("data", {})
        if isinstance(data, dict) and "artifact_id" in data:
            response["result"] = agent.workbench.read(session, data["artifact_id"]).model_dump(
                mode="json"
            )
        return response
    finally:
        await agent.close()


async def _main(parsed: argparse.Namespace) -> int:
    input_path = Path(parsed.input)
    output_path = Path(parsed.output)
    state_dir = Path(parsed.state_dir)
    if not state_dir.exists() or not state_dir.is_dir():
        raise ValueError("worker state directory does not exist")
    _private_mode(state_dir, stat.S_IRWXU)
    if not _inside(input_path, state_dir.parent) or not _inside(output_path, state_dir.parent):
        raise ValueError("worker paths must stay inside the manager-generated job directory")
    if input_path.parent != output_path.parent:
        raise ValueError("worker input and output must share the job directory")
    operation, arguments = _read_payload(input_path)
    result = await _execute(state_dir, operation, arguments)
    if not isinstance(result, dict):
        result = {
            "ok": False,
            "error": {
                "code": "invalid_result",
                "message": "The numerical tool returned an invalid result.",
            },
        }
    _write_result(output_path, result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parsed = _parse_args(argv)
    try:
        return asyncio.run(_main(parsed))
    except (EnergyError, OSError, ValueError, json.JSONDecodeError):
        # The manager intentionally receives only a stable error marker.  Raw
        # exception text can contain local paths or dependency details.
        try:
            output_path = Path(parsed.output)
            state_dir = Path(parsed.state_dir)
            if _inside(output_path, state_dir.parent):
                _write_result(
                    output_path,
                    {
                        "ok": False,
                        "error": {
                            "code": "worker_input_error",
                            "message": "The numerical worker rejected its private input.",
                        },
                    },
                )
        except Exception:
            pass
        return 2
    except Exception:
        try:
            output_path = Path(parsed.output)
            state_dir = Path(parsed.state_dir)
            if _inside(output_path, state_dir.parent):
                _write_result(
                    output_path,
                    {
                        "ok": False,
                        "error": {
                            "code": "worker_failed",
                            "message": "The numerical worker failed before producing a result.",
                        },
                    },
                )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
