"""Run the developer SDK acceptance when the JavaScript runtime is available."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_typescript_sdk_builds_and_runs_real_gateway_acceptance():
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        pytest.skip("Node.js/npm unavailable; run scripts/verify_typescript_sdk.py separately")
    version = subprocess.run([node, "--version"], check=True, capture_output=True, text=True)
    if int(version.stdout.strip().removeprefix("v").split(".")[0]) < 20:
        pytest.skip("TypeScript SDK requires Node.js 20 or newer")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/verify_typescript_sdk.py")],
        cwd=root,
        env={**os.environ, "ENERGY_AGENT_TEST_PYTHON": sys.executable},
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
