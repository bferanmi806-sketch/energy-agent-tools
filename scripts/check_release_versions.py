"""Reject a release whose gateway, SDK, web or lock metadata disagree."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    gateway = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    versions = {"gateway": gateway}
    lock = tomllib.loads((root / "uv.lock").read_text())
    versions["gateway lock"] = next(
        package["version"] for package in lock["package"] if package["name"] == "energy-agent-tools"
    )
    for directory in ("packages/typescript", "apps/web"):
        for filename in ("package.json", "package-lock.json"):
            package = json.loads((root / directory / filename).read_text())
            versions[f"{directory}/{filename}"] = package["version"]
            if filename == "package-lock.json":
                versions[f"{directory} lock root"] = package["packages"][""]["version"]
                if directory == "apps/web":
                    versions["web SDK lock"] = package["packages"]["../../packages/typescript"][
                        "version"
                    ]
    client = re.search(
        r'name: "energy-agent-tools-typescript", version: "([^"]+)"',
        (root / "packages/typescript/src/mcp.ts").read_text(),
    )
    if client is None:
        raise SystemExit("SDK MCP client version metadata was not found")
    versions["SDK MCP client"] = client.group(1)
    mismatches = {name: version for name, version in versions.items() if version != gateway}
    if mismatches:
        raise SystemExit(f"Release versions disagree with gateway {gateway}: {mismatches}")
    print(f"Gateway, SDK, web and lock metadata agree: {gateway}")


if __name__ == "__main__":
    main()
