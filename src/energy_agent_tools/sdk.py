"""Bound SDK sessions reuse the same helper contracts as MCP."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from .activity import ExecutionLogQuery, ExecutionLogScope
from .app import build_agent, configure_mcp
from .capabilities import CapabilityRequest
from .models import Json, Session
from .runtime import EnergyAgent
from .server import create_server, provider_tools


class BoundSession:
    def __init__(self, agent: EnergyAgent, context: Session):
        self.agent = agent
        self.context = context
        self._server = create_server(agent, context)

    @property
    def id(self) -> str:
        return self.context.id

    async def tools(
        self, provider: Literal["openai", "openai-responses", "anthropic"] = "openai-responses"
    ) -> list[Json]:
        return await provider_tools(self._server, provider)

    def search(self, query: str, limit: int = 5) -> list[Json]:
        return self.agent.search(self.context, query, limit)

    def resolve(self, capability: str, **kwargs: Any) -> Json:
        return self.agent.resolver.resolve(
            self.context, CapabilityRequest(capability=capability, **kwargs)
        )

    async def execute(self, tool: str, arguments: Json, **kwargs: Any) -> Json:
        return await self.agent.execute(self.context, tool, arguments, **kwargs)

    async def capability(
        self,
        capability: str,
        arguments: Json | None = None,
        *,
        persist: bool = False,
        **kwargs: Any,
    ) -> Json:
        return await self.agent.resolver.execute(
            self.context,
            CapabilityRequest(capability=capability, arguments=arguments or {}, **kwargs),
            persist,
        )

    async def skill(self, skill_id: str, parameters: Json | None = None) -> Json:
        from .workflows import run_skill

        return await run_skill(self.agent, self.context, skill_id, parameters or {})

    def activity(self, *, limit: int = 50, before: int | None = None) -> Json:
        self.agent._scope(self.context)
        query = ExecutionLogQuery(limit=limit, before=before)
        scope = ExecutionLogScope(
            user_id=self.context.user_id,
            workspace_id=self.context.workspace_id,
            access_mode=self.context.access_mode,
            site_ids={self.context.site_id},
            connection_ids=self.context.connection_grants,
        )
        return self.agent.execution_activity(
            scope, limit=query.limit, before=query.before
        ).model_dump(mode="json")

    async def job(self, operation: str, **kwargs: Any) -> Json:
        return await self.agent.job(self.context, operation, **kwargs)

    async def dispatch(self, name: str, arguments: Json) -> Any:
        """Dispatch a provider function call; helper names are identical across providers."""
        from .providers import resolve_provider_name

        names = [t.name for t in await self._server.list_tools()]
        canonical = resolve_provider_name([{"name": n} for n in names], name)
        result = await self._server.call_tool(canonical, arguments)
        if isinstance(result, tuple):
            return result[1]
        return result


class EnergyAgentTools:
    def __init__(
        self,
        root: Path | str,
        config: Json | None = None,
        *,
        data_root: Path | None = None,
        agent: EnergyAgent | None = None,
    ):
        self.agent = agent or build_agent(Path(root), config, data_root=data_root)
        self._config = config or {}
        self._initialized = agent is not None or not self._config.get("mcp_servers")

    async def initialize(self) -> EnergyAgentTools:
        if not self._initialized:
            await configure_mcp(self.agent, self._config)
            self._initialized = True
        return self

    def session(self, user_id: str, site_id: str | None = None, **kwargs: Any) -> BoundSession:
        if not self._initialized:
            raise RuntimeError(
                "Initialize configured MCP imports using async with or initialize()."
            )
        return BoundSession(self.agent, self.agent.session(user_id, site_id, **kwargs))

    async def close(self) -> None:
        await self.agent.close()

    async def __aenter__(self) -> EnergyAgentTools:
        try:
            return await self.initialize()
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
