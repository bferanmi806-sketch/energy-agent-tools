from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from energy_agent_tools import EnergyAgentTools

START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=8)
HISTORY_START = datetime(2026, 7, 1, tzinfo=UTC)
BILLING = {
    "standing_charge": {"amount_per_day": 0.3, "currency": "GBP", "taxable": False},
    "tax": {"rate": 0.05, "energy_taxable": True},
    "source": "explicit synthetic reviewed tariff components",
}


def _history():
    timestamp = HISTORY_START
    while timestamp < START:
        yield {
            "timestamp": timestamp.isoformat(),
            "end": (timestamp + timedelta(minutes=30)).isoformat(),
            "value": 0.5,
            "physical_meter": "false",
        }
        timestamp += timedelta(minutes=30)


def _configuration(data, provider):
    config = {
        "sites": [{"id": "home", "user_id": "owner", "name": "Synthetic home", "timezone": "UTC"}],
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
    if provider == "csv":
        config["bindings"].append(
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
        )
    else:
        config["accounts"] = [
            {
                "id": "meter",
                "user_id": "owner",
                "site_id": "home",
                "toolkit": "octopus-energy-account",
                "auth": {"scheme": "basic", "credential_env": "TEST_FORECAST_METER_KEY"},
                "settings": {"mpan": "SYNTHETIC", "serial_number": "SYNTHETIC"},
            }
        ]
    return config


@pytest.mark.parametrize("provider", ["csv", "octopus"])
@pytest.mark.parametrize("interface", ["sdk", "mcp"])
async def test_three_months_to_eight_day_forecast_bill(tmp_path, monkeypatch, provider, interface):
    monkeypatch.setenv("TEST_FORECAST_METER_KEY", "synthetic-fixture-credential")
    data = tmp_path / "data"
    data.mkdir()
    with (data / "history.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["timestamp", "end", "value", "physical_meter"])
        writer.writeheader()
        writer.writerows(_history())
    (data / "tariff.csv").write_text(
        "timestamp,end,value,physical_meter\n2026-09-01T00:00:00Z,2026-11-01T00:00:00Z,20,false\n"
    )
    requests = []

    def provider_request(request):
        requests.append(request)
        left = datetime.fromisoformat(request.url.params["period_from"].replace("Z", "+00:00"))
        right = datetime.fromisoformat(request.url.params["period_to"].replace("Z", "+00:00"))
        assert HISTORY_START <= left < right <= START
        assert right - left <= timedelta(days=30)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "interval_start": row["timestamp"],
                        "interval_end": row["end"],
                        "consumption": row["value"],
                    }
                    for row in _history()
                    if left <= datetime.fromisoformat(row["timestamp"]) < right
                ],
                "next": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_request)) as transport:
        async with EnergyAgentTools(
            tmp_path / "state", _configuration(data, provider), data_root=data
        ) as energy:
            if provider == "octopus":
                energy.agent.http = transport
            session = energy.session("owner", "home")
            parameters = {"start": START.isoformat(), "end": END.isoformat(), "billing": BILLING}
            if interface == "sdk":
                response = await session.skill("forecast-bill", parameters)
            else:
                response = await session.dispatch(
                    "ENERGY_RUN_SKILL", {"skill_id": "forecast-bill", "parameters": parameters}
                )
            assert response["ok"], response
            assert response["calculation_basis"] == "forecast_consumption"
            forecast_ref = response["forecast_artifact"]
            forecast = energy.agent.workbench.read(session.context, forecast_ref)
            assert forecast.kind.value == "forecast" and forecast.unit == "kWh"
            assert len(forecast.data["intervals"]) == 384
            assert forecast.data["summary"]["total_kwh"] == pytest.approx(192)
            analysis = response["evidence"][-1]["analysis"]
            result = energy.agent.workbench.read(
                session.context, analysis["result"]["data"]["artifact_id"]
            )
            assert result.kind.value == "calculated" and result.unit == "GBP"
            assert result.data["calculation_basis"] == "forecast_consumption"
            assert Decimal(str(result.data["estimate"]["total"])) == Decimal("42.72")
            sources = str(result.provenance)
            assert forecast_ref in sources and "metered" in sources and "forecast" in sources
            assert "synthetic-fixture-credential" not in str(response)
    assert len(requests) == (4 if provider == "octopus" else 0)
    if requests:
        assert requests[0].url.params["period_from"] == HISTORY_START.isoformat().replace(
            "+00:00", "Z"
        )
        assert requests[-1].url.params["period_to"] == START.isoformat().replace("+00:00", "Z")


async def test_forecast_workflow_refuses_foreign_history(tmp_path):
    from energy_agent_tools.models import DataKind, EnergyResult

    async with EnergyAgentTools(
        tmp_path, {"sites": [{"id": "home", "user_id": "owner", "name": "Home", "timezone": "UTC"}]}
    ) as energy:
        other = energy.session("other")
        ref = energy.agent.workbench.persist(
            other.context,
            EnergyResult(
                data=list(_history()),
                kind=DataKind.METERED,
                unit="kWh",
                source="synthetic",
                timezone="UTC",
                quantity_shape="interval",
            ),
        )["artifact_id"]
        result = await energy.session("owner", "home").skill(
            "consumption-forecast",
            {
                "start": START.isoformat(),
                "end": END.isoformat(),
                "artifacts": {"get_energy_consumption": ref},
            },
        )
        assert not result["ok"] and result["error"]["code"] == "artifact_not_found"


async def test_forecast_bill_requires_site_before_provider_calls(tmp_path):
    async with EnergyAgentTools(tmp_path) as energy:
        result = await energy.session("owner").skill("forecast-bill")
        assert not result["ok"] and result["error"]["code"] == "site_required"


async def test_unconfigured_public_tariff_is_not_an_available_forecast_source(tmp_path):
    from energy_agent_tools.capabilities import CapabilityRequest

    async with EnergyAgentTools(tmp_path) as energy:
        session = energy.session("owner")
        request = CapabilityRequest(
            capability="get_tariff",
            arguments={
                "start": START.isoformat(),
                "end": END.isoformat(),
            },
        )
        resolution = energy.agent.resolver.resolve(session.context, request)
        assert resolution["status"] == "unavailable"
        candidate = next(
            c for c in resolution["candidates"] if c["tool"] == "octopus_energy.get_tariffs"
        )
        assert "arguments_or_settings_required" in candidate["reasons"]
        request.arguments.update(product_code="SYNTHETIC", tariff_code="SYNTHETIC")
        configured = energy.agent.resolver.resolve(session.context, request)
        assert configured["selected"]["tool"] == "octopus_energy.get_tariffs"
