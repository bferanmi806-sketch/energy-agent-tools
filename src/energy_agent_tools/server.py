from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from .models import EnergyError, Json, Session
from .providers import format_tools
from .runtime import EnergyAgent
from .skills import SKILLS, search_skills
from .time import day_window


class ExecutionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    arguments: Json = Field(default_factory=dict)
    account_id: str | None = None
    persist: bool = False
    input_artifacts: Annotated[list[str], Field(max_length=10)] = Field(default_factory=list)


def create_server(agent: EnergyAgent, session: Session, *, port: int = 8765) -> FastMCP:
    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await agent.close()

    server = FastMCP(
        "Energy Agent Tools",
        host="127.0.0.1",
        port=port,
        lifespan=lifespan,
        instructions="Search first. Never confuse metered, calculated, estimated, simulated or forecast data. "
        "Credentials are configured locally; do not send them in tool arguments. "
        "Large outputs are local artifacts. Use workbench tools for analysis.",
    )

    @server.tool(name="ENERGY_SEARCH_TOOLS")
    async def search_tools(query: str, limit: Annotated[int, Field(ge=1, le=10)] = 5) -> Json:
        """Discover a bounded set of relevant tools with schemas and workflow guidance."""
        try:
            return {"tools": agent.search(session, query, limit), "skills": search_skills(query)}
        except EnergyError as exc:
            return {"error": {"code": exc.code, "message": exc.message}}

    @server.tool(name="ENERGY_GET_TOOL")
    async def get_tool(name: str) -> Json:
        """Get one tool's exact input schema, capabilities, action policy and semantics."""
        try:
            return agent.get_tool(session, name)
        except EnergyError as exc:
            return {"error": {"code": exc.code, "message": exc.message}}

    @server.tool(name="ENERGY_MANAGE_CONNECTIONS")
    async def manage_connections(
        operation: Literal["list", "select"] = "list",
        toolkit: str | None = None,
        account_id: str | None = None,
    ) -> Json:
        """List safe account metadata or select a configured account. Provision secrets outside agent context."""
        try:
            if operation == "select":
                if toolkit is None or account_id is None:
                    raise EnergyError(
                        "invalid_selection", "Selecting requires toolkit and account_id."
                    )
                return {"selected": agent.select_account(session, toolkit, account_id)}
            return {"connections": agent.connections(session)}
        except EnergyError as exc:
            return {"error": {"code": exc.code, "message": exc.message}}

    @server.tool(name="ENERGY_MULTI_EXECUTE_TOOL")
    async def multi_execute(
        calls: Annotated[list[ExecutionCall], Field(min_length=1, max_length=20)],
    ) -> Json:
        """Execute ordered independent calls; return structured per-call failures and provenance. persist=true stores a local artifact."""
        try:
            return {"results": await agent.multi_execute(session, [c.model_dump() for c in calls])}
        except EnergyError as exc:
            return {"error": {"code": exc.code, "message": exc.message}}

    @server.tool(name="ENERGY_LIST_TOOLKITS")
    async def list_toolkits() -> Json:
        """List connector runtimes, availability and credential requirements without dumping action schemas."""
        return {"toolkits": agent.catalogue(session)}

    @server.tool(name="ENERGY_LIST_SKILLS")
    async def list_skills(query: str | None = None) -> Json:
        """Read energy workflow sequences and engineering pitfalls."""
        return {"skills": search_skills(query) if query else SKILLS}

    @server.tool(name="ENERGY_SITE_CONTEXT")
    async def site_context() -> Json:
        """List this user's sites/assets and active timezone so date windows can respect DST."""
        sites = [
            s
            for s in agent.sites.values()
            if s.user_id == session.user_id and (session.site_id is None or s.id == session.site_id)
        ]
        ids = {s.id for s in sites}
        contexts = []
        for site in sites:
            local_now = datetime.now(ZoneInfo(site.timezone))
            start, end = day_window(local_now.date() - timedelta(days=1), site.timezone)
            contexts.append(
                {
                    **site.model_dump(mode="json"),
                    "local_now": local_now.isoformat(),
                    "yesterday_utc_window": {"start": start.isoformat(), "end": end.isoformat()},
                }
            )
        return {
            "sites": contexts,
            "assets": [
                a.model_dump(mode="json") for a in agent.assets.values() if a.site_id in ids
            ],
            "active_site_id": session.site_id,
        }

    return server


async def provider_tools(
    server: FastMCP, provider: Literal["openai", "openai-responses", "anthropic"]
) -> list[Json]:
    """The same seven search-first helpers can be passed to provider function calling."""
    tools = await server.list_tools()
    return format_tools(
        [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in tools
        ],
        provider,
    )
