"""History-to-consumption-forecast and bill estimation through the gateway."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import Field

from .capabilities import CapabilityRequest
from .models import DataKind, EnergyError, Json, Session, StrictModel
from .windows import bounds, instant

if TYPE_CHECKING:
    from .runtime import EnergyAgent


class ForecastWorkflowRequest(StrictModel):
    start: str | None = None
    end: str | None = None
    days: int = Field(default=8, ge=1, le=31)
    history_months: int = Field(default=3, ge=1, le=24)
    history_start: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    arguments: dict[str, Json] = Field(default_factory=dict)
    account_ids: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, str] = Field(default_factory=dict)
    asset_id: str | None = None
    forecast: Json = Field(default_factory=dict)
    billing: Json | None = None


def _timestamp_column(result: Json) -> str:
    rows = result.get("data")
    if not isinstance(rows, list) or not rows:
        raise EnergyError("insufficient_data", "A source must contain interval rows.")
    for column in ("timestamp", "from", "time", "interval_start"):
        if column in rows[0]:
            return column
    raise EnergyError("timestamp_required", "Source rows require an explicit timestamp.")


def _end_column(result: Json) -> str:
    rows = result.get("data")
    if isinstance(rows, list) and rows:
        for column in ("end", "to", "interval_end"):
            if column in rows[0]:
                return column
    raise EnergyError("interval_end_required", "Source rows require explicit interval ends.")


async def run_forecast_skill(
    agent: EnergyAgent, session: Session, skill_id: str, parameters: Json
) -> Json:
    evidence: list[Json] = []
    try:
        agent._scope(session)
        request = ForecastWorkflowRequest.model_validate(parameters)
        if not session.site_id:
            raise EnergyError("site_required", "Choose a site for forecast calendar and billing.")
        if request.billing is not None and skill_id != "forecast-bill":
            raise EnergyError("invalid_skill_parameters", "Billing applies only to forecast-bill.")
        if set(request.artifacts) - {
            "get_energy_consumption",
            "get_tariff",
            "historical_context",
            "future_context",
        }:
            raise EnergyError("invalid_skill_parameters", "Unknown forecast source role.")
        site = agent.sites[session.site_id]
        zone = ZoneInfo(site.timezone)
        if request.start is None and request.end is None:
            tomorrow = agent.calendar_clock().astimezone(zone).date() + timedelta(days=1)
            start = pd.Timestamp(tomorrow).tz_localize(zone)
            end = pd.Timestamp(tomorrow + timedelta(days=request.days)).tz_localize(zone)
        elif request.start is not None and request.end is not None:
            start_bound, end_bound = bounds(request.start, request.end)
            start, end = (
                pd.Timestamp(start_bound).tz_convert(zone),
                pd.Timestamp(end_bound).tz_convert(zone),
            )
        else:
            raise EnergyError("time_window_required", "Supply both future start and end.")
        history_start = (
            pd.Timestamp(instant(request.history_start)).tz_convert(zone)
            if request.history_start
            else start - pd.DateOffset(months=request.history_months)
        )
        bounds(history_start.isoformat(), start.isoformat())
        if end - start > pd.Timedelta(days=32):
            raise EnergyError(
                "invalid_time_range", "Forecast workflows cover at most 31 local days."
            )

        async def source_single(capability: str, source_start: str, source_end: str) -> str:
            if capability in request.artifacts:
                ref = request.artifacts[capability]
                source_value = agent.workbench.read(session, ref).model_dump(mode="json")
                if capability == "get_tariff":
                    # Tariff validity may start before the forecast horizon.
                    evidence.append({"capability": capability, "artifact_id": ref})
                    return ref
                selected = await agent.execute(
                    session,
                    "WORKBENCH_WINDOW",
                    {
                        "artifact_id": ref,
                        "start": source_start,
                        "end": source_end,
                        "timestamp": _timestamp_column(source_value),
                    },
                    input_artifacts=[ref],
                    persist=True,
                )
                evidence.append({"capability": capability, "window": selected})
                if not selected["ok"]:
                    raise EnergyError(selected["error"]["code"], selected["error"]["message"])
                return selected["result"]["data"]["artifact_id"]
            args = dict(request.arguments.get(capability, {}))
            if capability == "get_energy_consumption":
                for key, expected in (
                    ("start", history_start.isoformat()),
                    ("end", start.isoformat()),
                ):
                    supplied = args.pop(key, None)
                    if supplied is not None and instant(supplied) != instant(expected):
                        raise EnergyError(
                            "conflicting_time_window",
                            "Source arguments conflict with the full history window.",
                        )
            for key, value in (("start", source_start), ("end", source_end)):
                if key in args and instant(args[key]) != instant(value):
                    raise EnergyError(
                        "conflicting_time_window",
                        "Source arguments conflict with forecast windows.",
                    )
                args[key] = value
            resolved = await agent.resolver.execute(
                session,
                CapabilityRequest(
                    capability=capability,
                    arguments=args,
                    account_id=request.account_ids.get(capability),
                    tool=request.tools.get(capability),
                    asset_id=request.asset_id if capability == "get_energy_consumption" else None,
                    kind=DataKind.METERED if capability == "get_energy_consumption" else None,
                ),
                persist=True,
            )
            evidence.append({"capability": capability, **resolved})
            if not resolved["ok"]:
                raise EnergyError(resolved["error"]["code"], resolved["error"]["message"])
            ref = resolved["result"]["data"]["artifact_id"]
            if capability == "get_energy_consumption":
                original = agent.workbench.read(session, ref).model_dump(mode="json")
                selected = await agent.execute(
                    session,
                    "WORKBENCH_WINDOW",
                    {
                        "artifact_id": ref,
                        "start": source_start,
                        "end": source_end,
                        "timestamp": _timestamp_column(original),
                    },
                    input_artifacts=[ref],
                    persist=True,
                )
                evidence.append({"capability": capability, "window": selected})
                if not selected["ok"]:
                    raise EnergyError(selected["error"]["code"], selected["error"]["message"])
                ref = selected["result"]["data"]["artifact_id"]
            return ref

        async def source(capability: str, source_start: str, source_end: str) -> str:
            if capability != "get_energy_consumption" or capability in request.artifacts:
                return await source_single(capability, source_start, source_end)
            left, right = bounds(source_start, source_end)
            # Provider history limits are respected through bounded gateway calls.
            pieces = []
            cursor = left
            while cursor < right:
                chunk_end = min(cursor + timedelta(days=30), right)
                ref = await source_single(capability, cursor.isoformat(), chunk_end.isoformat())
                pieces.append((ref, agent.workbench.read(session, ref)))
                cursor = chunk_end
            first = pieces[0][1]
            identity = (
                "kind",
                "unit",
                "source",
                "provider",
                "site_id",
                "asset_id",
                "quantity_shape",
            )
            if any(
                any(getattr(piece, key) != getattr(first, key) for key in identity)
                for _, piece in pieces
            ):
                raise EnergyError(
                    "incompatible_history_chunks",
                    "Historical chunks changed source or reviewed semantics.",
                )
            combined_rows = [row for _, piece in pieces for row in piece.data]
            combined = first.model_copy(
                update={
                    "data": combined_rows,
                    "time_start": left,
                    "time_end": right,
                    "coverage": {
                        "requested_start": left.isoformat(),
                        "requested_end": right.isoformat(),
                        "chunk_count": len(pieces),
                        "end_exclusive": True,
                    },
                    "provenance": [
                        {
                            "operation": "concatenate_history",
                            "inputs": [
                                {
                                    "artifact_id": ref,
                                    "kind": piece.kind.value,
                                    "source": piece.source,
                                    "provenance": piece.provenance,
                                }
                                for ref, piece in pieces
                            ],
                        }
                    ],
                    "warnings": list(
                        dict.fromkeys(w for _, piece in pieces for w in piece.warnings)
                    ),
                }
            )
            stored = agent.workbench.persist(session, combined)
            evidence.append({"capability": capability, "history_chunks": len(pieces), **stored})
            return stored["artifact_id"]

        history_id = await source(
            "get_energy_consumption", history_start.isoformat(), start.isoformat()
        )
        history = agent.workbench.read(session, history_id)
        history_value = history.model_dump(mode="json")
        # Full requested history is evidence, not a silent shorter substitution.
        rows = history.data
        timestamp, end_column = _timestamp_column(history_value), _end_column(history_value)
        starts = [instant(row[timestamp]) for row in rows]
        ends = [instant(row[end_column]) for row in rows]
        if min(starts) != history_start.tz_convert("UTC") or max(ends) != start.tz_convert("UTC"):
            raise EnergyError(
                "incomplete_history",
                "Meter history must cover the complete requested historical window.",
            )
        forecast_args = dict(request.forecast)
        for key in (
            "start",
            "end",
            "timezone",
            "history_artifact",
            "historical_context_artifact",
            "future_context_artifact",
        ):
            if key in forecast_args:
                raise EnergyError(
                    "invalid_skill_parameters",
                    "Forecast windows and source references are owned by the workflow.",
                )
        forecast_args.update(
            start=start.isoformat(),
            end=end.isoformat(),
            timezone=site.timezone,
            history_artifact=history_id,
        )
        forecast_args.setdefault("interval_minutes", 30)
        forecast_args.setdefault("timestamp", timestamp)
        forecast_args.setdefault("end_column", end_column)
        refs = [history_id]
        for role in ("historical_context", "future_context"):
            if role in request.artifacts:
                ref = request.artifacts[role]
                agent.workbench.read(session, ref)
                forecast_args[role + "_artifact"] = ref
                refs.append(ref)
        predicted = await agent.resolver.execute(
            session,
            CapabilityRequest(
                capability="forecast_energy_consumption",
                arguments=forecast_args,
                kind=DataKind.FORECAST,
                unit="kWh",
                input_artifacts=refs,
            ),
            persist=True,
        )
        evidence.append({"forecast": predicted})
        if not predicted["ok"]:
            raise EnergyError(predicted["error"]["code"], predicted["error"]["message"])
        forecast_id = predicted["result"]["data"]["artifact_id"]
        model = agent.workbench.read(session, forecast_id)
        analysis = predicted
        if skill_id == "forecast-bill":
            tariff_id = await source("get_tariff", start.isoformat(), end.isoformat())
            tariff = agent.workbench.read(session, tariff_id).model_dump(mode="json")
            billing_args: Json = {
                "tariff_timestamp": _timestamp_column(tariff),
                "tariff_end": _end_column(tariff),
            }
            if request.billing is not None:
                billing_args["billing"] = request.billing
            analysis = await agent.resolver.execute(
                session,
                CapabilityRequest(
                    capability="estimate_forecast_bill",
                    arguments={
                        "forecast_artifact": forecast_id,
                        "tariff_artifact": tariff_id,
                        "parameters": billing_args,
                    },
                    kind=DataKind.CALCULATED,
                    input_artifacts=[forecast_id, tariff_id],
                ),
                persist=True,
            )
            if not analysis["ok"]:
                raise EnergyError(analysis["error"]["code"], analysis["error"]["message"])
        evidence.append({"analysis": analysis})
        return {
            "ok": True,
            "skill_id": skill_id,
            "evidence": evidence,
            "forecast_artifact": forecast_id,
            "forecast_summary": model.data["summary"],
            "model": model.data["model"],
            "calculation_basis": "forecast_consumption"
            if skill_id == "forecast-bill"
            else "forecast",
            "window": {
                "history_start": history_start.isoformat(),
                "history_end": start.isoformat(),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timezone": site.timezone,
            },
        }
    except (EnergyError, ValueError, TypeError, KeyError) as exc:
        return {
            "ok": False,
            "skill_id": skill_id,
            "evidence": evidence,
            "error": {
                "code": exc.code if isinstance(exc, EnergyError) else "invalid_skill_parameters",
                "message": exc.message
                if isinstance(exc, EnergyError)
                else "Forecast workflow inputs do not satisfy the reviewed contract.",
            },
        }
