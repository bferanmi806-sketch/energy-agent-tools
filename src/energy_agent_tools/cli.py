from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .app import build_agent, load_config
from .server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-hosted energy tool gateway")
    parser.add_argument("command", choices=["serve", "catalogue", "manifests"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path(".energy-agent"))
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=Path("manifests"))
    args = parser.parse_args()
    config = load_config(args.config)
    data_root = Path(config["data_root"]).resolve() if config.get("data_root") else None
    agent = build_agent(args.state_dir, config, data_root=data_root)
    session = agent.session(
        config.get("user_id", os.environ.get("ENERGY_USER_ID", "local")), config.get("site_id")
    )
    if config.get("mcp_servers"):
        # Imported MCP registry setup needs the same asyncio loop as server startup.
        from .connectors.mcp_bridge import import_mcp

        async def setup() -> None:
            for spec in config["mcp_servers"]:
                await import_mcp(agent.registry, **spec)

        asyncio.run(setup())
    if args.command == "catalogue":
        print(json.dumps(agent.catalogue(session), indent=2))
        asyncio.run(agent.close())
    elif args.command == "manifests":
        agent.write_manifests(args.output)
        asyncio.run(agent.close())
    else:
        create_server(agent, session, port=args.port).run(transport=args.transport)
