"""Build and exercise the real web app and gateway without changing workflows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    npm = shutil.which("npm")
    if npm is None:
        print("npm is required for web acceptance.")
        return 1
    environment = {
        **os.environ,
        "ENERGY_WEB_TEST_PYTHON": sys.executable,
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    for folder, arguments in (
        ("packages/typescript", ["ci", "--ignore-scripts"]),
        ("packages/typescript", ["run", "build"]),
        ("apps/web", ["ci", "--ignore-scripts"]),
        ("apps/web", ["run", "build"]),
        ("apps/web", ["run", "check"]),
        ("apps/web", ["test"]),
    ):
        result = subprocess.run([npm, *arguments], cwd=root / folder, env=environment, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
