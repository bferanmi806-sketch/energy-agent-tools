from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = (
    "home_energy.py",
    "building_energy.py",
    "network_engineering.py",
)


@pytest.mark.parametrize("entrypoint", PROJECTS)
def test_reference_project_runs_its_real_entrypoint(entrypoint: str) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "src"), str(ROOT), environment.get("PYTHONPATH", ""))
    )
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "reference_projects" / entrypoint),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert '"project":' in process.stdout
