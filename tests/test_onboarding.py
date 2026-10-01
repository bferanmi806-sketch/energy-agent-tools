from __future__ import annotations

import base64
import concurrent.futures
import json
import stat
from pathlib import Path

import httpx
import pytest

from energy_agent_tools.app import build_agent
from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.onboarding import LocalProfile


def _octopus_response(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.path.endswith(
        "/electricity-meter-points/MPAN123/meters/SERIAL456/consumption/"
    )
    authorization = request.headers["authorization"]
    assert authorization.startswith("Basic ")
    decoded = base64.b64decode(authorization.removeprefix("Basic ")).decode()
    assert decoded == "fixture-secret:"
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "interval_start": "2026-09-30T00:00:00Z",
                    "interval_end": "2026-09-30T00:30:00Z",
                    "consumption": 1.25,
                }
            ]
        },
        request=request,
    )


@pytest.mark.asyncio
async def test_connect_store_probe_resolve_and_consume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_octopus_response))
    profile = LocalProfile(tmp_path, http=client)
    profile.create_site("Home", "Europe/London", site_id="home")

    outcome = await profile.connect(
        "octopus",
        credential="fixture-secret",
        site_id="home",
        connection_id="octopus-home",
        metadata={"mpan": "MPAN123", "serial_number": "SERIAL456"},
    )
    assert outcome["ok"] is True
    assert outcome["health"]["status"] == "healthy"
    assert outcome["health"]["probe"] == "provider-read"
    assert "fixture-secret" not in json.dumps(outcome)
    assert "fixture-secret" not in (tmp_path / "profile.json").read_text()
    assert b"fixture-secret" not in (tmp_path / "vault" / "auth.sqlite3").read_bytes()

    binding = outcome["config"]["accounts"][0]["settings"]["capability_bindings"][0]
    assert binding["capability"] == "get_energy_consumption"
    assert binding["tool"] == "octopus_energy.get_consumption"
    assert binding["kind"] == "metered"
    assert binding["unit"] == "kWh"
    assert outcome["config"]["vault"] == {"master_key_file": "vault.key"}

    # The current build_agent accepts an environment key.  The onboarding
    # profile deliberately emits the file reference that the extended app
    # loader consumes; this conversion keeps the fixture usable on v0.2.
    key_env = "TEST_ONBOARDING_MASTER"
    monkeypatch.setenv(key_env, (tmp_path / "vault.key").read_text())
    build_config = outcome["config"].copy()
    build_config["vault"] = {"master_key_env": key_env}
    agent = build_agent(tmp_path, build_config)
    await agent.http.aclose()
    agent.http = client
    agent._owns_http = False
    session = agent.session("local", "home")
    resolution = agent.resolver.resolve(
        session, CapabilityRequest(capability="get_energy_consumption")
    )
    assert resolution["status"] == "resolved"
    assert resolution["selected"]["account_id"] == "octopus-home"
    result = await agent.resolver.execute(
        session, CapabilityRequest(capability="get_energy_consumption")
    )
    assert result["ok"] is True
    assert result["result"]["unit"] == "kWh"
    assert result["result"]["kind"] == "metered"
    assert result["result"]["data"][0]["value"] == 1.25
    await agent.close()
    profile.close()

    # A new profile process can read both metadata and the encrypted secret.
    reopened = LocalProfile(tmp_path, http=client)
    assert reopened.connections("local", "home")[0]["verified"] is True
    assert reopened.auth_store.credential("local", "octopus-home", "home") == "fixture-secret"
    reopened.close()
    await client.aclose()


def test_key_profile_and_vault_permissions_and_no_plaintext(tmp_path: Path) -> None:
    profile = LocalProfile(tmp_path)
    profile.create_site("Home", "UTC")
    profile.configure_connection(
        "emoncms",
        credential="emon-secret",
        metadata={"base_url": "https://emon.example", "feed_id": 12},
    )
    assert stat.S_IMODE((tmp_path / "vault.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "profile.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) & 0o777 == 0o700
    assert stat.S_IMODE((tmp_path / "vault").stat().st_mode) & 0o777 == 0o700
    assert b"emon-secret" not in (tmp_path / "vault" / "auth.sqlite3").read_bytes()
    assert "emon-secret" not in (tmp_path / "profile.json").read_text()
    profile.close()


def test_octopus_origin_is_fixed_to_official_api(tmp_path: Path) -> None:
    profile = LocalProfile(tmp_path)
    profile.create_site("Home", "UTC")
    with pytest.raises(ValueError, match="fixed official API origin"):
        profile.configure_connection(
            "octopus",
            credential="octopus-secret",
            metadata={
                "base_url": "http://127.0.0.1:18123",
                "mpan": "MPAN123",
                "serial_number": "SERIAL456",
            },
        )
    profile.close()


def test_home_assistant_does_not_promote_cumulative_counter(tmp_path: Path) -> None:
    profile = LocalProfile(tmp_path)
    profile.create_site("Home", "Europe/London")
    account = profile.configure_connection(
        "home_assistant",
        credential="ha-secret",
        metadata={
            "base_url": "https://ha.example",
            "entity_id": "sensor.total_energy",
            "telemetry_role": "consumption_interval",
            "measurement_kind": "metered",
            "unit": "kWh",
            "quantity_shape": "interval",
            "state_class": "total_increasing",
            # A total_increasing counter is not interval energy.  Without an
            # explicit interval semantic claim, no reviewed mapping is made.
        },
    )
    assert "capability_bindings" not in account.settings
    profile.close()


@pytest.mark.parametrize(
    ("role", "unit", "quantity_shape", "capability", "tool"),
    [
        ("current_power", "W", "instantaneous", "get_current_power", "home_assistant.get_state"),
        ("generation", "kWh", "interval", "get_generation", "home_assistant.get_history"),
        ("generation", "kW", "instantaneous", "get_generation", "home_assistant.get_history"),
        ("export_interval", "kWh", "interval", "get_export", "home_assistant.get_history"),
        ("storage_state", "%", "instantaneous", "get_storage_state", "home_assistant.get_state"),
        ("storage_state", "kWh", "instantaneous", "get_storage_state", "home_assistant.get_state"),
    ],
)
@pytest.mark.asyncio
async def test_reviewed_home_assistant_mappings_resolve_to_matching_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    unit: str,
    quantity_shape: str,
    capability: str,
    tool: str,
) -> None:
    profile = LocalProfile(tmp_path)
    profile.create_site("Home", "Europe/London", site_id="home")
    profile.configure_connection(
        "home_assistant",
        credential="ha-secret",
        site_id="home",
        metadata={
            "base_url": "https://ha.example",
            "entity_id": "sensor.energy",
            "telemetry_role": role,
            "measurement_kind": "metered",
            "unit": unit,
            "quantity_shape": quantity_shape,
            "state_class": "measurement",
        },
    )
    key_env = "TEST_ONBOARDING_HA_MASTER"
    monkeypatch.setenv(key_env, (tmp_path / "vault.key").read_text())
    config = profile.config()
    config["vault"] = {"master_key_env": key_env}
    agent = build_agent(tmp_path, config)
    session = agent.session("local", "home")
    arguments = {}
    if tool == "home_assistant.get_history":
        arguments = {
            "start": "2026-09-30T00:00:00Z",
            "end": "2026-09-30T01:00:00Z",
        }
    resolved = agent.resolver.resolve(
        session, CapabilityRequest(capability=capability, arguments=arguments)
    )
    assert resolved["status"] == "resolved"
    assert resolved["selected"]["tool"] == tool
    assert resolved["selected"]["arguments"]["entity_id"] == "sensor.energy"
    await agent.close()
    profile.close()


@pytest.mark.asyncio
async def test_home_assistant_probe_validates_declared_state_metadata(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "entity_id": "sensor.power",
                "state": "2.5",
                "attributes": {"unit_of_measurement": "W", "state_class": "measurement"},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    profile = LocalProfile(tmp_path, http=client)
    profile.create_site("Home", "UTC")
    result = await profile.connect(
        "home_assistant",
        credential="ha-secret",
        metadata={
            "base_url": "https://ha.example",
            "entity_id": "sensor.power",
            "telemetry_role": "current_power",
            "measurement_kind": "metered",
            "unit": "W",
            "quantity_shape": "instantaneous",
            "state_class": "measurement",
        },
    )
    assert result["health"]["status"] == "healthy"
    assert result["account"]["verified"] is True
    profile.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_home_assistant_probe_rejects_declared_unit_mismatch(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "entity_id": "sensor.power",
                "state": "2.5",
                "attributes": {"unit_of_measurement": "W", "state_class": "measurement"},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    profile = LocalProfile(tmp_path, http=client)
    profile.create_site("Home", "UTC")
    result = await profile.connect(
        "home_assistant",
        credential="ha-secret",
        metadata={
            "base_url": "https://ha.example",
            "entity_id": "sensor.power",
            "telemetry_role": "current_power",
            "measurement_kind": "metered",
            "unit": "kW",
            "quantity_shape": "instantaneous",
            "state_class": "measurement",
        },
    )
    assert result["health"]["status"] == "unhealthy"
    assert result["account"]["verified"] is False
    profile.close()
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {"value": 2.0}, True, [2.0], float("inf")])
async def test_emoncms_probe_requires_finite_numeric_value(tmp_path: Path, payload: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    profile = LocalProfile(tmp_path, http=client)
    profile.create_site("Home", "UTC")
    result = await profile.connect(
        "emoncms",
        credential="emon-secret",
        metadata={"base_url": "https://emon.example", "feed_id": 7},
    )
    assert result["health"]["status"] == "unhealthy"
    assert result["account"]["verified"] is False
    profile.close()
    await client.aclose()


def test_parallel_profile_initialization_keeps_one_fernet_key(tmp_path: Path) -> None:
    def open_and_read_key(_: int) -> bytes:
        profile = LocalProfile(tmp_path)
        try:
            return (tmp_path / "vault.key").read_bytes()
        finally:
            profile.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        keys = list(executor.map(open_and_read_key, range(6)))
    assert len(set(keys)) == 1
    assert len(keys[0]) == 44


def test_home_assistant_interval_mapping_requires_explicit_quantity_shape(tmp_path: Path) -> None:
    profile = LocalProfile(tmp_path)
    profile.create_site("Home", "Europe/London")
    account = profile.configure_connection(
        "home_assistant",
        credential="ha-secret",
        metadata={
            "base_url": "https://ha.example",
            "entity_id": "sensor.interval_energy",
            "telemetry_role": "consumption_interval",
            "measurement_kind": "metered",
            "unit": "kWh",
            "quantity_shape": "interval",
        },
    )
    binding = account.settings["capability_bindings"][0]
    assert binding["capability"] == "get_energy_consumption"
    assert binding["fixed_arguments"] == {"entity_id": "sensor.interval_energy"}
    profile.close()


@pytest.mark.asyncio
async def test_provider_failure_has_public_health_without_secret_or_url_query(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"detail": "token fixture-secret"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    profile = LocalProfile(tmp_path, http=client)
    profile.create_site("Home", "UTC")
    health = await profile.connect(
        "emoncms",
        credential="fixture-secret",
        metadata={"base_url": "https://emon.example", "feed_id": 7},
    )
    assert health["ok"] is False
    assert health["health"]["status"] == "unhealthy"
    assert health["health"]["message"] == "Provider verification failed."
    assert "fixture-secret" not in json.dumps(health)
    assert "apikey" not in health["health"]
    assert seen[0].url.params["apikey"] == "fixture-secret"
    profile.close()
    await client.aclose()


def test_metadata_rejects_secret_shaped_values(tmp_path: Path) -> None:
    profile = LocalProfile(tmp_path)
    profile.create_site("Home", "UTC")
    with pytest.raises(ValueError, match="credential fields"):
        profile.configure_connection(
            "home_assistant",
            credential="fixture-secret",
            metadata={"base_url": "https://ha.example", "token": "should-reject"},
        )
    profile.close()
