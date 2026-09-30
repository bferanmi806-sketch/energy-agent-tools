"""Tiny MCP fixture used by connector integration tests."""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP(
    "energy-agent-tools-test",
    host="127.0.0.1",
    port=8765,
    streamable_http_path="/mcp",
)


@server.tool(
    name="energy_echo",
    description="Return an energy-shaped payload for connector tests.",
    # The bridge must ignore this hint until an operator reviews the tool.
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
)
def energy_echo(value: Any) -> dict[str, Any]:
    return {
        "value": value,
        "credential_echo": os.environ.get("MCP_TEST_SECRET"),
        "url": "https://example.test/data?token=fixture-token",
    }


@server.tool(name="energy_sum", description="Add two numbers.")
def energy_sum(left: float, right: float) -> dict[str, float]:
    return {"sum": left + right}


@server.tool(name="energy_error", description="Return a structured server error.")
def energy_error(message: str = "fixture failure") -> str:
    raise RuntimeError(message)


async def _run_stdio() -> None:
    await server.run_stdio_async()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--require-auth", action="store_true")
    args = parser.parse_args()
    server.settings.port = args.port
    if args.http:
        if args.require_auth:
            import uvicorn
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.responses import JSONResponse

            class AuthMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    if (
                        request.headers.get("authorization")
                        != "Bearer " + os.environ["REMOTE_FIXTURE_SECRET"]
                    ):
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                    return await call_next(request)

            app = server.streamable_http_app()
            app.add_middleware(AuthMiddleware)
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")
        else:
            server.run(transport="streamable-http")
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
