from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from scripts import qualify_home_assistant as base
from scripts import qualify_home_assistant_authorization as qualification


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("utf-8"))


@pytest.mark.asyncio
async def test_existing_user_login_exchange_refresh_and_revoke_protocol() -> None:
    onboarding_code = "onboarding-code-private"
    login_code = "login-flow-code-private"
    first_access = "first-access-private"
    refreshed_access = "refreshed-access-private"
    refresh_token = "refresh-token-private"
    seen: list[tuple[str, str]] = []
    revoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal revoked
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/onboarding":
            return httpx.Response(200, json=[{"step": "user", "done": False}])
        if request.method == "POST" and request.url.path == "/api/onboarding/users":
            payload = json.loads(request.content)
            assert payload["username"].startswith("eat_oauth_")
            assert isinstance(payload["password"], str) and payload["password"]
            assert payload["client_id"] == "http://127.0.0.1:18123/"
            return httpx.Response(200, json={"auth_code": onboarding_code})
        if request.method == "POST" and request.url.path == "/auth/login_flow":
            payload = json.loads(request.content)
            assert payload == {
                "client_id": "http://127.0.0.1:18123/",
                "handler": ["homeassistant", None],
                "redirect_uri": "http://127.0.0.1:18123/oauth/callback",
            }
            return httpx.Response(200, json={"type": "form", "flow_id": "flow-123"})
        if request.method == "POST" and request.url.path == "/auth/login_flow/flow-123":
            payload = json.loads(request.content)
            assert payload["username"].startswith("eat_oauth_")
            assert payload["password"]
            return httpx.Response(200, json={"type": "create_entry", "result": login_code})
        if request.method == "POST" and request.url.path == "/auth/token":
            form = _form(request)
            if form.get("grant_type") == ["authorization_code"]:
                assert form == {
                    "grant_type": ["authorization_code"],
                    "code": [login_code],
                    "client_id": ["http://127.0.0.1:18123/"],
                }
                assert form["code"] != [onboarding_code]
                return httpx.Response(
                    200,
                    json={
                        "access_token": first_access,
                        "refresh_token": refresh_token,
                        "token_type": "Bearer",
                        "expires_in": 1800,
                    },
                )
            if form.get("grant_type") == ["refresh_token"]:
                if not revoked and form.get("refresh_token") == [refresh_token]:
                    return httpx.Response(
                        200,
                        json={
                            "access_token": refreshed_access,
                            "token_type": "Bearer",
                            "expires_in": 1800,
                        },
                    )
                return httpx.Response(400, json={"error": "invalid_grant"})
        if request.method == "GET" and request.url.path == "/api/config":
            token = request.headers.get("authorization")
            if not revoked and (
                token == f"Bearer {first_access}" or token == f"Bearer {refreshed_access}"
            ):
                return httpx.Response(200, json={"version": qualification.HOME_ASSISTANT_VERSION})
            return httpx.Response(401, json={"message": "Unauthorized"})
        if (
            request.method == "POST"
            and request.url.path == f"/api/states/{qualification.ENTITY_ID}"
        ):
            assert request.headers["authorization"] == f"Bearer {first_access}"
            return httpx.Response(
                201,
                json={
                    "entity_id": qualification.ENTITY_ID,
                    "state": "1.75",
                    "attributes": {"unit_of_measurement": "kW"},
                },
            )
        if request.method == "GET" and request.url.path == f"/api/states/{qualification.ENTITY_ID}":
            assert request.headers.get("authorization") in {
                f"Bearer {first_access}",
                f"Bearer {refreshed_access}",
            }
            return httpx.Response(
                200,
                json={
                    "entity_id": qualification.ENTITY_ID,
                    "state": "1.75",
                    "attributes": {"unit_of_measurement": "kW"},
                },
            )
        if request.method == "POST" and request.url.path == "/auth/revoke":
            assert _form(request) == {"token": [refresh_token]}
            revoked = True
            return httpx.Response(200)
        return httpx.Response(404, json={"message": "not found"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:18123",
        follow_redirects=True,
    ) as client:
        report = await qualification.qualify_existing_user(client, "http://127.0.0.1:18123")

    assert report["ok"] is True
    assert report["protocol"] == {
        "existing_user_login_flow_completed": True,
        "login_flow_authorization_code_exchanged": True,
        "access_token_api_verified": True,
        "synthetic_entity_read": True,
        "refresh_token_accepted": True,
        "refreshed_access_token_api_verified": True,
        "refresh_token_revoked": True,
        "revoked_refresh_token_rejected": True,
        "revoked_access_token_rejected": True,
        "browser_consent_driven": False,
    }
    assert seen.count(("POST", "/auth/token")) == 3
    assert onboarding_code != login_code
    for private_value in (
        onboarding_code,
        login_code,
        first_access,
        refreshed_access,
        refresh_token,
    ):
        assert private_value not in repr(report)


@pytest.mark.asyncio
async def test_redirect_and_reflected_token_are_rejected_without_echoing_secrets() -> None:
    private = "private-token-that-must-not-escape"
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": f"http://evil.invalid/?access_token={private}"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect),
        base_url="http://127.0.0.1:18123",
        follow_redirects=True,
    ) as client:
        with pytest.raises(qualification.QualificationFailure) as caught:
            await qualification._request_response(client, "GET", "/start")
    assert caught.value.code == "unexpected_redirect"
    assert private not in str(caught.value)
    assert len(requests) == 1

    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"debug": private})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(echo), base_url="http://127.0.0.1:18123"
    ) as client:
        with pytest.raises(qualification.QualificationFailure) as caught:
            await qualification._request_json(
                client, "GET", "/api/config", reject_echoes=(private,)
            )
    assert caught.value.code == "secret_echo"
    assert private not in str(caught.value)


def test_failed_run_cleans_only_its_generated_container_and_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    cleanup: list[tuple[str, str, str]] = []

    def fake_docker(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        if args[0] == "pull":
            raise qualification.QualificationFailure("docker_failed", "safe failure")
        return ""

    def fake_cleanup(container_name: str, volume_name: str, init_name: str) -> bool:
        cleanup.append((container_name, volume_name, init_name))
        return True

    monkeypatch.setattr(base, "docker", fake_docker)
    monkeypatch.setattr(base, "cleanup_owned_resources", fake_cleanup)

    with pytest.raises(qualification.QualificationFailure, match="safe failure"):
        qualification.run_qualification()

    assert len(cleanup) == 1
    container, volume, initializer = cleanup[0]
    assert container.startswith("eat-ha-qualification-")
    assert volume == container
    assert initializer == container + "-init"
    assert calls[0][0] == "pull"
    assert calls[1] == (
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"name=^/{container}$",
    )
    assert calls[2] == (
        "volume",
        "ls",
        "--quiet",
        "--filter",
        f"name=^{volume}$",
    )
    assert calls[3] == (
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"name=^/{initializer}$",
    )


def test_primary_failure_and_cleanup_failure_are_both_saved_safely(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    owned: tuple[str, str, str] | None = None

    def fake_docker(*args: str, **kwargs: Any) -> str:
        if args[0] == "port":
            raise qualification.QualificationFailure(
                "home_assistant_startup_timeout", "Home Assistant did not become ready in time."
            )
        if args[0] == "volume" and args[1] == "ls":
            assert owned is not None
            return owned[1]
        if args[0] == "ps":
            assert owned is not None
            return owned[2] if owned[2] in args[-1] else owned[0]
        return ""

    def fail_cleanup(container_name: str, volume_name: str, init_name: str) -> bool:
        nonlocal owned
        owned = (container_name, volume_name, init_name)
        return False

    report_path = tmp_path / "report.json"
    monkeypatch.setattr(base, "docker", fake_docker)
    monkeypatch.setattr(base, "cleanup_owned_resources", fail_cleanup)
    monkeypatch.setattr(sys, "argv", ["qualify", "--report", str(report_path)])

    with pytest.raises(SystemExit) as caught:
        qualification.main()

    assert caught.value.code == 1
    saved = report_path.read_text(encoding="utf-8")
    report = json.loads(saved)
    assert owned is not None
    assert report["error"]["code"] == "home_assistant_startup_timeout"
    assert report["cleanup_error"]["code"] == "cleanup_failed"
    assert report["startup"] == {
        "home_assistant_version": qualification.HOME_ASSISTANT_VERSION,
        "timeout_seconds": qualification.STARTUP_TIMEOUT_SECONDS,
        "observed_symptom": "Unauthenticated onboarding did not become ready before the timeout.",
    }
    assert report["resources"] == {
        "container": owned[0],
        "volume": owned[1],
        "initializer": owned[2],
    }
    assert report["cleanup"] == {
        "attempted": True,
        "command_succeeded": False,
        "resources_verified_absent": False,
    }
    assert owned[0] in saved and owned[1] in saved and owned[2] in saved
    assert "Home Assistant did not become ready in time." in capsys.readouterr().out


def test_keyboard_interrupt_runs_exact_cleanup_then_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("stop qualification")
    cleanup: list[tuple[str, str, str]] = []

    def interrupted_docker(*args: str, **kwargs: Any) -> str:
        raise interrupt

    def fake_cleanup(container_name: str, volume_name: str, init_name: str) -> bool:
        cleanup.append((container_name, volume_name, init_name))
        return True

    def verify_exact_resources(container_name: str, volume_name: str, init_name: str) -> bool:
        assert cleanup == [(container_name, volume_name, init_name)]
        assert volume_name == container_name
        assert init_name == container_name + "-init"
        return True

    monkeypatch.setattr(base, "docker", interrupted_docker)
    monkeypatch.setattr(base, "cleanup_owned_resources", fake_cleanup)
    monkeypatch.setattr(qualification, "_verify_owned_cleanup", verify_exact_resources)

    with pytest.raises(KeyboardInterrupt) as caught:
        qualification.run_qualification()

    assert caught.value is interrupt
    assert len(cleanup) == 1


def test_request_urls_are_exact_local_same_origin_callbacks() -> None:
    client_id, redirect_uri = qualification._request_client_urls("http://127.0.0.1:18123")
    assert client_id == "http://127.0.0.1:18123/"
    assert redirect_uri == "http://127.0.0.1:18123/oauth/callback"
    for url in (
        "https://127.0.0.1:18123",
        "http://localhost:18123",
        "http://127.0.0.1:18123/other",
    ):
        with pytest.raises(qualification.QualificationFailure):
            qualification._request_client_urls(url)


def test_failure_output_has_no_response_body_or_private_value() -> None:
    private = "private-password-value"
    failure = qualification.QualificationFailure(
        "home_assistant_protocol", "Home Assistant returned HTTP 401 for GET."
    )
    assert private not in str(failure)
    assert private not in str({"code": failure.code, "message": failure.message})


def test_main_saves_only_safe_failure_report(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = tmp_path / "report.json"
    private = "private-token-value"

    def fail_run(startup_timeout: int) -> dict[str, Any]:
        assert startup_timeout == qualification.STARTUP_TIMEOUT_SECONDS
        raise qualification.QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned HTTP 401 for GET."
        )

    monkeypatch.setattr(sys, "argv", ["qualify", "--report", str(report_path)])
    monkeypatch.setattr(qualification, "run_qualification", fail_run)
    with pytest.raises(SystemExit) as caught:
        qualification.main()

    assert caught.value.code == 1
    saved = report_path.read_text(encoding="utf-8")
    assert json.loads(saved) == {
        "ok": False,
        "error": {
            "code": "home_assistant_protocol",
            "message": "Home Assistant returned HTTP 401 for GET.",
        },
    }
    assert private not in saved
    assert private not in capsys.readouterr().out
