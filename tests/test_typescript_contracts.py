"""Catch Python/SDK schema drift using real production host responses."""

from __future__ import annotations

import importlib.util
import secrets
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_typescript_contracts_are_current():
    root = Path(__file__).resolve().parents[1]
    source = _load(root / "scripts/export_typescript_contracts.py", "sdk_contract_export")
    assert (root / "packages/typescript/src/contracts.ts").read_text() == source.render()


@pytest.mark.asyncio
async def test_sdk_response_schemas_accept_real_host_and_reject_invalid_energy_semantics(
    tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parents[1]
    source = _load(root / "scripts/export_typescript_contracts.py", "sdk_contract_export_live")
    fixture = _load(root / "packages/typescript/test/host.py", "sdk_contract_host")
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("ENERGY_AGENT_TEST_TOKEN", token)
    app = fixture.create_app(tmp_path / "state")
    validators = {
        name: Draft202012Validator(schema, format_checker=FormatChecker())
        for name, schema in source.schemas().items()
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            created = await client.post("/sessions", json={"site_id": "sdk-site"})
            validators["SessionResponse"].validate(created.json())
            prefix = "/sessions/" + created.json()["session_id"]
            for path, payload, name in (
                ("search", {"query": "fixture calculation"}, "SearchResponse"),
                ("skills/run", {"skill_id": "missing_fixture_skill"}, "WorkflowResponse"),
                (
                    "execute",
                    {"tool": "FIXTURE_CALCULATE", "arguments": {"base": 2, "multiplier": 3}},
                    "ExecutionResponse",
                ),
                (
                    "resolve",
                    {
                        "capability": "calculate_fixture_value",
                        "arguments": {"base": 2, "multiplier": 3},
                    },
                    "ResolutionResponse",
                ),
                ("capability", {"capability": "missing_fixture"}, "ExecutionResponse"),
                ("jobs", {"operation": "not-real"}, "JobResponse"),
            ):
                response = await client.post(prefix + "/" + path, json=payload)
                assert response.status_code == 200
                validators[name].validate(response.json())
            for path, name in (
                ("connections", "ConnectionsResponse"),
                ("toolkits", "ToolkitsResponse"),
                ("skills", "SkillsResponse"),
                ("artifacts", "ArtifactsResponse"),
            ):
                response = await client.get(prefix + "/" + path)
                validators[name].validate(response.json())
            bounded_skills = await client.post(
                prefix + "/skills", json={"query": "forecast", "limit": 1}
            )
            assert len(bounded_skills.json()["skills"]) == 1
            response = await client.post(
                prefix + "/execute",
                json={"tool": "FIXTURE_CALCULATE", "arguments": {"base": 2, "multiplier": 3}},
            )
            result = response.json()
            result["result"]["kind"] = "measured-forecast"
            assert not validators["ExecutionResponse"].is_valid(result)
            result["result"]["kind"] = "calculated"
            del result["result"]["unit"]
            assert not validators["ExecutionResponse"].is_valid(result)
    finally:
        await app.agent.close()


@pytest.mark.asyncio
async def test_rest_sdk_skill_route_composes_synthetic_forecast_and_bill(tmp_path):
    from datetime import UTC, datetime

    from energy_agent_tools import EnergyAgentTools
    from energy_agent_tools.hosting import Principal, create_host, token_digest
    from energy_agent_tools.models import Session
    from examples.reference_projects.forecast_workflow import (
        BILLING,
        FORECAST_END,
        FORECAST_START,
        SITE_ID,
        USER_ID,
        _binding,
        _write_history,
        _write_tariff,
    )

    data = tmp_path / "data"
    data.mkdir()
    _write_history(data / "history.csv")
    _write_tariff(data / "tariff.csv", datetime(2026, 11, 1, tzinfo=UTC))
    config = {
        "sites": [
            {"id": SITE_ID, "user_id": USER_ID, "name": "Synthetic SDK forecast", "timezone": "UTC"}
        ],
        "bindings": [
            _binding("get_energy_consumption", "history.csv", "metered", "kWh"),
            _binding("get_tariff", "tariff.csv", "forecast", "p/kWh"),
        ],
    }
    token = secrets.token_urlsafe(32)
    async with EnergyAgentTools(tmp_path / "state", config, data_root=data) as energy:
        app = create_host(
            energy.agent, {USER_ID: Principal(USER_ID, {SITE_ID}, token_digest(token))}
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            created = await client.post("/sessions", json={"site_id": SITE_ID})
            session_id = created.json()["session_id"]
            response = await client.post(
                f"/sessions/{session_id}/skills/run",
                json={
                    "skill_id": "forecast-bill",
                    "parameters": {
                        "start": FORECAST_START.isoformat(),
                        "end": FORECAST_END.isoformat(),
                        "history_end": FORECAST_START.isoformat(),
                        "context_mode": "explicit",
                        "billing": BILLING,
                    },
                },
            )
            assert response.status_code == 200
            result = response.json()
            assert result["ok"], result
            context = Session(id=session_id, user_id=USER_ID, site_id=SITE_ID)
            forecast = energy.agent.workbench.read(context, result["forecast_artifact"])
            assert forecast.kind.value == "forecast"
            assert forecast.data["summary"]["total_kwh"] == 192
            analysis = next(item["analysis"] for item in result["evidence"] if "analysis" in item)
            bill = energy.agent.workbench.read(context, analysis["result"]["data"]["artifact_id"])
            assert bill.kind.value == "calculated"
            assert bill.data["calculation_basis"] == "forecast_consumption"
            assert float(bill.data["estimate"]["total"]) == 42.72
            invalid = await client.post(
                f"/sessions/{session_id}/skills/run", json={"skill_id": 1, "parameters": {}}
            )
            assert invalid.status_code == 400
