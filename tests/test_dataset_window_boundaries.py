from datetime import UTC, datetime

import pytest

from energy_agent_tools import EnergyAgentTools


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
@pytest.mark.parametrize("window_tool", ["DATASET_WINDOW", "WORKBENCH_WINDOW"])
async def test_dataset_window_boundaries_through_gateways(tmp_path, interface, window_tool):
    data = tmp_path / "data"
    data.mkdir()
    (data / "custom-intervals.csv").write_text(
        "observed_at,interval_finish,value\n"
        "2026-01-01T00:00:00Z,2026-01-01T01:00:00Z,2\n"
        "2026-01-01T01:00:00Z,2026-01-01T02:00:00Z,3\n"
        "2026-01-01T02:00:00Z,2026-01-01T03:00:00Z,4\n"
    )
    (data / "resolution-intervals.csv").write_text(
        "metered_at,value\n2026-01-01T00:00:00Z,5\n2026-01-01T01:00:00Z,6\n2026-01-01T02:00:00Z,7\n"
    )

    async with EnergyAgentTools(tmp_path / "state", data_root=data) as energy:
        session = energy.session("owner")

        async def call(tool, arguments, **options):
            if interface == "sdk":
                return await session.execute(tool, arguments, **options)
            response = await session.dispatch(
                "ENERGY_MULTI_EXECUTE_TOOL",
                {"calls": [{"tool": tool, "arguments": arguments, **options}]},
            )
            return response["results"][0]

        async def import_dataset(file, timestamp, resolution=None):
            arguments = {
                "file": file,
                "kind": "metered",
                "unit": "kWh",
                "timezone": "UTC",
                "quantity_shape": "interval",
                "timestamp": timestamp,
            }
            if resolution is not None:
                arguments["resolution"] = resolution
            imported = await call("DATASET_IMPORT_CSV", arguments)
            assert imported["ok"], imported
            return imported["result"]["data"]["dataset_id"]

        custom_ref = await import_dataset("custom-intervals.csv", "observed_at")

        async def window(artifact_id, start, end, timestamp, end_column=None):
            arguments = {
                "artifact_id": artifact_id,
                "start": start,
                "end": end,
                "timestamp": timestamp,
            }
            if end_column is not None:
                arguments["end_column"] = end_column
            return await call(window_tool, arguments, input_artifacts=[artifact_id])

        cut_start = await window(
            custom_ref,
            "2026-01-01T00:30:00Z",
            "2026-01-01T02:00:00Z",
            "observed_at",
            "interval_finish",
        )
        assert not cut_start["ok"] and cut_start["error"]["code"] == (
            "interval_boundary_mismatch"
        ), cut_start

        cut_end = await window(
            custom_ref,
            "2026-01-01T01:00:00Z",
            "2026-01-01T01:30:00Z",
            "observed_at",
            "interval_finish",
        )
        assert not cut_end["ok"] and cut_end["error"]["code"] == ("interval_boundary_mismatch"), (
            cut_end
        )

        missing_end = await window(
            custom_ref,
            "2026-01-01T00:00:00Z",
            "2026-01-01T01:00:00Z",
            "observed_at",
            "declared_finish",
        )
        assert not missing_end["ok"] and missing_end["error"]["code"] == ("column_not_found"), (
            missing_end
        )

        exact = await window(
            custom_ref,
            "2026-01-01T01:00:00Z",
            "2026-01-01T02:00:00Z",
            "observed_at",
            "interval_finish",
        )
        assert exact["ok"], exact
        selected = exact["result"]
        assert selected["kind"] == "metered"
        assert selected["data"] == [
            {
                "observed_at": "2026-01-01T01:00:00Z",
                "interval_finish": "2026-01-01T02:00:00Z",
                "value": "3",
            }
        ]
        assert datetime.fromisoformat(selected["time_end"].replace("Z", "+00:00")) == datetime(
            2026, 1, 1, 2, tzinfo=UTC
        )
        assert any(
            entry.get("operation") == "window" and entry.get("artifact_id") == custom_ref
            for entry in selected["provenance"]
        )
        assert any(
            entry.get("operation") == "partitioned_csv_import" for entry in selected["provenance"]
        )

        resolution_ref = await import_dataset(
            "resolution-intervals.csv", "metered_at", resolution="1h"
        )
        resolution_cut = await window(
            resolution_ref,
            "2026-01-01T00:30:00Z",
            "2026-01-01T02:00:00Z",
            "metered_at",
        )
        assert not resolution_cut["ok"] and resolution_cut["error"]["code"] == (
            "interval_boundary_mismatch"
        ), resolution_cut

        resolution_exact = await window(
            resolution_ref,
            "2026-01-01T01:00:00Z",
            "2026-01-01T02:00:00Z",
            "metered_at",
        )
        assert resolution_exact["ok"], resolution_exact
        inferred = resolution_exact["result"]
        assert inferred["kind"] == "metered"
        assert len(inferred["data"]) == 1
        assert any(entry.get("operation") == "window" for entry in inferred["provenance"])
