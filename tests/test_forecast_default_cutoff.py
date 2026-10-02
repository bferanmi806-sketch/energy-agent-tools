from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta

import pytest

from energy_agent_tools import EnergyAgentTools

HISTORY_START = datetime(2026, 7, 2, tzinfo=UTC)
HISTORY_END = datetime(2026, 10, 2, tzinfo=UTC)
FROZEN_NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
FORECAST_START = HISTORY_END + timedelta(days=1)
FORECAST_END = FORECAST_START + timedelta(days=8)
INTERVAL = timedelta(minutes=30)
HISTORY_ROWS = int((HISTORY_END - HISTORY_START) / INTERVAL)


def _write_history(path):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["timestamp", "end", "value"])
        writer.writeheader()
        timestamp = HISTORY_START
        while timestamp < HISTORY_END:
            writer.writerow(
                {
                    "timestamp": timestamp.isoformat(),
                    "end": (timestamp + INTERVAL).isoformat(),
                    "value": "0.5",
                }
            )
            timestamp += INTERVAL


def _configuration():
    return {
        "sites": [{"id": "home", "user_id": "owner", "name": "Synthetic home", "timezone": "UTC"}],
        "bindings": [
            {
                "capability": "get_energy_consumption",
                "tool": "CSV_READ_TIMESERIES",
                "reviewed": True,
                "kind": "metered",
                "unit": "kWh",
                "quantity_shape": "interval",
                "fixed_arguments": {
                    "file": "history.csv",
                    "kind": "metered",
                    "unit": "kWh",
                    "timezone": "UTC",
                    "quantity_shape": "interval",
                    "resolution": "30min",
                },
            }
        ],
    }


def _record_csv_reads(energy, monkeypatch):
    requests = []
    original_execute = energy.agent.execute

    async def recording_execute(session, name, arguments, *args, **kwargs):
        if name == "CSV_READ_TIMESERIES":
            requests.append(dict(arguments))
        return await original_execute(session, name, arguments, *args, **kwargs)

    monkeypatch.setattr(energy.agent, "execute", recording_execute)
    return requests


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


async def _run_workflow(session, interface, parameters):
    if interface == "sdk":
        return await session.skill("consumption-forecast", parameters)
    return await session.dispatch(
        "ENERGY_RUN_SKILL",
        {"skill_id": "consumption-forecast", "parameters": parameters},
    )


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
async def test_default_forecast_uses_complete_metered_history_through_yesterday(
    tmp_path, monkeypatch, interface
):
    data = tmp_path / "data"
    data.mkdir()
    _write_history(data / "history.csv")

    async with EnergyAgentTools(tmp_path / "state", _configuration(), data_root=data) as energy:
        energy.agent.calendar_clock = lambda: FROZEN_NOW
        session = energy.session("owner", "home")
        csv_requests = _record_csv_reads(energy, monkeypatch)
        response = await _run_workflow(session, interface, {"context_mode": "explicit"})

        assert response["ok"], response
        assert _instant(response["window"]["history_start"]) == HISTORY_START
        assert _instant(response["window"]["history_end"]) == HISTORY_END
        assert _instant(response["window"]["start"]) == FORECAST_START
        assert _instant(response["window"]["end"]) == FORECAST_END

        forecast = energy.agent.workbench.read(session.context, response["forecast_artifact"])
        assert forecast.kind.value == "forecast"
        assert forecast.unit == "kWh"
        assert len(forecast.data["intervals"]) == 8 * 48
        assert forecast.data["summary"]["total_kwh"] == pytest.approx(192)
        model = forecast.data["model"]
        assert _instant(model["history_end"]) == HISTORY_END
        assert _instant(model["forecast_start"]) == FORECAST_START
        assert model["history_to_forecast_gap_seconds"] == 24 * 60 * 60
        assert model["final_training_rows"] == HISTORY_ROWS
        assert _instant(model["final_training_start"]) == HISTORY_START
        assert _instant(model["final_training_end"]) == HISTORY_END
        assert forecast.provenance[0]["inputs"][0]["kind"] == "metered"

        assert csv_requests
        assert all(
            HISTORY_START <= _instant(request["start"]) < _instant(request["end"]) <= HISTORY_END
            for request in csv_requests
        )
        assert min(_instant(request["start"]) for request in csv_requests) == HISTORY_START
        assert max(_instant(request["end"]) for request in csv_requests) == HISTORY_END


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
async def test_explicit_future_history_cutoff_fails_before_meter_read(
    tmp_path, monkeypatch, interface
):
    data = tmp_path / "data"
    data.mkdir()
    _write_history(data / "history.csv")

    async with EnergyAgentTools(tmp_path / "state", _configuration(), data_root=data) as energy:
        energy.agent.calendar_clock = lambda: FROZEN_NOW
        session = energy.session("owner", "home")
        csv_requests = _record_csv_reads(energy, monkeypatch)
        response = await _run_workflow(
            session,
            interface,
            {
                "context_mode": "explicit",
                "history_end": (HISTORY_END + timedelta(days=2)).isoformat(),
            },
        )

        assert not response["ok"]
        assert response["error"]["code"] == "invalid_history_end"
        assert csv_requests == []
