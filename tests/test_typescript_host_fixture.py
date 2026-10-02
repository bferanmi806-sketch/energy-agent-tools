from __future__ import annotations

import importlib.util
import secrets
from pathlib import Path
from types import ModuleType

import httpx
import pytest


def _fixture_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "packages" / "typescript" / "test" / "host.py"
    spec = importlib.util.spec_from_file_location("typescript_acceptance_host", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_typescript_acceptance_host_uses_authenticated_production_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = secrets.token_urlsafe(32)
    foreign_token = secrets.token_urlsafe(32)
    wrong_token = secrets.token_urlsafe(32)
    monkeypatch.setenv("ENERGY_AGENT_TEST_TOKEN", token)
    monkeypatch.setenv("ENERGY_AGENT_TEST_FOREIGN_TOKEN", foreign_token)

    app = _fixture_module().create_app(tmp_path / "state")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8765"
        ) as client:
            unauthorized = await client.post(
                "/sessions",
                headers={"Authorization": f"Bearer {wrong_token}"},
                json={"site_id": "sdk-site"},
            )
            assert unauthorized.status_code == 401
            assert wrong_token not in unauthorized.text

            headers = {"Authorization": f"Bearer {token}"}
            created = await client.post("/sessions", headers=headers, json={"site_id": "sdk-site"})
            assert created.status_code == 200
            session_id = created.json()["session_id"]

            foreign = await client.post(
                "/sessions",
                headers={"Authorization": f"Bearer {foreign_token}"},
                json={"site_id": "sdk-foreign-site"},
            )
            assert foreign.status_code == 200
            foreign_session_id = foreign.json()["session_id"]

            search = await client.post(
                f"/sessions/{session_id}/search",
                headers=headers,
                json={"query": "fixture calculate value"},
            )
            assert search.status_code == 200
            assert search.json()["tools"][0]["name"] == "FIXTURE_CALCULATE"

            executed = await client.post(
                f"/sessions/{session_id}/execute",
                headers=headers,
                json={
                    "tool": "FIXTURE_CALCULATE",
                    "arguments": {"base": 17, "multiplier": 3},
                },
            )
            assert executed.status_code == 200
            assert executed.json()["ok"] is True
            assert executed.json()["result"]["data"] == {"value": 51}

            capability_request = {
                "capability": "calculate_fixture_value",
                "arguments": {"base": 17, "multiplier": 3},
            }
            resolved = await client.post(
                f"/sessions/{session_id}/resolve", headers=headers, json=capability_request
            )
            assert resolved.status_code == 200
            assert resolved.json()["status"] == "resolved"
            assert resolved.json()["selected"]["tool"] == "FIXTURE_CALCULATE"

            capability = await client.post(
                f"/sessions/{session_id}/capability",
                headers=headers,
                json={**capability_request, "persist": True},
            )
            assert capability.status_code == 200
            assert capability.json()["ok"] is True
            artifact_id = capability.json()["result"]["data"]["artifact_id"]

            connections = await client.get(f"/sessions/{session_id}/connections", headers=headers)
            assert connections.status_code == 200
            assert connections.json()["connections"][0]["id"] == "fixture-local-connection"

            skills = await client.get(f"/sessions/{session_id}/skills", headers=headers)
            assert skills.status_code == 200
            assert skills.json()["skills"]
            matching_skills = await client.post(
                f"/sessions/{session_id}/skills",
                headers=headers,
                json={"query": "electricity"},
            )
            assert matching_skills.status_code == 200
            assert matching_skills.json()["skills"]

            artifacts = await client.get(f"/sessions/{session_id}/artifacts", headers=headers)
            assert artifacts.status_code == 200
            assert artifacts.json()["artifacts"][0]["artifact_id"] == artifact_id
            removed_artifact = await client.delete(
                f"/sessions/{session_id}/artifacts/{artifact_id}", headers=headers
            )
            assert removed_artifact.status_code == 200
            empty_artifacts = await client.get(f"/sessions/{session_id}/artifacts", headers=headers)
            assert empty_artifacts.json()["artifacts"] == []

            owner_reading_foreign = await client.post(
                f"/sessions/{foreign_session_id}/search",
                headers=headers,
                json={"query": "fixture calculate"},
            )
            assert owner_reading_foreign.status_code == 404
            foreign_reading_owner = await client.post(
                f"/sessions/{session_id}/search",
                headers={"Authorization": f"Bearer {foreign_token}"},
                json={"query": "fixture calculate"},
            )
            assert foreign_reading_owner.status_code == 404

            deleted = await client.delete(f"/sessions/{session_id}", headers=headers)
            assert deleted.status_code == 200
            missing = await client.get(f"/sessions/{session_id}/artifacts", headers=headers)
            assert missing.status_code == 404
    finally:
        await app.agent.close()
