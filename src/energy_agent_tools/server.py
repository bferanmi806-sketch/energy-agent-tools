from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from .job_contracts import JobCursorToken, JobState
from .models import DataKind, EnergyError, Json, Session
from .providers import format_tools
from .runtime import EnergyAgent
from .skills import SKILLS, search_skills
from .time import day_window


class ExecutionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    arguments: Json = Field(default_factory=dict)
    account_id: str | None = None
    asset_id: str | None = None
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
        "Large outputs are local artifacts. Execute discovered workbench tools with "
        "ENERGY_MULTI_EXECUTE_TOOL, which also accepts a single call. Tool capability tags "
        "describe discovery topics; ENERGY_EXECUTE_CAPABILITY requires a reviewed binding. "
        "Use ENERGY_LIST_SKILLS and ENERGY_RUN_SKILL for composed workflows such as forecast-bill.",
    )

    @server.tool(name="ENERGY_SEARCH_TOOLS")
    async def search_tools(query: str, limit: Annotated[int, Field(ge=1, le=10)] = 5) -> Json:
        """Discover relevant tools with schemas and composed skills.

        Execute canonical tools through ENERGY_MULTI_EXECUTE_TOOL. Capability tags
        are search topics and do not establish a reviewed capability binding.
        """
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
        """Execute discovered canonical tools, including provider reads and workbench analysis.

        Put the canonical tool name in calls[].tool and its schema arguments in
        calls[].arguments. A single call is supported. Calls run in order and
        return per-call failures and provenance. persist=true stores an artifact.
        """
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
        """Find energy workflows, required inputs and pitfalls. Execute a selected workflow with ENERGY_RUN_SKILL."""
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
            local_now = agent.calendar_clock().astimezone(ZoneInfo(site.timezone))
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

    @server.tool(name="ENERGY_RESOLVE_CAPABILITY")
    async def resolve_capability(
        capability: str,
        arguments: Json | None = None,
        asset_id: str | None = None,
        kind: str | None = None,
        account_id: str | None = None,
        unit: str | None = None,
        resolution: str | None = None,
        tool: str | None = None,
        max_age_seconds: Annotated[int | None, Field(ge=1, le=86400)] = None,
    ) -> Json:
        """Rank reviewed available sources by site, asset, account, measurement kind, units and coverage. Ambiguity is explicit."""
        from .capabilities import CapabilityRequest

        try:
            request = CapabilityRequest(
                capability=capability,
                arguments=arguments or {},
                asset_id=asset_id,
                kind=DataKind(kind) if kind else None,
                account_id=account_id,
                unit=unit,
                resolution=resolution,
                tool=tool,
                max_age_seconds=max_age_seconds,
            )
            return agent.resolver.resolve(session, request)
        except (EnergyError, ValueError) as exc:
            return {
                "error": {
                    "code": exc.code if isinstance(exc, EnergyError) else "invalid_request",
                    "message": "Capability request cannot be resolved in this scope.",
                }
            }

    @server.tool(name="ENERGY_EXECUTE_CAPABILITY")
    async def execute_capability(
        capability: str,
        arguments: Json | None = None,
        asset_id: str | None = None,
        kind: str | None = None,
        account_id: str | None = None,
        persist: bool = False,
        unit: str | None = None,
        resolution: str | None = None,
        tool: str | None = None,
        max_age_seconds: Annotated[int | None, Field(ge=1, le=86400)] = None,
        input_artifacts: Annotated[list[str] | None, Field(max_length=10)] = None,
    ) -> Json:
        """Execute a uniquely selected reviewed capability binding through normal policies.

        A tool's capability tags alone do not establish a binding. Use
        ENERGY_MULTI_EXECUTE_TOOL for discovered workbench and other canonical
        tools. Current-power freshness applies to current observations; retrieve
        historical samples with a history tool and its explicit time window.
        Never substitutes incompatible schemas.
        """
        from .capabilities import CapabilityRequest

        try:
            request = CapabilityRequest(
                capability=capability,
                arguments=arguments or {},
                asset_id=asset_id,
                kind=DataKind(kind) if kind else None,
                account_id=account_id,
                unit=unit,
                resolution=resolution,
                tool=tool,
                max_age_seconds=max_age_seconds,
                input_artifacts=input_artifacts or [],
            )
            return await agent.resolver.execute(session, request, persist)
        except (EnergyError, ValueError) as exc:
            return {
                "ok": False,
                "error": {
                    "code": exc.code if isinstance(exc, EnergyError) else "invalid_request",
                    "message": "Capability request is invalid or outside this scope.",
                },
            }

    @server.tool(name="ENERGY_RUN_SKILL")
    async def run_skill(skill_id: str, parameters: Json | None = None) -> Json:
        """Run a listed executable skill. Inspect ENERGY_LIST_SKILLS first.

        parameters.arguments maps capability IDs to provider arguments only.
        parameters.tools and parameters.account_ids select canonical sources.
        Preloaded measured counters or power can use consumption_transform with
        operation counter or integrate_power and explicit operation parameters.
        Conversion precedes window selection and remains calculated evidence.
        Cost workflows accept billing with standing_charge, tax and source for
        complete local-day windows; tariff-comparison needs alternative_billing too.
        start/end are offset-aware ranges; artifacts maps capabilities to scoped
        artifact IDs. Battery workflows require battery constraints; weather-based
        solar estimation requires solar model inputs. Missing inputs stay explicit.
        """
        from .workflows import run_skill as execute_skill

        return await execute_skill(agent, session, skill_id, parameters or {})

    @server.tool(name="ENERGY_SIMULATION_JOB")
    async def simulation_job(
        operation: Literal[
            "submit", "list", "status", "result", "cancel", "delete", "resume", "start"
        ],
        job_id: str | None = None,
        simulation: Literal[
            "heat_loss", "power_flow", "battery", "solar", "network_power_flow", "network_dispatch"
        ]
        | None = None,
        arguments: Json | None = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
        before: JobCursorToken | None = None,
        status: JobState | None = None,
    ) -> Json:
        """Submit bounded local numerical jobs and inspect, cancel or delete scoped results.

        heat_loss uses engineering.calculate_heat_loss; power_flow uses
        engineering.run_power_flow; battery uses engineering.schedule_battery_charging;
        solar uses engineering.estimate_solar_generation; network_power_flow uses
        pypsa.power_flow; network_dispatch uses pypsa.optimize_dispatch. Submit the arguments
        from that tool's schema. For provider reads and artifact
        analysis, use ENERGY_EXECUTE_CAPABILITY or ENERGY_MULTI_EXECUTE_TOOL.
        List discovers safe history across originating sessions under the current actor,
        workspace, site, mode and toolkit grants. Use limit, before and status only for list.
        Result and controls recover the original scope after checking current access.
        Start explicitly queues only a saved pending job after current calculation authorization.
        Interrupted or finished work cannot be restarted. Resume returns scope without rerunning work. No executable or path inputs.
        """
        return await agent.job(
            session,
            operation,
            job_id=job_id,
            simulation=simulation,
            arguments=arguments,
            limit=limit,
            before=before,
            status=status,
        )

    return server


async def provider_tools(
    server: FastMCP, provider: Literal["openai", "openai-responses", "anthropic"]
) -> list[Json]:
    """The same search-first helpers can be passed to provider function calling."""
    tools = await server.list_tools()
    return format_tools(
        [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in tools
        ],
        provider,
    )
