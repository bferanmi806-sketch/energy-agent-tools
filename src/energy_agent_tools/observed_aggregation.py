"""Gateway adapter for complete observed interval energy aggregation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

from .models import (
    Action,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Tool,
    schema,
)
from .registry import Registry
from .windows import bounds, instant


def register(registry: Registry) -> None:
    column = {"type": "string", "minLength": 1}

    async def aggregate(args: Json, ctx: ExecutionContext) -> EnergyResult:
        from .interval_aggregation import aggregate_interval_energy

        source = ctx.workbench.read(ctx.session, args["artifact_id"])
        if source.kind != DataKind.METERED or source.quantity_shape != "interval":
            raise EnergyError(
                "observed_interval_energy_required", "Use explicitly metered interval energy."
            )
        left, right = bounds(args["start"], args["end"])
        timestamp = args.get("timestamp", "timestamp")
        end_column = args.get("end_column", "end")
        value_column = args.get("column", "value")
        unit = source.field_units.get(value_column, source.unit)

        def calculate() -> list[Json]:
            rows: Iterator[Json]
            if (
                isinstance(source.data, dict)
                and source.data.get("storage") == "partitioned-timeseries.v1"
            ):
                chunks = ctx.workbench.partitioned.iter_window_chunks(
                    ctx.session,
                    source.data["dataset_id"],
                    start=left,
                    end=right,
                    timestamp=timestamp,
                    end_column=end_column,
                )
                rows = (row for chunk in chunks for row in chunk)
            elif isinstance(source.data, list):
                rows = iter(source.data)
            else:
                raise EnergyError(
                    "not_tabular", "Use interval rows or a scoped partitioned dataset."
                )

            def selected():
                for row in rows:
                    if timestamp not in row:
                        raise EnergyError("column_not_found", "Timestamp column is missing.")
                    point = instant(row[timestamp])
                    if point < left:
                        if end_column not in row:
                            raise EnergyError("column_not_found", "Interval end column is missing.")
                        if instant(row[end_column]) > left:
                            raise EnergyError(
                                "interval_boundary_mismatch", "The start cuts an observed interval."
                            )
                    elif point < right:
                        yield row

            return aggregate_interval_energy(
                selected(),
                start=args["start"],
                end=args["end"],
                timestamp=timestamp,
                end_column=end_column,
                value=value_column,
                unit=unit,
                interval_minutes=args.get("interval_minutes", 30),
            )

        data = await asyncio.to_thread(calculate)
        return source.model_copy(
            update={
                "data": data,
                "unit": "kWh",
                "resolution": f"{args.get('interval_minutes', 30)}min",
                "time_start": left,
                "time_end": right,
                "field_units": {"value": "kWh"},
                "coverage": {
                    "requested_start": left.isoformat(),
                    "requested_end": right.isoformat(),
                    "end_exclusive": True,
                    "complete_observed_intervals": True,
                },
                "provenance": [
                    {
                        "operation": "aggregate_observed_interval_energy",
                        "artifact_id": args["artifact_id"],
                        "input_kind": source.kind.value,
                        "source": source.source,
                        "unit": source.unit,
                        "provenance": source.provenance,
                        "timestamp_column": timestamp,
                        "end_column": end_column,
                        "value_column": value_column,
                    }
                ],
                "warnings": [
                    *source.warnings,
                    "Sums complete observed interval amounts without filling, splitting or modeled values; metered source qualification is unchanged.",
                ],
            }
        )

    registry.add(
        Tool(
            name="WORKBENCH_AGGREGATE_ENERGY",
            toolkit="workbench",
            description="Stream complete metered interval energy into aligned larger intervals, retaining observed measurement lineage; refuse gaps and any required splitting.",
            input_schema=schema(
                {
                    "artifact_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "timestamp": column,
                    "end_column": column,
                    "column": column,
                    "interval_minutes": {"enum": [15, 30, 60]},
                },
                ["artifact_id", "start", "end"],
            ),
            capabilities=["analyse_timeseries"],
            actions={Action.CALCULATE},
        ),
        aggregate,
    )
