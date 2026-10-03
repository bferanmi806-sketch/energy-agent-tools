from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from energy_agent_tools.app import build_agent
from energy_agent_tools.hosting import Principal, create_host, token_digest
from energy_agent_tools.models import EnergyError


@pytest.mark.asyncio
async def test_hosted_sessions_cannot_read_operator_files_but_local_sessions_can(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "operator-data"
    data_root.mkdir()
    (data_root / "fictional.csv").write_text(
        "timestamp,value\n2026-10-01T00:00:00Z,1042.5\n2026-10-01T00:30:00Z,2043.5\n",
        encoding="utf-8",
    )
    with sqlite3.connect(data_root / "fictional.sqlite3") as db:
        db.execute("CREATE TABLE readings (timestamp TEXT, value REAL)")
        db.executemany(
            "INSERT INTO readings VALUES (?, ?)",
            [
                ("2026-10-01T00:00:00Z", 3054.25),
                ("2026-10-01T00:30:00Z", 4055.25),
            ],
        )

    sites = [
        {"id": "tenant-site", "user_id": "tenant", "name": "Fictional Home", "timezone": "UTC"},
        {"id": "other-site", "user_id": "other", "name": "Other Home", "timezone": "UTC"},
    ]
    agent = build_agent(tmp_path / "state", {"sites": sites}, data_root=data_root)
    fake_provider = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "hourly": {"time": ["2026-10-03T00:00"], "temperature_2m": [13.5]},
                    "hourly_units": {"temperature_2m": "°C"},
                },
                request=request,
            )
        )
    )
    await agent.http.aclose()
    agent.http = fake_provider
    agent._owns_http = False

    host = create_host(
        agent,
        {
            "tenant": Principal("tenant", {"tenant-site"}, token_digest("tenant-token")),
            "other": Principal("other", {"other-site"}, token_digest("other-token")),
        },
    )
    csv_arguments = {
        "file": "fictional.csv",
        "kind": "metered",
        "unit": "kWh",
        "timezone": "UTC",
    }
    dataset_arguments = {**csv_arguments, "quantity_shape": "interval"}
    sqlite_arguments = {
        "path": "fictional.sqlite3",
        "table": "readings",
        "timestamp_column": "timestamp",
        "value_column": "value",
        "unit": "kWh",
        "kind": "metered",
        "timezone": "UTC",
    }
    protected_tools = [
        ("CSV_READ_TIMESERIES", csv_arguments, ["1042.5", "2043.5"], ["import_timeseries"]),
        ("DATASET_IMPORT_CSV", dataset_arguments, ["1042.5", "2043.5"], ["large_timeseries"]),
        ("sqlite.read_timeseries", sqlite_arguments, ["3054.25", "4055.25"], ["sqlite_timeseries"]),
    ]

    try:
        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            tenant_headers = {"Authorization": "Bearer tenant-token"}
            other_headers = {"Authorization": "Bearer other-token"}
            tenant_created = await client.post(
                "/sessions", headers=tenant_headers, json={"site_id": "tenant-site"}
            )
            other_created = await client.post(
                "/sessions", headers=other_headers, json={"site_id": "other-site"}
            )
            assert tenant_created.status_code == other_created.status_code == 200
            tenant_session_id = tenant_created.json()["session_id"]
            other_session_id = other_created.json()["session_id"]
            hosted_session = host._sessions[tenant_session_id].session
            assert hosted_session.access_mode == "hosted"
            assert hosted_session.site_id == "tenant-site"

            for name, arguments, sentinel_values, capabilities in protected_tools:
                with pytest.raises(EnergyError) as denied_tool:
                    agent.get_tool(hosted_session, name)
                assert denied_tool.value.code == "tool_forbidden"

                search = await client.post(
                    f"/sessions/{tenant_session_id}/search",
                    headers=tenant_headers,
                    json={"query": name, "limit": 10},
                )
                assert search.status_code == 200
                assert name not in {tool["name"] for tool in search.json()["tools"]}

                execution = await client.post(
                    f"/sessions/{tenant_session_id}/execute",
                    headers=tenant_headers,
                    json={"tool": name, "arguments": arguments},
                )
                assert execution.status_code == 200
                body = execution.json()
                assert body["ok"] is False
                assert body["error"]["code"] == "tool_forbidden"
                assert "result" not in body
                assert all(value not in execution.text for value in sentinel_values)

                for capability in capabilities:
                    resolved = await client.post(
                        f"/sessions/{tenant_session_id}/resolve",
                        headers=tenant_headers,
                        json={"capability": capability},
                    )
                    assert resolved.status_code == 200
                    assert name not in {
                        candidate["tool"] for candidate in resolved.json()["candidates"]
                    }

            # The hosted session still creates and processes its own artifact through
            # public and session-scoped tools. The provider response is synthetic.
            forecast = await client.post(
                f"/sessions/{tenant_session_id}/execute",
                headers=tenant_headers,
                json={
                    "tool": "open_meteo.get_forecast",
                    "arguments": {
                        "latitude": 51.5,
                        "longitude": -0.12,
                        "variables": ["temperature_2m"],
                    },
                    "persist": True,
                },
            )
            assert forecast.status_code == 200
            forecast_body = forecast.json()
            assert forecast_body["ok"] is True, forecast_body
            artifact_id = forecast_body["result"]["data"]["artifact_id"]

            summary = await client.post(
                f"/sessions/{tenant_session_id}/execute",
                headers=tenant_headers,
                json={
                    "tool": "WORKBENCH_SUMMARIZE",
                    "arguments": {"artifact_id": artifact_id, "column": "value"},
                },
            )
            assert summary.status_code == 200
            assert summary.json()["ok"] is True
            assert summary.json()["result"]["data"]["mean"] == pytest.approx(13.5)

            cross_user = await client.post(
                f"/sessions/{other_session_id}/execute",
                headers=other_headers,
                json={
                    "tool": "WORKBENCH_SUMMARIZE",
                    "arguments": {"artifact_id": artifact_id, "column": "value"},
                },
            )
            assert cross_user.status_code == 200
            assert cross_user.json()["ok"] is False
            assert cross_user.json()["error"]["code"] == "artifact_not_found"

        # Local sessions retain operator-granted CSV and SQLite access.
        local_session = agent.session("tenant", "tenant-site")
        csv_read = await agent.execute(local_session, "CSV_READ_TIMESERIES", csv_arguments)
        assert csv_read["ok"] is True
        assert [row["value"] for row in csv_read["result"]["data"]] == ["1042.5", "2043.5"]

        imported = await agent.execute(local_session, "DATASET_IMPORT_CSV", dataset_arguments)
        assert imported["ok"] is True
        dataset_id = imported["result"]["data"]["dataset_id"]
        processed = await agent.execute(
            local_session, "DATASET_SUMMARIZE", {"artifact_id": dataset_id, "column": "value"}
        )
        assert processed["ok"] is True
        assert processed["result"]["data"]["sum"] == pytest.approx(3086.0)

        foreign_artifact = await agent.execute(
            agent.session("other", "other-site"),
            "DATASET_SUMMARIZE",
            {"artifact_id": dataset_id, "column": "value"},
        )
        assert foreign_artifact["ok"] is False
        assert foreign_artifact["error"]["code"] == "artifact_not_found"

        sqlite_read = await agent.execute(local_session, "sqlite.read_timeseries", sqlite_arguments)
        assert sqlite_read["ok"] is True
        assert [row["value"] for row in sqlite_read["result"]["data"]] == [3054.25, 4055.25]
    finally:
        await agent.close()
        await fake_provider.aclose()
