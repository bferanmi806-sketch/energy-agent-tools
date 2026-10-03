"""Loopback web acceptance host using the production registry and synthetic sites.

The caller supplies a temporary bearer key via ENERGY_WEB_TEST_TOKEN. No private
provider credentials are loaded. Catalogue browsing does not execute providers.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import os
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.connection_onboarding import OctopusConnectionService
from energy_agent_tools.control_contracts import AgentKeyAccess
from energy_agent_tools.hosting import Principal, create_host, token_digest


def create_app(root: Path, *, octopus_fixture: bool = False, agent_key: bool = False):
    token = os.environ.get("ENERGY_WEB_TEST_TOKEN")
    if not token:
        raise ValueError("A temporary acceptance token is required.")
    config = {
        "sites": [
            {
                "id": "synthetic-home",
                "user_id": "web-test",
                "name": "Synthetic home",
                "timezone": "Europe/London",
            },
            {
                "id": "synthetic-workshop",
                "user_id": "web-test",
                "name": "Synthetic workshop",
                "timezone": "UTC",
            },
        ],
        "assets": [
            {
                "id": "synthetic-meter",
                "site_id": "synthetic-home",
                "name": "Synthetic meter",
                "kind": "meter",
            },
        ],
    }
    provider_key = os.environ.get("ENERGY_WEB_TEST_OCTOPUS_KEY")
    if octopus_fixture or agent_key:
        if not provider_key:
            raise ValueError("A fictional Octopus fixture key is required.")
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        key_path = root / "vault.key"
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
            key_path.chmod(0o600)
        config["vault"] = {"master_key_file": "vault.key"}
    agent = build_agent(root, config)
    if octopus_fixture or agent_key:
        expected = "Basic " + base64.b64encode(f"{provider_key}:".encode()).decode()

        def fake_provider(request: httpx.Request) -> httpx.Response:
            if request.url.host != "api.octopus.energy":
                return httpx.Response(503)
            if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "consumption": 1.25,
                            "interval_start": "2026-09-30T00:00:00Z",
                            "interval_end": "2026-09-30T00:30:00Z",
                        }
                    ],
                    "next": None,
                },
            )

        asyncio.run(agent.http.aclose())
        agent.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_provider))
    if agent_key:
        asyncio.run(
            OctopusConnectionService(agent.auth_store, agent.http).connect(
                user_id="web-test",
                site=agent.sites["synthetic-home"],
                credential=provider_key,
                mpan="1234567890123",
                serial_number="TEST123",
            )
        )
        principal = Principal(
            "web-test",
            {"synthetic-home"},
            token_digest(token),
            key_access=AgentKeyAccess(site_ids=["synthetic-home"]),
        )
    else:
        principal = Principal(
            "web-test", {"synthetic-home", "synthetic-workshop"}, token_digest(token)
        )
    return create_host(
        agent,
        {"web-test": principal},
        close_agent_on_shutdown=True,
        # The scripted acceptance makes many requests in seconds; production keeps 60/min.
        max_requests_per_minute=300,
    )


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Synthetic loopback web acceptance gateway")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--octopus-fixture",
        action="store_true",
        help="Use fictional Octopus responses; never qualification of a private account",
    )
    parser.add_argument(
        "--agent-key",
        action="store_true",
        help="Use a static home-only read-only agent principal with a fictional Octopus connection",
    )
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.state_dir, octopus_fixture=args.octopus_fixture, agent_key=args.agent_key),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
