from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from energy_agent_tools import cli
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.models import AuthConfig, ConnectedAccount


def _invoke(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["energy-agent", *arguments])
    cli.main()


def test_bootstrap_prints_once_and_repeated_bootstrap_preserves_prior_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"

    _invoke(monkeypatch, "bootstrap", "--state-dir", str(state), "--name", "Home")
    first_output = capsys.readouterr().out
    first = json.loads(first_output)
    assert first_output.count(first["management_key"]["token"]) == 1
    _invoke(monkeypatch, "bootstrap", "--state-dir", str(state), "--name", "Home")
    second = json.loads(capsys.readouterr().out)

    assert first["ok"] and second["ok"]
    assert first["workspace"]["mode"] == "managed"
    assert first["workspace"]["name"] == "Home"
    assert first["owner"]["name"] == "Home owner"
    assert first["owner"]["id"] != second["owner"]["id"]
    assert first["workspace"]["id"] != second["workspace"]["id"]
    assert first["management_key"]["token"].startswith("eat_")
    assert second["management_key"]["token"] != first["management_key"]["token"]

    controls = ControlStore(state / "control")
    try:
        first_identity = controls.authenticate(first["management_key"]["token"])
        second_identity = controls.authenticate(second["management_key"]["token"])
        assert first_identity is not None
        assert second_identity is not None
        assert first_identity.user_id == first["owner"]["id"]
        assert second_identity.user_id == second["owner"]["id"]
        assert first_identity.key_id == first["management_key"]["id"]
    finally:
        controls.close()

    key_path = state / "vault.key"
    assert key_path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    for path in state.rglob("*"):
        if path.is_file():
            assert first["management_key"]["token"].encode() not in path.read_bytes()
            assert second["management_key"]["token"].encode() not in path.read_bytes()


def test_bootstrap_rejects_malformed_key_without_publishing_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    marker = b"this-is-not-a-vault-key"
    (state / "vault.key").write_bytes(marker)

    with pytest.raises(SystemExit) as error:
        _invoke(monkeypatch, "bootstrap", "--state-dir", str(state))

    output = capsys.readouterr().out
    assert error.value.code == 1
    assert json.loads(output) == {"ok": False, "error": "bootstrap_failed"}
    assert marker not in output.encode()
    if (state / "control" / "control.sqlite3").exists():
        db = sqlite3.connect(state / "control" / "control.sqlite3")
        try:
            assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        finally:
            db.close()


def test_bootstrap_rejects_malformed_control_state_before_creating_vault_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    control = state / "control"
    control.mkdir(parents=True)
    (control / "control.sqlite3").write_bytes(b"not sqlite")

    with pytest.raises(SystemExit) as error:
        _invoke(monkeypatch, "bootstrap", "--state-dir", str(state))

    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "bootstrap_failed"}
    assert not (state / "vault.key").exists()


def test_managed_host_uses_generated_vault_and_explicit_host_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from energy_agent_tools import hosting

    captured: dict[str, Any] = {}
    real_create_host = hosting.create_host

    def capture_create_host(agent, principals, **options):
        captured["has_auth_store"] = agent.auth_store is not None
        captured["has_control_store"] = options.get("control_store") is not None
        captured["managed_workspaces"] = options.get("managed_workspaces")
        return real_create_host(agent, principals, **options)

    monkeypatch.setattr(hosting, "create_host", capture_create_host)
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)

    _invoke(
        monkeypatch,
        "host",
        "--state-dir",
        str(tmp_path / "state"),
        "--managed-workspaces",
    )

    assert capsys.readouterr().out == ""
    assert captured == {
        "has_auth_store": True,
        "has_control_store": True,
        "managed_workspaces": True,
    }
    assert (tmp_path / "state" / "vault.key").is_file()
    assert not (tmp_path / "state" / "profile.json").exists()


def test_managed_host_does_not_bypass_an_explicit_environment_key_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import energy_agent_tools.hosting as hosting

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"vault": {"master_key_env": "MISSING_TEST_VAULT_KEY"}}))
    monkeypatch.delenv("MISSING_TEST_VAULT_KEY", raising=False)
    calls = 0

    def unexpected_create_host(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("create_host must not run without the configured key")

    monkeypatch.setattr(hosting, "create_host", unexpected_create_host)
    with pytest.raises(SystemExit) as error:
        _invoke(
            monkeypatch,
            "host",
            "--state-dir",
            str(tmp_path / "state"),
            "--config",
            str(config),
            "--managed-workspaces",
        )

    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "managed_host_configuration_failed",
    }
    assert calls == 0
    assert not (tmp_path / "state" / "vault.key").exists()


def test_managed_host_does_not_create_an_explicitly_configured_missing_key_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"vault": {"master_key_file": "chosen.key"}}))
    state = tmp_path / "state"

    with pytest.raises(SystemExit) as error:
        _invoke(
            monkeypatch,
            "host",
            "--state-dir",
            str(state),
            "--config",
            str(config),
            "--managed-workspaces",
        )

    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "managed_host_configuration_failed",
    }
    assert not (state / "chosen.key").exists()
    assert not (state / "vault.key").exists()


def test_bootstrap_rejects_valid_wrong_key_for_existing_encrypted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    correct_key = Fernet.generate_key()
    vault = AuthStore(state / "vault", correct_key)
    try:
        vault.configure(
            ConnectedAccount(
                id="legacy",
                user_id="owner",
                toolkit="fixture",
                site_id="home",
                auth=AuthConfig(scheme="bearer"),
            ),
            "existing-private-secret",
        )
    finally:
        vault.close()
    (state / "vault.key").write_bytes(Fernet.generate_key())

    with pytest.raises(SystemExit) as error:
        _invoke(monkeypatch, "bootstrap", "--state-dir", str(state))

    output = capsys.readouterr().out
    assert error.value.code == 1
    assert json.loads(output) == {"ok": False, "error": "bootstrap_failed"}
    assert "existing-private-secret" not in output
    controls = ControlStore(state / "control")
    try:
        db = sqlite3.connect(controls.path)
        try:
            assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        finally:
            db.close()
    finally:
        controls.close()


def test_managed_host_rejects_wrong_key_before_starting_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    correct_key = Fernet.generate_key()
    vault = AuthStore(state / "vault", correct_key)
    vault.configure(
        ConnectedAccount(
            id="legacy", user_id="owner", toolkit="fixture", auth=AuthConfig(scheme="bearer")
        ),
        "existing-private-secret",
    )
    vault.close()
    (state / "vault.key").write_bytes(Fernet.generate_key())
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: calls.append(True))
    with pytest.raises(SystemExit) as error:
        _invoke(monkeypatch, "host", "--state-dir", str(state), "--managed-workspaces")
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"ok": False, "error": "managed_host_failed"}
    assert "existing-private-secret" not in output
    assert not calls
    reopened = AuthStore(state / "vault", correct_key)
    try:
        assert reopened.credential("owner", "legacy") == "existing-private-secret"
    finally:
        reopened.close()


@pytest.mark.parametrize("invalid", [False, True])
def test_host_loads_only_valid_deployment_oauth_configurations(
    tmp_path, monkeypatch, capsys, invalid
):
    import uvicorn

    import energy_agent_tools.hosting as hosting

    configuration = {
        "id": "home",
        "name": "Home",
        "base_url": "https://home.example.test",
        "client_id": "https://energy.example.test",
        "redirect_uri": "https://energy.example.test/api/workspace/oauth/callback",
    }
    if invalid:
        configuration["base_url"] = "http://unapproved.example.test"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"hosting": {"managed_oauth_configurations": [configuration]}}))
    captured = []
    original = hosting.create_host

    def capture(agent, principals, **options):
        captured.extend(options.get("managed_oauth_configurations", ()))
        return original(agent, principals, **options)

    monkeypatch.setattr(hosting, "create_host", capture)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)
    arguments = (
        "host",
        "--state-dir",
        str(tmp_path / "state"),
        "--config",
        str(config),
        "--managed-workspaces",
    )
    if invalid:
        with pytest.raises(SystemExit):
            _invoke(monkeypatch, *arguments)
        assert captured == []
        assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "managed_host_failed"}
    else:
        _invoke(monkeypatch, *arguments)
        assert captured[0].provider().protocol == "home_assistant"
        assert captured[0].redirect_uri == configuration["redirect_uri"]
        assert capsys.readouterr().out == ""
