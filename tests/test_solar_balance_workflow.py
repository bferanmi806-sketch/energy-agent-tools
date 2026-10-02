import pytest

from energy_agent_tools import EnergyAgentTools
from energy_agent_tools.models import DataKind, EnergyResult

START = "2026-09-29T00:00:00Z"
END = "2026-09-29T03:00:00Z"


def _sources(session):
    refs = {}
    for capability, values, kind in (
        ("get_energy_consumption", [0.5, 0.75, 0.4], DataKind.METERED),
        ("get_generation", [0.1, 0.35, 0.5], DataKind.FORECAST),
    ):
        result = EnergyResult(
            data=[
                {
                    "timestamp": f"2026-09-29T0{i}:00:00Z",
                    "end": f"2026-09-29T0{i + 1}:00:00Z",
                    "value": value,
                }
                for i, value in enumerate(values)
            ],
            kind=kind,
            unit="kWh",
            source="synthetic-reference",
            timezone="UTC",
            resolution="1h",
            quantity_shape="interval",
        )
        refs[capability] = session.agent.workbench.persist(session.context, result)["artifact_id"]
    return refs


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
async def test_explicit_solar_balance_through_scoped_workflow(tmp_path, interface):
    async with EnergyAgentTools(tmp_path) as energy:
        session = energy.session("owner")
        refs = _sources(session)
        parameters = {
            "artifacts": refs,
            "start": START,
            "end": END,
            "solar_balance": {"consumption_basis": "total_load", "storage_mode": "none"},
        }
        if interface == "sdk":
            response = await session.skill("solar-consumption", parameters)
        else:
            response = await session.dispatch(
                "ENERGY_RUN_SKILL", {"skill_id": "solar-consumption", "parameters": parameters}
            )
        assert response["ok"], response
        result = response["evidence"][-1]["analysis"]["result"]
        assert result["kind"] == "calculated"
        assert result["unit"] == "kWh"
        expected = {
            "load_kwh": 1.65,
            "generation_kwh": 0.95,
            "self_consumption_kwh": 0.85,
            "estimated_import_kwh": 0.8,
            "estimated_export_kwh": 0.1,
        }
        for name, value in expected.items():
            assert result["data"]["summary"][name] == pytest.approx(value)
        provenance = str(result["provenance"])
        assert all(ref in provenance for ref in refs.values())
        assert "forecast" in provenance and "metered" in provenance


async def test_default_solar_workflow_keeps_alignment_without_assumptions(tmp_path):
    async with EnergyAgentTools(tmp_path) as energy:
        session = energy.session("owner")
        response = await session.skill("solar-consumption", {"artifacts": _sources(session)})
        assert response["ok"], response
        data = response["evidence"][-1]["analysis"]["result"]["data"]
        assert isinstance(data, list)
        assert "estimated_import_kwh" not in data[0]


@pytest.mark.parametrize(
    "skill,balance",
    [
        ("solar-consumption", {"consumption_basis": "grid_import", "storage_mode": "none"}),
        ("solar-consumption", {"consumption_basis": "total_load"}),
        ("solar-consumption", {"consumption_basis": "total_load", "storage_mode": "battery"}),
        ("yesterday-consumption", {"consumption_basis": "total_load", "storage_mode": "none"}),
    ],
)
async def test_solar_balance_requires_explicit_supported_semantics(tmp_path, skill, balance):
    async with EnergyAgentTools(tmp_path) as energy:
        response = await energy.session("owner").skill(skill, {"solar_balance": balance})
        assert not response["ok"], response
        assert response["error"]["code"] == "invalid_skill_parameters"


async def test_solar_balance_refuses_another_users_artifacts(tmp_path):
    async with EnergyAgentTools(tmp_path) as energy:
        refs = _sources(energy.session("other"))
        response = await energy.session("owner").skill(
            "solar-consumption",
            {
                "artifacts": refs,
                "solar_balance": {"consumption_basis": "total_load", "storage_mode": "none"},
            },
        )
        assert not response["ok"], response
        assert response["error"]["code"] == "artifact_not_found"


async def test_solar_balance_requires_full_requested_window(tmp_path):
    async with EnergyAgentTools(tmp_path) as energy:
        session = energy.session("owner")
        response = await session.skill(
            "solar-consumption",
            {
                "artifacts": _sources(session),
                "start": START,
                "end": "2026-09-29T04:00:00Z",
                "solar_balance": {"consumption_basis": "total_load", "storage_mode": "none"},
            },
        )
        assert not response["ok"], response
        analysis = response["evidence"][-1]["analysis"]
        assert analysis["error"]["code"] == "insufficient_coverage"
