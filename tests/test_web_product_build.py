"""Exercise the production web build and authenticated real-gateway journey."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_web_build_and_real_gateway_journey():
    node, npm = shutil.which("node"), shutil.which("npm")
    if node is None or npm is None:
        pytest.skip("Node.js/npm unavailable; run scripts/verify_web_app.py separately")
    version = subprocess.run([node, "--version"], check=True, capture_output=True, text=True)
    parts = version.stdout.strip().removeprefix("v").split(".")
    if tuple(int(part) for part in parts[:2]) < (20, 9):
        pytest.skip("Next.js requires Node.js 20.9 or newer")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/verify_web_app.py")],
        cwd=root,
        env={**os.environ, "NEXT_TELEMETRY_DISABLED": "1"},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
