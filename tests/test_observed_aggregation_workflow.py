from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from energy_agent_tools import EnergyAgentTools


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
@pytest.mark.parametrize("source", ["artifact", "provider"])
async def test_one_minute_three_month_history_forecasts_and_bills(tmp_path, interface, source):
    data = tmp_path / "data"
    data.mkdir()
    left = datetime(2026, 7, 1, tzinfo=UTC)
    right = datetime(2026, 10, 1, tzinfo=UTC)
    horizon = right + timedelta(days=8)
    with (data / "history.csv").open("w") as stream:
        stream.write("begins,finishes,energy_wh\n")
        current = left
        while current < right:
            edge = current + timedelta(minutes=1)
            stream.write(f"{current.isoformat()},{edge.isoformat()},50\n")
            current = edge
    (data / "tariff.csv").write_text(
        "timestamp,end,value\n2026-09-01T00:00:00Z,2026-11-01T00:00:00Z,20\n"
    )
    import_args = {
        "file": "history.csv",
        "kind": "metered",
        "unit": "Wh",
        "timezone": "UTC",
        "quantity_shape": "interval",
        "resolution": "1min",
        "timestamp": "begins",
    }
    config = {
        "sites": [{"id": "site", "user_id": "owner", "name": "Synthetic site", "timezone": "UTC"}],
        "bindings": [
            {
                "capability": "get_tariff",
                "tool": "CSV_READ_TIMESERIES",
                "reviewed": True,
                "kind": "forecast",
                "unit": "p/kWh",
                "quantity_shape": "interval",
                "fixed_arguments": {
                    "file": "tariff.csv",
                    "window_mode": "overlap",
                    "kind": "forecast",
                    "unit": "p/kWh",
                    "timezone": "UTC",
                    "quantity_shape": "interval",
                },
            }
        ],
    }
    if source == "provider":
        config["bindings"].append(
            {
                "capability": "get_energy_consumption",
                "tool": "DATASET_IMPORT_CSV",
                "reviewed": True,
                "kind": "metered",
                "unit": "Wh",
                "quantity_shape": "interval",
                "fixed_arguments": import_args,
            }
        )
    async with EnergyAgentTools(tmp_path / "state", config, data_root=data) as energy:
        session = energy.session("owner", "site")
        parameters = {
            "start": right.isoformat(),
            "end": horizon.isoformat(),
            "forecast": {"timestamp": "begins", "end_column": "finishes", "column": "energy_wh"},
            "billing": {
                "standing_charge": {"amount_per_day": 0.3, "currency": "GBP", "taxable": False},
                "tax": {"rate": 0.05, "energy_taxable": True},
                "source": "synthetic declared components",
            },
        }
        if source == "artifact":
            imported = await session.execute("DATASET_IMPORT_CSV", import_args)
            assert imported["ok"], imported
            ref = imported["result"]["data"]["dataset_id"]
            assert energy.agent.workbench.read(session.context, ref).data["rows"] == 132480
            parameters["artifacts"] = {"get_energy_consumption": ref}
        if interface == "sdk":
            response = await session.skill("forecast-bill", parameters)
        else:
            response = await session.dispatch(
                "ENERGY_RUN_SKILL", {"skill_id": "forecast-bill", "parameters": parameters}
            )
        assert response["ok"], response
        forecast = energy.agent.workbench.read(session.context, response["forecast_artifact"])
        assert forecast.kind.value == "forecast"
        assert forecast.data["summary"]["total_kwh"] == pytest.approx(576)
        analysis = response["evidence"][-1]["analysis"]
        bill = energy.agent.workbench.read(
            session.context, analysis["result"]["data"]["artifact_id"]
        )
        assert (
            bill.kind.value == "calculated"
            and bill.data["calculation_basis"] == "forecast_consumption"
        )
        assert Decimal(str(bill.data["estimate"]["total"])) == Decimal("123.36")
        assert "aggregate_observed_interval_energy" in str(bill.provenance)
        assert "metered" in str(bill.provenance)


@pytest.mark.parametrize(
    "kind,shape,unit,code",
    [
        ("metered", "counter", "kWh", "observed_interval_energy_required"),
        ("forecast", "interval", "kWh", "observed_interval_energy_required"),
        ("metered", "interval", "kW", "unknown_unit"),
    ],
)
async def test_observed_aggregation_refuses_other_semantics(tmp_path, kind, shape, unit, code):
    from energy_agent_tools.models import DataKind, EnergyResult

    async with EnergyAgentTools(tmp_path) as energy:
        session = energy.session("owner")
        ref = energy.agent.workbench.persist(
            session.context,
            EnergyResult(
                data=[
                    {"timestamp": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:30:00Z", "value": 2}
                ],
                kind=DataKind(kind),
                unit=unit,
                source="synthetic",
                quantity_shape=shape,
            ),
        )["artifact_id"]
        response = await session.execute(
            "WORKBENCH_AGGREGATE_ENERGY",
            {
                "artifact_id": ref,
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:30:00Z",
            },
        )
        assert not response["ok"] and response["error"]["code"] == code, response
