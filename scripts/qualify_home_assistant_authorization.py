"""Qualify an existing Home Assistant user's login-flow OAuth lifecycle.

The script provisions one development owner in a disposable, pinned Home
Assistant installation, then authenticates that existing user through the
normal login-flow endpoint. It exercises authorization-code exchange, API
access, refresh, and revocation. It does not drive a browser or claim a physical
meter or private-user consent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

if __package__:
    from . import qualify_home_assistant as base
else:
    import qualify_home_assistant as base

HOME_ASSISTANT_IMAGE = base.HOME_ASSISTANT_IMAGE
HOME_ASSISTANT_VERSION = base.HOME_ASSISTANT_VERSION
STARTUP_TIMEOUT_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = base.REQUEST_TIMEOUT_SECONDS
MAX_HTTP_RESPONSE_BYTES = base.MAX_HTTP_RESPONSE_BYTES
ENTITY_ID = base.ENTITY_ID
CLIENT_NAME = "Energy Agent Tools authorization qualification"

QualificationFailure = base.QualificationFailure


class QualificationRunFailure(QualificationFailure):
    """A safe run failure that keeps primary and cleanup evidence together."""

    def __init__(
        self,
        primary: QualificationFailure | None,
        *,
        container_name: str,
        volume_name: str,
        init_name: str,
        startup_timeout_seconds: int,
        cleanup_command_succeeded: bool,
        resources_verified_absent: bool,
    ) -> None:
        cleanup_failed = not cleanup_command_succeeded or not resources_verified_absent
        if primary is not None:
            code, message = primary.code, primary.message
        else:
            code = "cleanup_failed"
            message = (
                "Owned Home Assistant Docker resources could not be removed."
                if not cleanup_command_succeeded
                else "Owned Home Assistant Docker resources remain after cleanup."
            )
        super().__init__(code, message)
        self.report: dict[str, Any] = {
            "ok": False,
            "error": {"code": code, "message": message},
            "resources": {
                "container": container_name,
                "volume": volume_name,
                "initializer": init_name,
            },
            "cleanup": {
                "attempted": True,
                "command_succeeded": cleanup_command_succeeded,
                "resources_verified_absent": resources_verified_absent,
            },
        }
        if primary is not None and primary.code == "home_assistant_startup_timeout":
            self.report["startup"] = {
                "home_assistant_version": HOME_ASSISTANT_VERSION,
                "timeout_seconds": startup_timeout_seconds,
                "observed_symptom": (
                    "Unauthenticated onboarding did not become ready before the timeout."
                ),
            }
        if cleanup_failed:
            self.report["cleanup_error"] = {
                "code": "cleanup_failed",
                "message": "Owned Home Assistant Docker resources could not be confirmed removed.",
            }


def _request_client_urls(base_url: str) -> tuple[str, str]:
    """Return a local IndieAuth client ID and same-origin callback URI."""

    parsed = urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise QualificationFailure(
            "invalid_local_url", "Authorization qualification must use its local loopback URL."
        )
    origin = f"http://127.0.0.1:{parsed.port}"
    return f"{origin}/", f"{origin}/oauth/callback"


def _reject_secret_echo(content: bytes, secret_values: Iterable[str]) -> None:
    """Fail safely if a non-token endpoint reflects a supplied secret."""

    if any(secret and secret.encode("utf-8") in content for secret in secret_values):
        raise QualificationFailure(
            "secret_echo", "Home Assistant echoed a private authorization value."
        )


async def _request_response(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    expected_status: int = 200,
    reject_echoes: Iterable[str] = (),
    **kwargs: Any,
) -> httpx.Response:
    """Send one bounded request without following redirects or exposing bodies."""

    try:
        response = await client.request(method, path, follow_redirects=False, **kwargs)
    except httpx.HTTPError:
        raise QualificationFailure(
            "home_assistant_unavailable", "Home Assistant did not answer the request."
        ) from None
    if 300 <= response.status_code < 400:
        raise QualificationFailure(
            "unexpected_redirect", "Home Assistant returned an unexpected redirect."
        )
    if response.status_code != expected_status:
        raise QualificationFailure(
            "home_assistant_protocol",
            f"Home Assistant returned HTTP {response.status_code} for {method}.",
        )
    if len(response.content) > MAX_HTTP_RESPONSE_BYTES:
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned too much data."
        )
    _reject_secret_echo(response.content, reject_echoes)
    return response


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    expected_status: int = 200,
    reject_echoes: Iterable[str] = (),
    **kwargs: Any,
) -> Any:
    response = await _request_response(
        client,
        method,
        path,
        expected_status=expected_status,
        reject_echoes=reject_echoes,
        **kwargs,
    )
    try:
        return response.json()
    except (TypeError, ValueError):
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned invalid JSON."
        ) from None


def _required_string(payload: Any, key: str) -> str:
    if not isinstance(payload, Mapping):
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned an invalid response."
        )
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned an invalid response."
        )
    return value


def _token_response(payload: Any, *, forbidden_values: Iterable[str]) -> dict[str, Any]:
    """Validate the documented short-lived token response without logging it."""

    if not isinstance(payload, Mapping):
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned an invalid token response."
        )
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _reject_secret_echo(serialized, forbidden_values)
    access_token = _required_string(payload, "access_token")
    token_type = _required_string(payload, "token_type")
    if token_type.lower() != "bearer":
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned an unsupported token type."
        )
    expires_in = payload.get("expires_in")
    if type(expires_in) is not int or not 0 < expires_in <= 24 * 60 * 60:
        raise QualificationFailure(
            "home_assistant_protocol", "Home Assistant returned an invalid token expiry."
        )
    return {"access_token": access_token, "expires_in": expires_in}


async def _create_existing_user(client: httpx.AsyncClient, client_id: str) -> tuple[str, str, str]:
    """Create a disposable development owner and retain its login credentials."""

    steps = await _request_json(client, "GET", "/api/onboarding")
    if not isinstance(steps, list) or not any(
        isinstance(step, Mapping) and step.get("step") == "user" and step.get("done") is False
        for step in steps
    ):
        raise QualificationFailure(
            "onboarding_state_invalid", "Disposable Home Assistant onboarding is not ready."
        )
    username = f"eat_oauth_{secrets.token_hex(8)}"
    password = secrets.token_urlsafe(32)
    created = await _request_json(
        client,
        "POST",
        "/api/onboarding/users",
        json={
            "name": CLIENT_NAME,
            "username": username,
            "password": password,
            "client_id": client_id,
            "language": "en",
        },
        reject_echoes=(password,),
    )
    # Discard the code returned by onboarding; the tested code comes from a
    # subsequent login flow for the now-existing user.
    onboarding_code = _required_string(created, "auth_code")
    return username, password, onboarding_code


async def _login_existing_user(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    redirect_uri: str,
    username: str,
    password: str,
    onboarding_code: str,
) -> str:
    """Submit the built-in local auth provider's login flow for an existing user."""

    started = await _request_json(
        client,
        "POST",
        "/auth/login_flow",
        json={
            "client_id": client_id,
            "handler": ["homeassistant", None],
            "redirect_uri": redirect_uri,
        },
        reject_echoes=(password, onboarding_code),
    )
    if not isinstance(started, Mapping) or started.get("type") != "form":
        raise QualificationFailure(
            "login_flow_failed", "Home Assistant did not start its local login flow."
        )
    flow_id = _required_string(started, "flow_id")
    completed = await _request_json(
        client,
        "POST",
        f"/auth/login_flow/{flow_id}",
        json={"client_id": client_id, "username": username, "password": password},
        reject_echoes=(password, onboarding_code),
    )
    if not isinstance(completed, Mapping) or completed.get("type") != "create_entry":
        raise QualificationFailure(
            "login_flow_failed", "Existing-user authentication did not complete."
        )
    code = _required_string(completed, "result")
    if code == onboarding_code:
        raise QualificationFailure(
            "login_flow_failed", "Login flow reused the one-time onboarding code."
        )
    return code


async def _exchange_code(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code: str,
    forbidden_values: Iterable[str],
) -> dict[str, Any]:
    response = await _request_json(
        client,
        "POST",
        "/auth/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": client_id},
    )
    tokens = _token_response(response, forbidden_values=forbidden_values)
    tokens["refresh_token"] = _required_string(response, "refresh_token")
    return tokens


async def _refresh_token(
    client: httpx.AsyncClient, *, client_id: str, refresh_token: str
) -> dict[str, Any]:
    response = await _request_json(
        client,
        "POST",
        "/auth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    return _token_response(response, forbidden_values=(refresh_token,))


async def _verify_api(
    client: httpx.AsyncClient, access_token: str, *, seed_entity: bool = False
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    private_values = (access_token,)
    configuration = await _request_json(
        client, "GET", "/api/config", headers=headers, reject_echoes=private_values
    )
    if (
        not isinstance(configuration, Mapping)
        or configuration.get("version") != HOME_ASSISTANT_VERSION
    ):
        raise QualificationFailure(
            "version_mismatch", "Home Assistant version differs from the pinned release."
        )
    if seed_entity:
        seeded = await _request_json(
            client,
            "POST",
            f"/api/states/{ENTITY_ID}",
            expected_status=201,
            headers=headers,
            json={
                "state": "1.75",
                "attributes": {
                    "unit_of_measurement": "kW",
                    "friendly_name": "Synthetic authorization qualification power",
                },
            },
            reject_echoes=private_values,
        )
        _validate_synthetic_state(seeded)
    readback = await _request_json(
        client,
        "GET",
        f"/api/states/{ENTITY_ID}",
        headers=headers,
        reject_echoes=private_values,
    )
    _validate_synthetic_state(readback)
    return {"api_config_verified": True, "synthetic_entity_read": True}


def _validate_synthetic_state(value: Any) -> None:
    attributes = value.get("attributes") if isinstance(value, Mapping) else None
    if (
        not isinstance(value, Mapping)
        or value.get("entity_id") != ENTITY_ID
        or value.get("state") != "1.75"
        or not isinstance(attributes, Mapping)
        or attributes.get("unit_of_measurement") != "kW"
        or "state_class" in attributes
    ):
        raise QualificationFailure(
            "api_verification_failed", "Synthetic Home Assistant API state did not match."
        )


async def qualify_existing_user(client: httpx.AsyncClient, base_url: str) -> dict[str, Any]:
    """Exercise login, code exchange, API reads, refresh, and revocation."""

    client_id, redirect_uri = _request_client_urls(base_url)
    username, password, onboarding_code = await _create_existing_user(client, client_id)
    authorization_code = await _login_existing_user(
        client,
        client_id=client_id,
        redirect_uri=redirect_uri,
        username=username,
        password=password,
        onboarding_code=onboarding_code,
    )
    tokens = await _exchange_code(
        client,
        client_id=client_id,
        code=authorization_code,
        forbidden_values=(password, onboarding_code),
    )
    initial_access = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    initial_api = await _verify_api(client, initial_access, seed_entity=True)

    refreshed = await _refresh_token(client, client_id=client_id, refresh_token=refresh_token)
    refreshed_access = refreshed["access_token"]
    refreshed_api = await _verify_api(client, refreshed_access)

    await _request_response(
        client,
        "POST",
        "/auth/revoke",
        data={"token": refresh_token},
        reject_echoes=(refresh_token, initial_access, refreshed_access),
    )
    revoked = await _request_json(
        client,
        "POST",
        "/auth/token",
        expected_status=400,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        reject_echoes=(refresh_token, initial_access, refreshed_access),
    )
    if not isinstance(revoked, Mapping) or revoked.get("error") != "invalid_grant":
        raise QualificationFailure(
            "revocation_failed", "Home Assistant accepted a revoked refresh token."
        )
    await _request_response(
        client,
        "GET",
        "/api/config",
        expected_status=401,
        headers={"Authorization": f"Bearer {refreshed_access}"},
        reject_echoes=(refresh_token, initial_access, refreshed_access),
    )
    return {
        "ok": True,
        "provider": "home-assistant",
        "home_assistant_version": HOME_ASSISTANT_VERSION,
        "image": HOME_ASSISTANT_IMAGE,
        "protocol": {
            "existing_user_login_flow_completed": True,
            "login_flow_authorization_code_exchanged": True,
            "access_token_api_verified": initial_api["api_config_verified"],
            "synthetic_entity_read": initial_api["synthetic_entity_read"],
            "refresh_token_accepted": True,
            "refreshed_access_token_api_verified": refreshed_api["api_config_verified"],
            "refresh_token_revoked": True,
            "revoked_refresh_token_rejected": True,
            "revoked_access_token_rejected": True,
            "browser_consent_driven": False,
        },
        "synthetic_entity": {
            "entity_id": ENTITY_ID,
            "state": "1.75",
            "unit": "kW",
            "state_class": None,
        },
        "cleanup": {
            "owned_container_removed": True,
            "owned_volume_removed": True,
        },
        "qualification_limit": (
            "Ephemeral Home Assistant development owner and synthetic entity; "
            "no browser consent, physical meter, device control, or private-user evidence."
        ),
    }


def _verify_owned_cleanup(container_name: str, volume_name: str, init_name: str) -> bool:
    """Confirm only the exact run-owned containers and volume are absent."""

    try:
        containers = base.docker("ps", "--all", "--quiet", "--filter", f"name=^/{container_name}$")
        volumes = base.docker("volume", "ls", "--quiet", "--filter", f"name=^{volume_name}$")
        initializers = base.docker("ps", "--all", "--quiet", "--filter", f"name=^/{init_name}$")
    except QualificationFailure:
        return False
    return not containers and not volumes and not initializers


def run_qualification(startup_timeout: int = STARTUP_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run against one disposable instance of the pinned Home Assistant image."""

    container_name = base._safe_name("eat-ha-qualification")
    volume_name = container_name
    init_name = f"{container_name}-init"
    if not 30 <= startup_timeout <= 300:
        raise QualificationFailure(
            "invalid_timeout", "Startup timeout must be between 30 and 300 seconds."
        )
    result: dict[str, Any] | None = None
    primary_failure: QualificationFailure | None = None
    primary_interrupt: BaseException | None = None
    primary_interrupt_traceback = None
    try:
        base.docker("pull", HOME_ASSISTANT_IMAGE)
        base.docker("volume", "create", volume_name)
        base.docker(
            *base._configuration_command(HOME_ASSISTANT_IMAGE, volume_name, init_name),
            timeout=60,
        )
        base.docker(
            "run",
            "--detach",
            "--name",
            container_name,
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--publish",
            "127.0.0.1::8123",
            "--mount",
            f"type=volume,source={volume_name},target=/config",
            HOME_ASSISTANT_IMAGE,
        )
        port = base._published_port(container_name)

        async def run() -> dict[str, Any]:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                follow_redirects=False,
            ) as client:
                await base.wait_until_ready(client, startup_timeout)
                return await qualify_existing_user(client, f"http://127.0.0.1:{port}")

        result = asyncio.run(run())
    except QualificationFailure as exc:
        primary_failure = exc
    except Exception:
        primary_failure = QualificationFailure(
            "qualification_failed", "Home Assistant authorization qualification failed safely."
        )
    except BaseException as exc:
        primary_interrupt = exc
        primary_interrupt_traceback = exc.__traceback__

    cleanup_interrupt: BaseException | None = None
    cleanup_interrupt_traceback = None
    try:
        cleanup_command_succeeded = base.cleanup_owned_resources(
            container_name, volume_name, init_name
        )
    except Exception:
        cleanup_command_succeeded = False
    except BaseException as exc:
        cleanup_command_succeeded = False
        cleanup_interrupt = exc
        cleanup_interrupt_traceback = exc.__traceback__
    try:
        resources_verified_absent = _verify_owned_cleanup(container_name, volume_name, init_name)
    except Exception:
        resources_verified_absent = False
    except BaseException as exc:
        resources_verified_absent = False
        if cleanup_interrupt is None:
            cleanup_interrupt = exc
            cleanup_interrupt_traceback = exc.__traceback__
    if primary_interrupt is not None:
        raise primary_interrupt.with_traceback(primary_interrupt_traceback)
    if primary_failure is None and cleanup_interrupt is not None:
        raise cleanup_interrupt.with_traceback(cleanup_interrupt_traceback)
    if (
        primary_failure is not None
        or not cleanup_command_succeeded
        or not resources_verified_absent
    ):
        raise QualificationRunFailure(
            primary_failure,
            container_name=container_name,
            volume_name=volume_name,
            init_name=init_name,
            startup_timeout_seconds=startup_timeout,
            cleanup_command_succeeded=cleanup_command_succeeded,
            resources_verified_absent=resources_verified_absent,
        )
    assert result is not None
    result["resources"] = {
        "container": container_name,
        "volume": volume_name,
        "initializer": init_name,
    }
    result["cleanup"] = {
        "attempted": True,
        "command_succeeded": True,
        "resources_verified_absent": True,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-timeout", type=int, default=STARTUP_TIMEOUT_SECONDS)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = run_qualification(args.startup_timeout)
    except QualificationFailure as exc:
        result = getattr(
            exc,
            "report",
            {"ok": False, "error": {"code": exc.code, "message": exc.message}},
        )
        output = json.dumps(result, sort_keys=True)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(output + "\n", encoding="utf-8")
        print(output)
        raise SystemExit(1) from None
    except Exception:
        result = {
            "ok": False,
            "error": {
                "code": "qualification_failed",
                "message": "Home Assistant authorization qualification failed safely.",
            },
        }
        output = json.dumps(result, sort_keys=True)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(output + "\n", encoding="utf-8")
        print(output)
        raise SystemExit(1) from None
    output = json.dumps(result, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
