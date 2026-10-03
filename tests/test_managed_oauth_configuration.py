from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.managed_oauth import (
    HomeAssistantOAuthConfiguration,
    ManagedHomeAssistantOAuth,
)
from energy_agent_tools.models import EnergyError


def configuration(**changes):
    return HomeAssistantOAuthConfiguration.model_validate(
        {
            "id": "home",
            "name": "Home instance",
            "base_url": "https://home.example.test",
            "client_id": "https://energy.example.test",
            "redirect_uri": "https://energy.example.test/api/workspace/oauth/callback",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://home.example.test"},
        {"base_url": "https://user:secret@home.example.test"},
        {"base_url": "https://home.example.test?token=secret"},
        {"base_url": "https://home.example.test/proxy/path"},
        {"redirect_uri": "https://elsewhere.example.test/callback"},
        {"redirect_uri": "https://energy.example.test:444/callback"},
        {"redirect_uri": "https://energy.example.test/callback#fragment"},
        {"client_id": "http://energy.example.test"},
        {"id": "../home"},
        {"name": "\nHome"},
        {"client_secret": "unexpected"},
    ],
)
def test_configuration_rejects_unsafe_or_mismatched_destinations(changes):
    with pytest.raises(ValidationError):
        configuration(**changes)


def test_public_configuration_omits_network_and_protocol_secrets():
    approved = configuration(base_url="https://home.example.test/")
    assert approved.base_url == "https://home.example.test"
    assert approved.public() == {
        "id": "home",
        "name": "Home instance",
        "toolkit": "home-assistant",
        "protocol": "home_assistant",
    }
    with pytest.raises(ValidationError):
        approved.base_url = "https://elsewhere.example.test"


def test_loopback_configuration_accepts_explicit_default_port_equivalence():
    approved = configuration(
        base_url="http://127.0.0.1:8123",
        client_id="http://localhost",
        redirect_uri="http://localhost:80/callback",
    )
    assert approved.base_url == "http://127.0.0.1:8123"


@pytest.mark.asyncio
async def test_unknown_configuration_is_rejected_before_enrollment_or_provider_io(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request)
        raise AssertionError("Unknown configuration must not reach provider")

    store = AuthStore(tmp_path / "vault", Fernet.generate_key())
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
            service = ManagedHomeAssistantOAuth(store, http, [configuration()])
            with pytest.raises(EnergyError, match="approved configuration"):
                service.begin(
                    user_id="u",
                    workspace_id="w",
                    configuration_id="https://arbitrary.test",
                    entity_id="sensor.power",
                )
            with pytest.raises(EnergyError, match="approved configuration"):
                await service.complete(
                    user_id="u", workspace_id="w", configuration_id="unknown", state="s", code="c"
                )
            assert store.workspace_accounts("u", "w") == []
            assert calls == []
            with pytest.raises(ValueError, match="unique"):
                ManagedHomeAssistantOAuth(store, http, [configuration(), configuration()])
    finally:
        store.close()
