"""Reviewed capability bindings, with explicit provider schemas and availability."""

from __future__ import annotations

from datetime import datetime
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator, model_validator

from .models import ConnectedAccount, DataKind, EnergyError, Json, Session, StrictModel

if TYPE_CHECKING:
    from .runtime import EnergyAgent


class CapabilityBinding(StrictModel):
    capability: str
    tool: str
    account_id: str | None = None
    asset_id: str | None = None
    kind: DataKind | None = None
    unit: str | None = None
    quantity_shape: Literal["interval", "instantaneous", "counter"] | None = None
    resolution: str | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    quality: str = "unknown"
    preference: int = Field(default=0, ge=-100, le=100)
    defaults: Json = Field(default_factory=dict)
    fixed_arguments: Json = Field(default_factory=dict)
    argument_map: dict[str, str] = Field(default_factory=dict)
    required_arguments_or_settings: list[str] = Field(default_factory=list, max_length=20)
    reviewed: bool = False
    version: str = "1.0.0"

    @field_validator("defaults", "fixed_arguments")
    @classmethod
    def nonsecret_arguments(cls, value: Json) -> Json:
        return ConnectedAccount.nonsecret_settings(value)

    @model_validator(mode="after")
    def coverage_is_aware(self) -> CapabilityBinding:
        for edge in (self.coverage_start, self.coverage_end):
            if edge is not None and (edge.tzinfo is None or edge.utcoffset() is None):
                raise ValueError("Capability coverage requires offset-aware timestamps.")
        if self.coverage_start and self.coverage_end and self.coverage_end < self.coverage_start:
            raise ValueError("Capability coverage end precedes start.")
        return self


class CapabilityRequest(StrictModel):
    capability: str = Field(min_length=1, max_length=100)
    arguments: Json = Field(default_factory=dict)
    asset_id: str | None = None
    account_id: str | None = None
    kind: DataKind | None = None
    unit: str | None = None
    resolution: str | None = None
    tool: str | None = None
    max_age_seconds: int | None = Field(default=None, ge=1, le=86400)
    input_artifacts: list[str] = Field(default_factory=list, max_length=10)


def builtins(agent: EnergyAgent) -> list[CapabilityBinding]:
    definitions = [
        (
            "estimate_forecast_bill",
            "analytics.estimate_forecast_bill",
            DataKind.CALCULATED,
            None,
            None,
        ),
        (
            "forecast_energy_consumption",
            "analytics.forecast_consumption",
            DataKind.FORECAST,
            "kWh",
            None,
        ),
        (
            "explain_consumption_spike",
            "analytics.explain_consumption_spike",
            DataKind.CALCULATED,
            None,
            None,
        ),
        (
            "analyse_grid_conditions",
            "analytics.analyse_grid_conditions",
            DataKind.CALCULATED,
            "mixed",
            None,
        ),
        ("get_carbon_intensity", "carbon_intensity_gb.get_intensity", None, "gCO2/kWh", "30min"),
        (
            "get_energy_consumption",
            "octopus_energy.get_consumption",
            DataKind.METERED,
            "kWh",
            None,
        ),
        ("get_weather", "open_meteo.get_forecast", DataKind.FORECAST, None, "1h"),
        (
            "get_historical_weather",
            "open_meteo.get_historical_temperature",
            DataKind.ESTIMATED,
            "degC",
            "1h",
        ),
        ("get_tariff", "octopus_energy.get_tariffs", DataKind.CALCULATED, "p/kWh", None),
        (
            "estimate_solar_generation",
            "engineering.estimate_solar_generation",
            DataKind.ESTIMATED,
            None,
            None,
        ),
        ("run_power_flow", "engineering.run_power_flow", DataKind.SIMULATED, None, None),
        ("run_power_flow", "opendss.power_flow", DataKind.SIMULATED, None, None),
        ("run_power_flow", "pypsa.power_flow", DataKind.SIMULATED, "MW, Mvar, pu, degree", None),
        ("run_pipe_flow", "pandapipes.pipeflow", DataKind.SIMULATED, "bar, K, kg/s, m/s", None),
        (
            "estimate_wind_generation",
            "windpowerlib.estimate_generation",
            DataKind.ESTIMATED,
            "kW and kWh",
            None,
        ),
        (
            "plan_battery_charging",
            "engineering.schedule_battery_charging",
            DataKind.SIMULATED,
            None,
            None,
        ),
        ("calculate_heat_loss", "engineering.calculate_heat_loss", DataKind.CALCULATED, "W", None),
    ]
    bindings = [
        CapabilityBinding(
            capability=c,
            tool=t,
            kind=k,
            unit=u,
            resolution=r,
            reviewed=True,
            required_arguments_or_settings=(
                ["product_code", "tariff_code"] if t == "octopus_energy.get_tariffs" else []
            ),
        )
        for c, t, k, u, r in definitions
        if t in agent.registry.tools
    ]
    for binding in (
        CapabilityBinding(
            capability="get_carbon_intensity",
            tool="electricitymaps.get_signal",
            unit="gCO2eq/kWh",
            fixed_arguments={"signal": "carbon-intensity"},
            reviewed=True,
        ),
        CapabilityBinding(
            capability="get_grid_load",
            tool="entsoe.get_timeseries",
            kind=DataKind.METERED,
            unit="MW",
            fixed_arguments={"document_type": "A65", "process_type": "A16"},
            reviewed=True,
        ),
        CapabilityBinding(
            capability="get_grid_generation",
            tool="elexon.get_grid_data",
            kind=DataKind.METERED,
            unit="MW",
            resolution="30min",
            fixed_arguments={"dataset": "FUELHH"},
            reviewed=True,
        ),
    ):
        if binding.tool in agent.registry.tools:
            bindings.append(binding)
    return bindings


class CapabilityResolver:
    def __init__(
        self,
        agent: EnergyAgent,
        bindings: list[CapabilityBinding] | None = None,
        *,
        defer_unknown_tools: bool = False,
    ):
        self.agent = agent
        self._configured_bindings = builtins(agent) + list(bindings or [])
        self._defer_unknown_tools = defer_unknown_tools
        self.refresh_account_bindings()

    def refresh_account_bindings(self) -> None:
        """Refresh stored account mappings while preserving operator bindings."""
        agent = self.agent
        bindings = list(self._configured_bindings)
        for account in agent.accounts.values():
            for raw in account.settings.get("capability_bindings", []):
                binding = CapabilityBinding.model_validate(raw)
                binding.account_id = account.id
                bindings.append(binding)
        if self._defer_unknown_tools:
            bindings = [b for b in bindings if b.tool in agent.registry.tools]
        for binding in bindings:
            tool = agent.registry.get(binding.tool)
            if binding.account_id:
                bound_account = agent.accounts.get(binding.account_id)
                if bound_account is None or bound_account.toolkit != tool.toolkit:
                    raise ValueError("Capability binding must reference an account of its toolkit.")
            if binding.asset_id and binding.asset_id not in agent.assets:
                raise ValueError("Capability binding references an unknown asset.")
            if binding.asset_id and binding.account_id:
                asset = agent.assets[binding.asset_id]
                account = agent.accounts[binding.account_id]
                if account.site_id != asset.site_id or (
                    asset.account_ids and account.id not in asset.account_ids
                ):
                    raise ValueError(
                        "Capability account must belong to the bound asset's site and accounts."
                    )

        self.bindings = bindings

    def scoped_capabilities(self, session: Session) -> dict[str, list[str]]:
        """Publish reviewed role names only inside their account and asset scope."""

        self.agent._scope(session)
        self.agent._sync_connections(session.user_id, session.workspace_id)
        capabilities: dict[str, set[str]] = {}
        for binding in self.bindings:
            tool = self.agent.registry.get(binding.tool)
            if not binding.reviewed or not tool.reviewed:
                continue
            if not self.agent._tool_visible(session, tool):
                continue
            if binding.account_id:
                account = self.agent.accounts.get(binding.account_id)
                if (
                    account is None
                    or account.user_id != session.user_id
                    or account.workspace_id != session.workspace_id
                    or (session.site_id and account.site_id != session.site_id)
                ):
                    continue
                selected = session.account_ids.get(tool.toolkit)
                if selected and selected != account.id:
                    continue
            if binding.asset_id:
                asset = self.agent.assets[binding.asset_id]
                if self.agent.sites[asset.site_id].user_id != session.user_id or (
                    session.site_id and asset.site_id != session.site_id
                ):
                    continue
            capabilities.setdefault(tool.name, set()).add(binding.capability)
        return {name: sorted(values) for name, values in capabilities.items()}

    def resolve(self, session: Session, request: CapabilityRequest) -> Json:
        self.agent._scope(session)
        self.agent._sync_connections(session.user_id, session.workspace_id)
        if request.asset_id:
            asset = self.agent.assets.get(request.asset_id)
            if (
                asset is None
                or self.agent.sites[asset.site_id].user_id != session.user_id
                or (session.site_id and asset.site_id != session.site_id)
            ):
                raise EnergyError("asset_forbidden", "Asset is outside this session's site scope.")
        candidates: list[Json] = []
        bound = {b.tool for b in self.bindings if b.capability == request.capability}
        definitions = [b for b in self.bindings if b.capability == request.capability]
        definitions += [
            CapabilityBinding(capability=request.capability, tool=t.name)
            for t in self.agent.registry.tools.values()
            if request.capability in t.capabilities and t.name not in bound
        ]
        for binding in definitions:
            if binding.account_id:
                owner_account = self.agent.accounts.get(binding.account_id)
                if (
                    owner_account is None
                    or owner_account.user_id != session.user_id
                    or (session.site_id and owner_account.site_id != session.site_id)
                ):
                    continue
            tool = self.agent.registry.get(binding.tool)
            if request.tool and request.tool != tool.name:
                continue
            if not self.agent._tool_visible(session, tool):
                continue
            if binding.asset_id:
                binding_asset = self.agent.assets[binding.asset_id]
                if self.agent.sites[binding_asset.site_id].user_id != session.user_id or (
                    session.site_id and binding_asset.site_id != session.site_id
                ):
                    continue
                if request.asset_id and binding.asset_id != request.asset_id:
                    continue
            accounts = [
                a
                for a in self.agent.accounts.values()
                if a.toolkit == tool.toolkit
                and a.user_id == session.user_id
                and a.workspace_id == session.workspace_id
                and (session.site_id is None or a.site_id == session.site_id)
            ]
            if binding.account_id:
                accounts = [a for a in accounts if a.id == binding.account_id]
            selected = request.account_id or session.account_ids.get(tool.toolkit)
            if selected:
                accounts = [a for a in accounts if a.id == selected]
            selected_asset_id = binding.asset_id or request.asset_id
            if selected_asset_id:
                asset = self.agent.assets[selected_asset_id]
                accounts = [
                    a
                    for a in accounts
                    if a.site_id == asset.site_id
                    and (not asset.account_ids or a.id in asset.account_ids)
                ]
            options: list[Any] = accounts or [None]
            for account in options:
                reasons: list[str] = []
                if any(find_spec(dependency) is None for dependency in tool.dependencies):
                    reasons.append("dependency_unavailable")
                if not binding.reviewed or not tool.reviewed:
                    reasons.append("measurement_or_argument_mapping_requires_review")
                if not tool.actions <= session.allowed_actions:
                    reasons.append("policy_denied")
                if self.agent.registry.toolkits[tool.toolkit].status == "unavailable":
                    reasons.append("provider_unavailable")
                if account is None and (
                    self.agent.registry.toolkits[tool.toolkit].auth_required
                    or tool.resource_scope == "account"
                ):
                    reasons.append("connection_required")
                if (selected or binding.account_id) and account is None:
                    reasons.append("account_unavailable")
                if account is not None:
                    if not account.enabled or account.state != "active":
                        reasons.append("connection_inactive")
                    elif not self.agent.credential_available(account):
                        reasons.append("credential_missing")
                if request.kind is not None and request.kind != binding.kind:
                    reasons.append("measurement_kind_incompatible")
                if request.unit is not None and request.unit != binding.unit:
                    reasons.append("unit_incompatible")
                if request.resolution and request.resolution != binding.resolution:
                    reasons.append("resolution_incompatible")
                for key, edge in (("start", binding.coverage_start), ("end", binding.coverage_end)):
                    if edge and request.arguments.get(key):
                        raw = request.arguments[key]
                        if not isinstance(raw, str):
                            raise EnergyError(
                                "invalid_arguments",
                                "Coverage timestamps require offset-aware ISO strings.",
                            )
                        try:
                            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        except ValueError:
                            raise EnergyError(
                                "invalid_arguments",
                                "Coverage timestamps require offset-aware ISO strings.",
                            ) from None
                        if value.tzinfo is None or edge.tzinfo is None:
                            reasons.append("coverage_requires_offset")
                        elif (key == "start" and value < edge) or (key == "end" and value > edge):
                            reasons.append("outside_time_coverage")
                args = dict(binding.defaults)
                for key, value in request.arguments.items():
                    args[binding.argument_map.get(key, key)] = value
                if account:
                    args = {
                        **account.settings.get("capability_defaults", {}).get(
                            request.capability, {}
                        ),
                        **args,
                    }
                for key, value in binding.fixed_arguments.items():
                    if key in args and args[key] != value:
                        reasons.append("arguments_incompatible_with_capability")
                    else:
                        args[key] = value
                if session.site_id and request.capability in {
                    "get_weather",
                    "get_historical_weather",
                }:
                    site = self.agent.sites[session.site_id]
                    if site.latitude is not None and "latitude" in tool.input_schema.get(
                        "properties", {}
                    ):
                        args.setdefault("latitude", site.latitude)
                    if site.longitude is not None and "longitude" in tool.input_schema.get(
                        "properties", {}
                    ):
                        args.setdefault("longitude", site.longitude)
                for key in binding.required_arguments_or_settings:
                    required_value = args.get(key) or (
                        account.settings.get(key) if account else None
                    )
                    if required_value is None or required_value == "":
                        reasons.append("arguments_or_settings_required")
                try:
                    self.agent._validate(tool, args)
                except EnergyError:
                    reasons.append("arguments_required")
                score = binding.preference + (20 if binding.reviewed else 0)
                score += 10 if account and account.last_verified_at else 0
                score += 5 if binding.quality in {"verified", "complete", "metered"} else 0
                score += 5 if self.agent.registry.toolkits[tool.toolkit].status == "stable" else 0
                score += 10 if binding.asset_id == request.asset_id and request.asset_id else 0
                candidates.append(
                    {
                        "tool": tool.name,
                        "account_id": account.id if account else None,
                        "asset_id": binding.asset_id or request.asset_id,
                        "kind": binding.kind,
                        "unit": binding.unit,
                        "resolution": binding.resolution,
                        "quantity_shape": binding.quantity_shape,
                        "quality": binding.quality,
                        "binding_version": binding.version,
                        "available": not reasons,
                        "reasons": reasons,
                        "score": score,
                        "arguments": args,
                        "fixed_arguments": binding.fixed_arguments,
                        "input_schema": self.agent.get_tool(session, tool.name)["input_schema"],
                    }
                )
        candidates.sort(
            key=lambda c: (not c["available"], -c["score"], c["tool"], c["account_id"] or "")
        )
        available = [c for c in candidates if c["available"]]
        unique = bool(available) and (
            len(available) == 1 or available[0]["score"] > available[1]["score"]
        )
        self.agent._event(
            {
                "type": "capability_resolution",
                "user_id": session.user_id,
                "session_id": session.id,
                "capability": request.capability,
                "status": "resolved" if unique else "ambiguous" if available else "unavailable",
                "candidate_count": len(candidates),
                "available_count": len(available),
                "tool": available[0]["tool"] if unique else None,
                "account_id": available[0]["account_id"] if unique else None,
            }
        )
        return self.agent._redact(
            {
                "capability": request.capability,
                "candidates": candidates[:20],
                "selected": available[0] if unique else None,
                "status": "resolved" if unique else "ambiguous" if available else "unavailable",
                "note": "Reviewed bindings retain provider schemas. Choose explicitly when candidates tie.",
            },
            self.agent._secrets(session.user_id),
        )

    async def execute(
        self, session: Session, request: CapabilityRequest, persist: bool = False
    ) -> Json:
        resolution = self.resolve(session, request)
        selected = resolution["selected"]
        if selected is None:
            return {
                "ok": False,
                "error": {
                    "code": "capability_" + resolution["status"],
                    "message": "Select a compatible source or configure a reviewed binding.",
                },
                "resolution": resolution,
            }
        return await self.agent.execute(
            session,
            selected["tool"],
            selected["arguments"],
            selected["account_id"],
            persist,
            expected_kind=selected["kind"],
            expected_unit=selected["unit"],
            asset_id=selected["asset_id"],
            expected_resolution=selected["resolution"],
            expected_quantity_shape=selected["quantity_shape"],
            max_age_seconds=request.max_age_seconds
            or (300 if request.capability == "get_current_power" else None),
            expected_arguments=selected["fixed_arguments"],
            input_artifacts=request.input_artifacts,
        )
