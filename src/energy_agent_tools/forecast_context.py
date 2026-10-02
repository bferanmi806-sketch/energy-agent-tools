"""Resolve optional historical and future temperature through the scoped gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .capabilities import CapabilityRequest
from .models import DataKind, EnergyError, Json, Session
from .temperature_alignment import align_temperature_context
from .windows import instant

if TYPE_CHECKING:
    from .runtime import EnergyAgent


async def fetch_forecast_context(
    agent: EnergyAgent,
    session: Session,
    *,
    history_start: str,
    history_end: str,
    forecast_start: str,
    forecast_end: str,
    interval_minutes: int,
    arguments: dict[str, Json],
    account_ids: dict[str, str],
    tools: dict[str, str],
) -> tuple[dict[str, str], Json]:
    attempts: list[Json] = []
    references: dict[str, str] = {}
    try:
        if not session.site_id:
            raise EnergyError("site_required", "Temperature context requires a known site.")
        site = agent.sites[session.site_id]
        for role, capability, left, right in (
            ("historical_context", "get_historical_weather", history_start, history_end),
            ("future_context", "get_weather", forecast_start, forecast_end),
        ):
            # Include the preceding hour for bounded hold at non-hourly site midnights.
            fetch_start = instant(left).replace(minute=0, second=0, microsecond=0).isoformat()
            args = dict(arguments.get(capability, {}))
            for key, expected in (("start", fetch_start), ("end", right)):
                if key in args and instant(args[key]) != instant(expected):
                    raise EnergyError(
                        "conflicting_time_window",
                        "Context arguments conflict with the workflow window.",
                    )
                args[key] = expected
            request = CapabilityRequest(
                capability=capability,
                arguments=args,
                account_id=account_ids.get(capability),
                tool=tools.get(capability),
                kind=DataKind.FORECAST if role == "future_context" else None,
            )
            resolution = agent.resolver.resolve(session, request)
            selected = resolution["selected"]
            if selected is None:
                attempts.append({"role": role, "resolution": resolution})
                raise EnergyError(
                    "capability_" + resolution["status"],
                    "No compatible temperature source is selected.",
                )
            for coordinate in ("latitude", "longitude"):
                if coordinate in selected["arguments"]:
                    expected = getattr(site, coordinate)
                    if expected is None or selected["arguments"][coordinate] != expected:
                        raise EnergyError(
                            "context_location_mismatch",
                            "Weather coordinates must match the selected site.",
                        )
            retrieved = await agent.resolver.execute(
                session,
                request.model_copy(
                    update={"tool": selected["tool"], "account_id": selected["account_id"]}
                ),
                persist=True,
            )
            attempts.append({"role": role, "retrieval": retrieved})
            if not retrieved["ok"]:
                raise EnergyError(retrieved["error"]["code"], retrieved["error"]["message"])
            ref = retrieved["result"]["data"]["artifact_id"]
            source = agent.workbench.read(session, ref)
            if source.site_id != session.site_id:
                raise EnergyError(
                    "context_site_mismatch", "Temperature context must belong to the selected site."
                )
            if role == "historical_context" and source.kind not in {
                DataKind.METERED,
                DataKind.ESTIMATED,
            }:
                raise EnergyError(
                    "invalid_context",
                    "Historical weather must be observed or estimated analysis, not forecast.",
                )
            if role == "future_context" and source.kind != DataKind.FORECAST:
                raise EnergyError(
                    "invalid_context", "Future temperature must remain forecast data."
                )
            aligned = align_temperature_context(
                (ref, source),
                start=left,
                end=right,
                interval_minutes=interval_minutes,
            )
            stored = agent.workbench.persist(session, aligned)
            references[role] = stored["artifact_id"]
            attempts.append({"role": role, "alignment": stored, "kind": aligned.kind.value})
        return references, {"status": "available", "attempts": attempts}
    except EnergyError as exc:
        # A partial pair cannot condition the model. Retain its acquisition evidence.
        return {}, {
            "status": "unavailable",
            "attempts": attempts,
            "error": {"code": exc.code, "message": exc.message},
        }
