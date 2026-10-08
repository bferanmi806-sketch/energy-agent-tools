"""Approved cloud applications expose metadata without operator credentials."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from energy_agent_tools.cloud_oauth import CloudEnergyOAuthConfiguration


def configuration(monkeypatch, **changes):
    monkeypatch.setenv("TEST_CLOUD_SECRET", "operator-private-secret")
    monkeypatch.setenv("TEST_CLOUD_KEY", "operator-private-app-key")
    fields = {
        "id": "tesla-test",
        "name": "My energy system",
        "provider": "tesla",
        "client_id": "registered-client",
        "client_secret_env": "TEST_CLOUD_SECRET",
        "redirect_uri": "https://energy.example/api/workspace/oauth/callback",
        "region": "eu",
    }
    fields.update(changes)
    return CloudEnergyOAuthConfiguration.model_validate(fields)


def test_approved_cloud_profiles_pin_authentication_and_keep_secrets_private(monkeypatch):
    tesla = configuration(monkeypatch)
    assert tesla.public() == {
        "id": "tesla-test",
        "name": "My energy system",
        "toolkit": "tesla-energy",
        "protocol": "oauth2_confidential",
    }
    provider = tesla.provider()
    assert provider.scopes == ("openid", "offline_access", "energy_device_data")
    assert provider.extra_token_params == {
        "audience": "https://fleet-api.prd.eu.vn.cloud.tesla.com"
    }
    assert provider.refresh_endpoint_auth_method == "none"
    assert provider.revocation_endpoint is None
    enphase = configuration(
        monkeypatch, provider="enphase", region="na", api_key_env="TEST_CLOUD_KEY"
    )
    assert enphase.provider().token_endpoint_auth_method == "client_secret_basic"
    assert enphase.provider().refresh_endpoint_auth_method is None
    public = json.dumps(
        [tesla.public(), enphase.public(), tesla.model_dump(), enphase.model_dump()]
    )
    assert "operator-private-secret" not in public
    assert "operator-private-app-key" not in public
    previous = enphase.fingerprint()
    monkeypatch.setenv("TEST_CLOUD_KEY", "changed-operator-key")
    assert enphase.fingerprint() != previous


@pytest.mark.parametrize(
    "changes",
    [
        {"redirect_uri": "http://energy.example/api/workspace/oauth/callback"},
        {"redirect_uri": "https://user:password@energy.example/callback"},
        {"redirect_uri": "https://energy.example/callback?target=other"},
        {"region": "cn"},
        {"client_secret_env": "MISSING_CLOUD_SECRET"},
        {"provider": "enphase", "region": "na"},
        {"api_key_env": "TEST_CLOUD_KEY"},
        {"authorization_endpoint": "https://attacker.example/authorize"},
    ],
)
def test_invalid_or_unapproved_cloud_configuration_is_refused(monkeypatch, changes):
    monkeypatch.delenv("MISSING_CLOUD_SECRET", raising=False)
    with pytest.raises(ValidationError):
        configuration(monkeypatch, **changes)
