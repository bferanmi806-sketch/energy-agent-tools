"""Shared setup for the small, offline reference projects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from energy_agent_tools import EnergyAgentTools

DATA_ROOT = Path(__file__).resolve().parent / "data"
USER_ID = "reference-user"


def site_config(
    *,
    site_id: str,
    name: str,
    timezone: str,
    latitude: float | None,
    longitude: float | None,
    assets: list[dict[str, Any]],
    bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "sites": [
            {
                "id": site_id,
                "user_id": USER_ID,
                "name": name,
                "timezone": timezone,
                "latitude": latitude,
                "longitude": longitude,
            }
        ],
        "assets": assets,
        "bindings": bindings or [],
    }


def make_tools(state_root: Path, config: dict[str, Any]) -> EnergyAgentTools:
    """Create a local SDK using only the checked-in CSV fixture directory."""
    return EnergyAgentTools(state_root, config=config, data_root=DATA_ROOT)


async def import_csv(
    session: Any,
    filename: str,
    *,
    kind: str,
    unit: str,
    timezone: str,
) -> tuple[str, Any]:
    """Import and persist one declared local source through the production CSV tool."""
    output = await session.execute(
        "CSV_READ_TIMESERIES",
        {"file": filename, "kind": kind, "unit": unit, "timezone": timezone},
        persist=True,
    )
    assert output["ok"], output
    artifact_id = output["result"]["data"]["artifact_id"]
    artifact = session.agent.workbench.read(session.context, artifact_id)
    assert artifact.source == "local-csv"
    assert artifact.kind.value == kind
    assert artifact.unit == unit
    assert {"file": filename, "declared_kind": kind} in artifact.provenance
    return artifact_id, artifact


async def execute_capability(
    session: Any,
    capability: str,
    arguments: dict[str, Any],
    *,
    asset_id: str,
    input_artifact_ids: list[str] | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    """Resolve a reviewed binding, then execute its exact tool with artifact lineage."""
    request: dict[str, Any] = {"arguments": arguments, "asset_id": asset_id}
    if unit is not None:
        request["unit"] = unit
    resolution = session.resolve(capability, **request)
    assert resolution["status"] == "resolved", resolution
    selected = resolution["selected"]
    output = await session.execute(
        selected["tool"],
        selected["arguments"],
        account_id=selected["account_id"],
        asset_id=selected["asset_id"],
        expected_kind=selected["kind"],
        expected_unit=selected["unit"],
        expected_resolution=selected["resolution"],
        expected_quantity_shape=selected["quantity_shape"],
        expected_arguments=selected["fixed_arguments"],
        input_artifacts=input_artifact_ids or [],
    )
    assert output["ok"], output
    result = output["result"]
    assert result["site_id"] == session.context.site_id
    assert result["asset_id"] == asset_id
    return result


def numeric_rows(artifact: Any, value_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Convert the CSV connector's string cells to numeric model inputs."""
    rows = []
    for source_row in artifact.data:
        row = dict(source_row)
        for field in value_fields:
            row[field] = float(row[field])
        rows.append(row)
    return rows


def source_summary(artifact_id: str, artifact: Any) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "kind": artifact.kind.value,
        "unit": artifact.unit,
        "source": artifact.source,
        "quality": artifact.quality,
        "provenance": artifact.provenance,
        "rows": len(artifact.data),
    }


def missing_consumption_evidence(session: Any, *, asset_id: str) -> dict[str, Any]:
    """Show that a fixture alone does not create a reviewed meter capability."""
    resolution = session.resolve(
        "get_energy_consumption",
        asset_id=asset_id,
        kind="metered",
        unit="kWh",
    )
    assert resolution["status"] == "unavailable", resolution
    assert resolution["selected"] is None
    assert any(
        "connection_required" in candidate["reasons"] for candidate in resolution["candidates"]
    )
    assert any(
        "measurement_or_argument_mapping_requires_review" in candidate["reasons"]
        for candidate in resolution["candidates"]
    )
    return {"status": resolution["status"], "capability": resolution["capability"]}
