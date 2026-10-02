"""Read and restore real v0.2.0 workbench/vault state with the current package.

Install the published v0.2.0 wheel in a separate environment and pass its
interpreter as --baseline-python. The old package creates the source databases;
the current package reads them, backs them up and restores them. This does not
qualify REST session recovery, hardware connections or every operator config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from importlib.metadata import version
from pathlib import Path

from energy_agent_tools.auth import AuthStore
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import EnergyError, Session
from energy_agent_tools.workbench import Workbench

BASELINE_WHEEL_SHA256 = "9da16b10b7f6a51db8c3c7b8d4e82f47f91a471ed1965d0e260589cd06bd31e1"

# The subprocess imports the installed baseline package, never this checkout.
# Development keys and credentials are generated inside the child and written
# only to private files. They are absent from argv, stdout and evidence.
_SEED = """
import json, os, secrets, sys
from importlib.metadata import version
from pathlib import Path
from cryptography.fernet import Fernet
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.models import AuthConfig, ConnectedAccount, DataKind, EnergyResult, Session
from energy_agent_tools.workbench import Workbench

if version("energy-agent-tools") != "0.2.0":
    raise RuntimeError("wrong baseline package")
root = Path(sys.argv[1])
root.mkdir(mode=0o700)
key = Fernet.generate_key()
credential = secrets.token_urlsafe(32)
for path, value in ((root / "vault.key", key), (root.parent / "expected-credential", credential.encode())):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(value)
session = Session(id="upgrade-session", user_id="upgrade-user", site_id="upgrade-site")
artifact = Workbench(root).persist(session, EnergyResult(
    data=[{"timestamp":"2026-09-29T00:00:00Z","value":1.0},
          {"timestamp":"2026-09-29T00:30:00Z","value":2.25}],
    kind=DataKind.METERED, unit="kWh", source="v020-upgrade-fixture", resolution="30min",
    provenance=[{"fixture":"upgrade", "physical_meter":False}],
))
store = AuthStore(root / "vault", key)
store.configure(ConnectedAccount(id="upgrade-account", user_id="upgrade-user",
    site_id="upgrade-site", toolkit="home-assistant", auth=AuthConfig(scheme="bearer")), credential)
store.close()
print(json.dumps({"baseline_version":version("energy-agent-tools"), "artifact_id":artifact["artifact_id"]}))
"""


def run_upgrade(baseline_python: Path, baseline_wheel: Path, root: Path) -> dict:
    digest = hashlib.sha256(baseline_wheel.read_bytes()).hexdigest()
    if digest != BASELINE_WHEEL_SHA256:
        raise RuntimeError("Baseline wheel differs from the published v0.2.0 digest.")
    state = root / "state"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
    }
    completed = subprocess.run(
        [str(baseline_python.absolute()), "-I", "-c", _SEED, str(state)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("The installed v0.2.0 baseline could not create upgrade state.")
    metadata = json.loads(completed.stdout)
    session = Session(id="upgrade-session", user_id="upgrade-user", site_id="upgrade-site")
    key = (state / "vault.key").read_bytes()
    credential = (root / "expected-credential").read_text()

    def check(location: Path) -> None:
        workbench = Workbench(location)
        result = workbench.read(session, metadata["artifact_id"])
        if result.source != "v020-upgrade-fixture" or result.quantity_shape is not None:
            raise RuntimeError("Legacy artifact semantics changed after upgrade.")
        if workbench.summarize(session, metadata["artifact_id"], "value").data["sum"] != 3.25:
            raise RuntimeError("Legacy energy total changed after upgrade.")
        try:
            workbench.read(Session(user_id="outsider"), metadata["artifact_id"])
        except EnergyError:
            pass
        else:
            raise RuntimeError("Artifact ownership failed after upgrade.")
        store = AuthStore(location / "vault", key)
        try:
            if store.credential("upgrade-user", "upgrade-account", "upgrade-site") != credential:
                raise RuntimeError("Legacy encrypted credential changed after upgrade.")
            try:
                store.credential("outsider", "upgrade-account", "upgrade-site")
            except EnergyError:
                pass
            else:
                raise RuntimeError("Vault ownership failed after upgrade.")
        finally:
            store.close()

    check(state)
    archive = root / "upgrade-state.tar.gz"
    manifest = create_backup(state, archive, include_vault_key=True, vault_key=key)
    restored = root / "restored"
    restore_backup(archive, restored)
    shutil.rmtree(state)
    check(restored)
    return {
        "ok": True,
        "baseline_version": metadata["baseline_version"],
        "current_version": version("energy-agent-tools"),
        "published_baseline_wheel_sha256": digest,
        "checks": {
            "old_artifact_read": True,
            "legacy_optional_quantity_shape": True,
            "independent_sum_kwh": 3.25,
            "encrypted_credential_read": True,
            "ownership_enforced": True,
            "restored_after_original_deleted": True,
        },
        "backup_files": [file.path for file in manifest.files],
        "scope": "v0.2.0 workbench and vault compatibility; synthetic development data",
        "limitations": "No REST session migration, live provider access or arbitrary operator configuration qualification.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", type=Path, required=True)
    parser.add_argument("--baseline-wheel", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="eat-upgrade-") as directory:
        evidence = run_upgrade(args.baseline_python, args.baseline_wheel, Path(directory))
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
