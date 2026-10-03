from __future__ import annotations

import base64
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from scripts import qualify_home_assistant as qualification
from scripts import qualify_home_assistant_authorization as authorization


@pytest.mark.parametrize("outcome", ["passed", "safe_failure", "unexpected_failure"])
def test_default_cli_requires_authorization_and_preserves_provider_evidence(
    outcome: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stages: list[str] = []

    def reads(image: str, timeout: int):
        assert image == qualification.HOME_ASSISTANT_IMAGE
        assert timeout == qualification.STARTUP_TIMEOUT_SECONDS
        stages.append("reads_completed_and_cleaned")
        return {"ok": True, "reviewed_profile": {"entity_scoped": True}}

    def authorize(timeout: int):
        assert timeout == qualification.STARTUP_TIMEOUT_SECONDS
        stages.append("authorization")
        if outcome == "unexpected_failure":
            raise RuntimeError("private-provider-token")
        if outcome == "safe_failure":
            failure = authorization.QualificationFailure("authorization_failed", "Safe failure.")
            failure.report = {
                "ok": False,
                "error": {"code": "authorization_failed", "message": "Safe failure."},
                "cleanup": {"resources_verified_absent": True},
            }
            raise failure
        return {"ok": True, "protocol": {"refresh_token_revoked": True}}

    monkeypatch.setattr(sys, "argv", ["qualify_home_assistant.py"])
    monkeypatch.setattr(qualification, "run_qualification", reads)
    monkeypatch.setattr(authorization, "run_qualification", authorize)
    if outcome == "passed":
        qualification.main()
    else:
        with pytest.raises(SystemExit) as caught:
            qualification.main()
        assert caught.value.code == 1
    output = capsys.readouterr().out
    assert "private-provider-token" not in output
    result = json.loads(output)
    assert stages == ["reads_completed_and_cleaned", "authorization"]
    assert result["reviewed_profile"] == {"entity_scoped": True}
    assert result["ok"] is (outcome == "passed")
    assert result["authorization"]["ok"] is (outcome == "passed")
    if outcome == "safe_failure":
        assert result["authorization"]["cleanup"]["resources_verified_absent"] is True


def test_complete_qualification_rejects_unpinned_image_before_docker(monkeypatch):
    def forbidden(*_args):
        pytest.fail("No Docker work should run for an unpinned qualification image.")

    monkeypatch.setattr(qualification, "run_qualification", forbidden)
    with pytest.raises(qualification.QualificationFailure, match="pinned"):
        qualification.run_complete_qualification("unapproved:latest", 180)


def test_configuration_helper_is_minimal_and_secret_free() -> None:
    command = qualification._configuration_command(
        qualification.HOME_ASSISTANT_IMAGE,
        "eat-ha-qualification-" + "a" * 32,
        "eat-ha-qualification-" + "a" * 32 + "-init",
    )
    encoded = re.search(r"printf '%s' '([^']+)'", command[-1]).group(1)
    configuration = base64.b64decode(encoded).decode("utf-8")
    assert "default_config" not in configuration
    assert "sun:" not in configuration
    assert "go2rtc" not in configuration
    assert "homeassistant:" in configuration and "api:" in configuration
    assert qualification.HOME_ASSISTANT_IMAGE in command


@pytest.mark.asyncio
async def test_onboarding_uses_real_http_protocol_without_returning_credentials() -> None:
    requests: list[httpx.Request] = []
    auth_code = "project-auth-code"
    access_token = "project-access-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/onboarding":
            return httpx.Response(200, json=[{"done": False}], request=request)
        if request.method == "POST" and request.url.path == "/api/onboarding/users":
            body = json.loads(request.content)
            assert body["name"] == "Energy Agent Tools qualification"
            assert body["language"] == "en"
            assert body["client_id"] == qualification.CLIENT_ID
            assert isinstance(body["username"], str) and body["username"].startswith(
                "eat_qualification_"
            )
            assert isinstance(body["password"], str) and body["password"]
            return httpx.Response(200, json={"auth_code": auth_code}, request=request)
        if request.method == "POST" and request.url.path == "/auth/token":
            form = dict(item.split("=", 1) for item in request.content.decode().split("&"))
            assert form == {
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": "http%3A%2F%2Flocalhost%2F",
            }
            return httpx.Response(
                200,
                json={"access_token": access_token, "refresh_token": "project-refresh-token"},
                request=request,
            )
        if request.method == "GET" and request.url.path == "/api/config":
            assert request.headers["authorization"] == "Bearer " + access_token
            return httpx.Response(
                200, json={"version": qualification.HOME_ASSISTANT_VERSION}, request=request
            )
        if (
            request.method == "POST"
            and request.url.path == "/api/states/" + qualification.ENTITY_ID
        ):
            assert request.headers["authorization"] == "Bearer " + access_token
            body = json.loads(request.content)
            assert body == {
                "state": "1.75",
                "attributes": {
                    "unit_of_measurement": "kW",
                    "friendly_name": "Synthetic qualification power",
                },
            }
            return httpx.Response(
                201,
                json={
                    "entity_id": qualification.ENTITY_ID,
                    "state": "1.75",
                    "attributes": body["attributes"],
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ha.test"
    ) as client:
        returned = await qualification.onboard_and_seed(client)
    assert returned == access_token
    assert len(requests) == 5
    # The function has no logging or return envelope containing the generated
    # password/auth code.  Only the access token is kept for the immediate
    # provider exercise.
    assert qualification._reviewed_binding()["kind"] == "estimated"
    assert "state_class" not in qualification._reviewed_binding()


@pytest.mark.asyncio
async def test_profile_execution_proves_encrypted_scope_freshness_and_provenance(
    tmp_path: Path,
) -> None:
    observed = datetime.now(UTC).isoformat()
    access_token = "profile-exercise-token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer " + access_token
        return httpx.Response(
            200,
            json={
                "entity_id": qualification.ENTITY_ID,
                "state": "1.75",
                "attributes": {
                    "unit_of_measurement": "kW",
                    "friendly_name": "Synthetic qualification power",
                },
                "last_updated": observed,
                "last_changed": observed,
            },
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:18123"
    ) as client:
        evidence = await qualification.qualify_profile(
            client,
            "http://127.0.0.1:18123",
            tmp_path / "profile",
            access_token,
        )
    assert evidence["ok"] is True
    assert evidence["synthetic_reading"]["kind"] == "estimated"
    assert evidence["synthetic_reading"]["state_class"] is None
    assert evidence["credential_storage"] == {
        "encrypted_vault": True,
        "profile_excludes_credential": True,
        "output_excludes_credential": True,
    }
    assert evidence["scope"]["asset_id"] == qualification.ASSET_ID


def test_docker_failure_does_not_echo_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "should-never-be-printed"

    def fake_run(*args, **kwargs):
        return qualification.subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="", stderr=secret
        )

    monkeypatch.setattr(qualification.subprocess, "run", fake_run)
    with pytest.raises(qualification.QualificationFailure) as caught:
        qualification.docker("run", "image")
    assert secret not in str(caught.value)


def test_cleanup_targets_only_generated_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    root = "eat-ha-qualification-" + "b" * 32
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return qualification.subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(qualification.subprocess, "run", fake_run)
    assert qualification.cleanup_owned_resources(root, root, root + "-init") is True
    assert calls == [
        ["docker", "rm", "--force", root + "-init"],
        ["docker", "rm", "--force", root],
        ["docker", "volume", "rm", "--force", root],
    ]
