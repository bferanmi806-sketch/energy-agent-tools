from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.maintenance import (
    MaintenanceError,
    create_backup,
    inspect_backup,
    restore_backup,
)
from energy_agent_tools.models import AuthConfig, ConnectedAccount, DataKind, EnergyResult, Session
from energy_agent_tools.workbench import Workbench


def _account() -> ConnectedAccount:
    return ConnectedAccount(
        id="meter-home",
        user_id="alice",
        site_id="home",
        toolkit="octopus-energy-account",
        auth=AuthConfig(scheme="bearer"),
        settings={"base_url": "https://provider.example"},
    )


def _state(tmp_path: Path) -> tuple[Path, bytes, str]:
    state = tmp_path / "state"
    workbench = Workbench(state)
    session = Session(id="session-1", user_id="alice", site_id="home")
    result = EnergyResult(
        data=[{"timestamp": "2026-09-30T00:00:00Z", "kwh": 1.25}],
        kind=DataKind.METERED,
        unit="kWh",
        source="fixture",
    )
    artifact_id = workbench.persist(session, result)["artifact_id"]
    key = Fernet.generate_key()
    store = AuthStore(state / "vault", key)
    store.configure(_account(), "credential-never-in-archive")
    store.close()
    return state, key, artifact_id


def test_backup_restore_preserves_artifacts_and_encrypted_connections(tmp_path: Path) -> None:
    state, key, artifact_id = _state(tmp_path)
    archive = tmp_path / "state.tar.gz"

    manifest = create_backup(state, archive)

    assert archive.stat().st_mode & 0o777 == 0o600
    assert {item.path for item in manifest.files} == {"artifacts.sqlite3", "vault/auth.sqlite3"}
    assert not manifest.vault_key_included
    assert b"credential-never-in-archive" not in archive.read_bytes()

    restored = tmp_path / "restored"
    restored_manifest = restore_backup(archive, restored)
    assert restored_manifest == inspect_backup(archive)
    artifact = Workbench(restored).read(
        Session(id="session-1", user_id="alice", site_id="home"), artifact_id
    )
    assert artifact.data == [{"timestamp": "2026-09-30T00:00:00Z", "kwh": 1.25}]
    restored_store = AuthStore(restored / "vault", key)
    assert restored_store.credential("alice", "meter-home", "home") == "credential-never-in-archive"
    restored_store.close()


def test_vault_key_is_only_included_when_explicit(tmp_path: Path) -> None:
    state, key, _ = _state(tmp_path)
    archive = tmp_path / "with-key.tar.gz"
    manifest = create_backup(state, archive, vault_key=key, include_vault_key=True)

    assert manifest.vault_key_included
    assert {item.path for item in manifest.files} == {
        "artifacts.sqlite3",
        "vault/auth.sqlite3",
        "vault.key",
    }
    inspection = inspect_backup(archive)
    assert inspection == manifest

    with pytest.raises(MaintenanceError, match="include_vault_key"):
        create_backup(state, tmp_path / "rejected.tar.gz", vault_key=key)


def test_restore_refuses_existing_target_and_keeps_it_untouched(tmp_path: Path) -> None:
    state, _, _ = _state(tmp_path)
    archive = tmp_path / "state.tar.gz"
    create_backup(state, archive)
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep")

    with pytest.raises(MaintenanceError, match="overwrite"):
        restore_backup(archive, target)
    assert marker.read_text() == "keep"


def test_restore_rejects_path_traversal_and_symlink_members(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.tar"
    with tarfile.open(traversal, "w") as archive:
        payload = b"{}"
        info = tarfile.TarInfo("../escape")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with pytest.raises(MaintenanceError, match="member path"):
        inspect_backup(traversal)

    symlink = tmp_path / "symlink.tar"
    with tarfile.open(symlink, "w") as archive:
        info = tarfile.TarInfo("artifacts.sqlite3")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(MaintenanceError, match="regular files"):
        inspect_backup(symlink)


def test_restore_rejects_hash_mismatch_and_oversized_members(tmp_path: Path) -> None:
    hash_archive = tmp_path / "bad-hash.tar"
    manifest = {
        "format": "energy-agent-tools-state",
        "version": 1,
        "created_at": "2026-10-01T00:00:00+00:00",
        "vault_key_included": False,
        "files": [
            {
                "path": "artifacts.sqlite3",
                "kind": "artifacts",
                "size": 3,
                "sha256": "0" * 64,
                "schema": "workbench.v1",
            }
        ],
    }
    with tarfile.open(hash_archive, "w") as archive:
        data = b"abc"
        info = tarfile.TarInfo("artifacts.sqlite3")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        encoded = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(encoded)
        archive.addfile(info, io.BytesIO(encoded))
    with pytest.raises(MaintenanceError, match="hash"):
        inspect_backup(hash_archive)

    oversized = tmp_path / "oversized.tar"
    info = tarfile.TarInfo("artifacts.sqlite3")
    info.size = 2_000_000_001
    oversized.write_bytes(info.tobuf() + b"\0" * 1024)
    with pytest.raises(MaintenanceError, match="too large"):
        inspect_backup(oversized)


def test_backup_rejects_unrecognized_database_and_symlink(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "artifacts.sqlite3") as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(MaintenanceError, match="schema"):
        create_backup(state, tmp_path / "invalid.tar.gz")

    valid_state, _, _ = _state(tmp_path / "valid")
    os.remove(valid_state / "artifacts.sqlite3")
    os.symlink("/etc/passwd", valid_state / "artifacts.sqlite3")
    with pytest.raises(MaintenanceError, match="regular file"):
        create_backup(valid_state, tmp_path / "symlink-state.tar.gz")


def test_backup_requires_nonexistent_archive_and_timezone_clock(tmp_path: Path) -> None:
    state, _, _ = _state(tmp_path)
    archive = tmp_path / "state.tar.gz"
    create_backup(state, archive)
    with pytest.raises(MaintenanceError, match="already exists"):
        create_backup(state, archive)
    with pytest.raises(MaintenanceError, match="timezone"):
        create_backup(state, tmp_path / "naive.tar.gz", clock=datetime.now)


@pytest.mark.asyncio
async def test_complete_local_profile_and_job_restore_without_original_state(
    tmp_path: Path,
) -> None:
    import shutil

    from energy_agent_tools.app import build_agent
    from energy_agent_tools.jobs import JobManager
    from energy_agent_tools.onboarding import LocalProfile

    state = tmp_path / "original"
    profile = LocalProfile(state)
    profile.create_site("Home", "UTC", site_id="home")
    account = profile.configure_connection(
        "home_assistant",
        credential="local-only-fixture-secret",
        site_id="home",
        metadata={"base_url": "http://127.0.0.1:18123"},
    )
    profile.close()
    agent = build_agent(state)
    session = agent.session("local", "home")
    result = await agent.job(
        session,
        "submit",
        simulation="heat_loss",
        arguments={
            "indoor_temp_c": 21,
            "outdoor_temp_c": 2,
            "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
            "air_changes_per_hour": 0.4,
        },
    )
    job_id = result["job"]["job_id"]
    await agent._job_task
    await agent.close()
    archive = tmp_path / "full.tar.gz"
    manifest = create_backup(
        state, archive, vault_key=(state / "vault.key").read_bytes(), include_vault_key=True
    )
    assert {"profile.json", "vault.key", "jobs/jobs.sqlite3", f"jobs/{job_id}/output.json"} <= {
        f.path for f in manifest.files
    }
    restored = tmp_path / "restored-profile"
    restore_backup(archive, restored)
    shutil.rmtree(state)
    agent = build_agent(restored)
    try:
        assert (
            agent.auth_store.credential("local", account.id, "home") == "local-only-fixture-secret"
        )
        resumed = agent.session("local", "home", id=session.id)
        answer = await agent.job(resumed, "result", job_id=job_id)
        assert answer["ok"], answer
        assert answer["result"]["data"]["gross_heat_loss_kw"] == pytest.approx(0.38)
    finally:
        await agent.close()
    # The restored manager lock is reusable once the agent closes.
    manager = JobManager(restored / "jobs")
    manager.close()
