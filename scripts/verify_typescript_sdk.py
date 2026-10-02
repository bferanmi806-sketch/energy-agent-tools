"""Verify generated contracts and the TypeScript SDK against the real gateway."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def verify(*, install: bool = True) -> None:
    root = Path(__file__).resolve().parents[1]
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("Install Node.js 20 or newer and npm to verify the SDK.")
    subprocess.run(
        [sys.executable, str(root / "scripts/export_typescript_contracts.py"), "--check"],
        cwd=root,
        check=True,
    )
    package = root / "packages/typescript"
    environment = {**os.environ, "ENERGY_AGENT_TEST_PYTHON": sys.executable}
    if install:
        subprocess.run([npm, "ci", "--ignore-scripts"], cwd=package, env=environment, check=True)
    subprocess.run([npm, "test"], cwd=package, env=environment, check=True)
    subprocess.run([npm, "run", "check"], cwd=package, env=environment, check=True)


if __name__ == "__main__":
    verify()
