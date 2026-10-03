"""Loopback web acceptance host using the production registry and synthetic sites.

The caller supplies a temporary bearer key via ENERGY_WEB_TEST_TOKEN. No private
provider credentials are loaded. Catalogue browsing does not execute providers.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from energy_agent_tools.app import build_agent
from energy_agent_tools.hosting import Principal, create_host, token_digest


def create_app(root: Path):
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
    agent = build_agent(root, config)
    return create_host(
        agent,
        {
            "web-test": Principal(
                "web-test", {"synthetic-home", "synthetic-workshop"}, token_digest(token)
            )
        },
        close_agent_on_shutdown=True,
    )


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Synthetic loopback web acceptance gateway")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    uvicorn.run(create_app(args.state_dir), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
