"""Run an offline deployment smoke test without provider credentials.

This is used by ``docker compose --profile smoke run --rm deployment-smoke``
and can also be run with ``uv run python scripts/deployment_smoke.py``.  It
creates one artifact and one encrypted connection, snapshots the state, restores
it into a new directory, and reads both records after the restore.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import AuthConfig, ConnectedAccount, DataKind, EnergyResult, Session
from energy_agent_tools.workbench import Workbench


def _account() -> ConnectedAccount:
    return ConnectedAccount(
        id="smoke-meter",
        user_id="smoke-user",
        site_id="smoke-site",
        toolkit="octopus-energy-account",
        auth=AuthConfig(scheme="bearer"),
        settings={"base_url": "https://provider.example"},
    )


def run_smoke(work_root: Path) -> dict[str, object]:
    state = work_root / "state"
    restored = work_root / "restored"
    archive = work_root / "state.tar.gz"
    session = Session(id="smoke-session", user_id="smoke-user", site_id="smoke-site")
    artifact = Workbench(state).persist(
        session,
        EnergyResult(
            data=[{"timestamp": "2026-09-30T00:00:00Z", "kwh": 2.0}],
            kind=DataKind.METERED,
            unit="kWh",
            source="offline-smoke",
        ),
    )
    key = Fernet.generate_key()
    store = AuthStore(state / "vault", key)
    store.configure(_account(), "offline-smoke-credential")
    store.close()

    manifest = create_backup(state, archive)
    restore_backup(archive, restored)
    restored_artifact = Workbench(restored).read(session, str(artifact["artifact_id"]))
    restored_store = AuthStore(restored / "vault", key)
    credential = restored_store.credential("smoke-user", "smoke-meter", "smoke-site")
    restored_store.close()
    if restored_artifact.data != [{"timestamp": "2026-09-30T00:00:00Z", "kwh": 2.0}]:
        raise RuntimeError("restored artifact did not match the source")
    if credential != "offline-smoke-credential":
        raise RuntimeError("restored encrypted connection could not be read")
    return {
        "ok": True,
        "files": [item.path for item in manifest.files],
        "artifact_id": artifact["artifact_id"],
        "archive_bytes": archive.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-root",
        type=Path,
        help="Use a new empty directory for the smoke run; defaults to a temporary directory.",
    )
    args = parser.parse_args()
    if args.work_root is None:
        with tempfile.TemporaryDirectory(prefix="energy-agent-smoke-") as name:
            print(json.dumps(run_smoke(Path(name)), sort_keys=True))
        return
    work_root = args.work_root
    if work_root.exists():
        raise SystemExit("--work-root must not already exist")
    work_root.mkdir(parents=True)
    try:
        print(json.dumps(run_smoke(work_root), sort_keys=True))
    finally:
        # Keep caller-owned output when an explicit path was requested.
        pass


if __name__ == "__main__":
    main()
