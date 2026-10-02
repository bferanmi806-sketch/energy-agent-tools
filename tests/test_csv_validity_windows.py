import pytest

from energy_agent_tools import EnergyAgentTools


@pytest.mark.parametrize("mode,expected", [("starts", []), ("overlap", ["20"])])
async def test_csv_explicit_validity_selection_preserves_covering_rates(tmp_path, mode, expected):
    data = tmp_path / "data"
    data.mkdir()
    (data / "rate.csv").write_text(
        "timestamp,end,value\n2026-09-01T00:00:00Z,2026-11-01T00:00:00Z,20\n"
    )
    async with EnergyAgentTools(tmp_path / "state", data_root=data) as energy:
        output = await energy.session("owner").execute(
            "CSV_READ_TIMESERIES",
            {
                "file": "rate.csv",
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-09T00:00:00Z",
                "kind": "forecast",
                "timezone": "UTC",
                "unit": "p/kWh",
                "quantity_shape": "interval",
                "window_mode": mode,
            },
        )
        assert output["ok"], output
        assert [row["value"] for row in output["result"]["data"]] == expected


@pytest.mark.parametrize(
    "finish,code", [("", "interval_end_required"), ("2026-09-01T00:00:00", "invalid_interval")]
)
async def test_csv_overlap_selection_requires_explicit_valid_ends(tmp_path, finish, code):
    data = tmp_path / "data"
    data.mkdir()
    (data / "rate.csv").write_text(f"timestamp,end,value\n2026-09-01T00:00:00Z,{finish},20\n")
    async with EnergyAgentTools(tmp_path / "state", data_root=data) as energy:
        output = await energy.session("owner").execute(
            "CSV_READ_TIMESERIES",
            {
                "file": "rate.csv",
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-09T00:00:00Z",
                "kind": "forecast",
                "timezone": "UTC",
                "unit": "p/kWh",
                "window_mode": "overlap",
            },
        )
        assert not output["ok"] and output["error"]["code"] == code
