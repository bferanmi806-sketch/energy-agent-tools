"""Production managed-workspace REST host for the TypeScript SDK acceptance test.

The fixture uses the real host, control store, encrypted auth store, and agent
builder. Its Octopus transport is synthetic and never opens a provider socket.
The one-time management key is written to a private temporary file for the
Node test and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import httpx
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.auth import AuthStore
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host


def write_private_token(path: Path, token: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(token)


def create_app(state_dir: Path, token_file: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    control = ControlStore(state_dir / "control")
    bootstrap = control.bootstrap_workspace("SDK fixture owner", "SDK fixture workspace")
    write_private_token(token_file, bootstrap.key.token)

    vault = AuthStore(state_dir / "vault", Fernet.generate_key())
    agent = build_agent(state_dir / "agent")

    async def synthetic_octopus(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.octopus.energy" or not request.url.path.endswith(
            "/consumption/"
        ):
            return httpx.Response(404, json={"detail": "Synthetic provider route not found."})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "consumption": 1.25,
                        "interval_start": "2026-10-02T00:00:00Z",
                        "interval_end": "2026-10-02T00:30:00Z",
                    }
                ]
            },
            request=request,
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(synthetic_octopus))
    asyncio.run(agent.http.aclose())
    agent.http = upstream
    agent._owns_http = False
    agent.auth_store = vault
    host = create_host(
        agent,
        {},
        close_agent_on_shutdown=True,
        control_store=control,
        managed_workspaces=True,
        max_requests_per_minute=200,
    )
    return host, control, agent, upstream


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()

    import uvicorn

    host, control, agent, upstream = create_app(args.state_dir, args.token_file)
    try:
        uvicorn.run(
            host,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            access_log=False,
        )
    finally:
        asyncio.run(agent.close())
        asyncio.run(upstream.aclose())
        control.close()


if __name__ == "__main__":
    main()
