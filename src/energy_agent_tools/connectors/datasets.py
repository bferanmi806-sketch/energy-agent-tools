"""Bounded large-file operations through the scoped energy gateway."""

from __future__ import annotations

import asyncio
import csv
import json
from math import isfinite
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from ..models import (
    Action,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Tool,
    schema,
)
from ..registry import Registry
from ..timeseries import numeric_value
from ..windows import bounds, instant, select_window


def _source(ctx: ExecutionContext, ref: str) -> tuple[str, EnergyResult]:
    result = ctx.workbench.read(ctx.session, ref)
    if (
        not isinstance(result.data, dict)
        or result.data.get("storage") != "partitioned-timeseries.v1"
    ):
        raise EnergyError("not_partitioned", "Use a partitioned dataset reference.")
    dataset_id = result.data["dataset_id"]
    ctx.workbench.partitioned.describe(ctx.session, dataset_id)
    return dataset_id, result


def _lineage(ref: str, result: EnergyResult, operation: str) -> list[Json]:
    return [
        {
            "operation": operation,
            "artifact_id": ref,
            "input_kind": result.kind.value,
            "source": result.source,
            "unit": result.unit,
            "quantity_shape": result.quantity_shape,
            "provenance": result.provenance,
        }
    ]


def register(registry: Registry, root: Path) -> None:
    root = root.resolve()
    artifact = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
    column = {"type": "string", "minLength": 1, "maxLength": 128}

    async def import_csv(args: Json, ctx: ExecutionContext) -> EnergyResult:
        path = (root / args["file"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise EnergyError(
                "file_forbidden", "CSV file is outside the configured data directory."
            )
        try:
            ZoneInfo(args["timezone"])
        except ZoneInfoNotFoundError as exc:
            raise EnergyError("invalid_timezone", "Use a valid IANA timezone.") from exc
        time_bounds = (
            bounds(args["start"], args["end"]) if "start" in args and "end" in args else None
        )
        if ("start" in args) != ("end" in args):
            raise EnergyError("time_window_required", "Supply both start and end.")
        store = ctx.workbench.partitioned
        if path.stat().st_size > store.max_bytes:
            raise EnergyError("input_too_large", "CSV exceeds the dataset import byte limit.")
        metadata = EnergyResult(
            data=[],
            kind=DataKind(args["kind"]),
            unit=args["unit"],
            source="local-csv",
            provider="csv",
            timezone=args["timezone"],
            resolution=args.get("resolution"),
            quantity_shape=args["quantity_shape"],
            site_id=ctx.site_id,
            asset_id=ctx.asset_id,
            field_units=args.get("field_units", {}),
            quality="caller-declared",
            warnings=[
                "File semantics are operator-declared; import does not qualify physical readings or complete temporal coverage."
            ],
            provenance=[
                {
                    "operation": "partitioned_csv_import",
                    "file": args["file"],
                    "timestamp_column": args.get("timestamp", "timestamp"),
                }
            ],
        )

        def ingest() -> Json:
            with path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if (
                    not reader.fieldnames
                    or any(not name for name in reader.fieldnames)
                    or len(set(reader.fieldnames)) != len(reader.fieldnames)
                ):
                    raise EnergyError("invalid_csv", "CSV requires unique column names.")

                def rows():
                    for row in reader:
                        if None in row or any(value is None for value in row.values()):
                            raise EnergyError(
                                "invalid_csv", "CSV row width differs from its header."
                            )
                        if time_bounds:
                            timestamp = args.get("timestamp", "timestamp")
                            if timestamp not in row:
                                raise EnergyError(
                                    "column_not_found", "Timestamp column is missing."
                                )
                            if not time_bounds[0] <= instant(row[timestamp]) < time_bounds[1]:
                                continue
                        yield row

                return store.ingest(ctx.session, metadata, rows())

        saved = await asyncio.to_thread(ingest)
        return metadata.model_copy(
            update={
                "data": {
                    "storage": "partitioned-timeseries.v1",
                    "dataset_id": saved["artifact_id"],
                    "timestamp_column": args.get("timestamp", "timestamp"),
                    **saved,
                }
            }
        )

    async def page(args: Json, ctx: ExecutionContext) -> EnergyResult:
        dataset_id, result = _source(ctx, args["artifact_id"])
        envelope_bytes = len(json.dumps(result.model_dump(mode="json")).encode())
        byte_limit = int((ctx.workbench.inline_bytes - envelope_bytes - 4096) * 0.7)
        if byte_limit < 1:
            raise EnergyError(
                "metadata_too_large", "Dataset metadata exceeds the inline page budget."
            )
        fetched = await asyncio.to_thread(
            ctx.workbench.partitioned.read_page,
            ctx.session,
            dataset_id,
            args.get("offset", 0),
            args.get("limit", 1000),
            byte_limit=min(byte_limit, 16 * 1024 * 1024),
            columns=args.get("columns"),
        )
        return result.model_copy(
            update={
                "data": {
                    "rows": fetched["rows"],
                    "total_rows": fetched["total_rows"],
                    "next_offset": fetched["next_offset"],
                    "returned_rows": len(fetched["rows"]),
                },
                "provenance": _lineage(args["artifact_id"], result, "dataset_page"),
            }
        )

    async def summarize(args: Json, ctx: ExecutionContext) -> EnergyResult:
        dataset_id, result = _source(ctx, args["artifact_id"])
        selected_column = args["column"]
        unit = result.field_units.get(selected_column, result.unit)
        if unit == "mixed":
            raise EnergyError("unit_required", "Declare the selected column's field unit.")

        def calculate() -> Json:
            count = missing = 0
            total = correction = scale = 0.0
            minimum = maximum = None
            summable = result.quantity_shape == "interval" and unit in {"Wh", "kWh", "MWh"}
            for chunk in ctx.workbench.partitioned.iter_chunks(ctx.session, dataset_id):
                for row in chunk:
                    if selected_column not in row:
                        raise EnergyError("column_not_found", "Requested column is missing.")
                    raw = row[selected_column]
                    value = numeric_value(None if raw == "" else raw)
                    if value is None:
                        missing += 1
                        continue
                    count += 1
                    if abs(value) > scale:
                        ratio = scale / abs(value)
                        total *= ratio
                        correction *= ratio
                        scale = abs(value)
                    normalized = value / scale if scale else 0.0
                    updated = total + normalized
                    correction += (
                        ((total - updated) + normalized)
                        if abs(total) >= abs(normalized)
                        else ((normalized - updated) + total)
                    )
                    total = updated
                    minimum = value if minimum is None else min(minimum, value)
                    maximum = value if maximum is None else max(maximum, value)
            corrected = total + correction
            mean = (corrected / count) * scale if count else None
            energy_sum = corrected * scale if summable and count else None
            if any(value is not None and not isfinite(value) for value in (mean, energy_sum)):
                raise EnergyError(
                    "numeric_overflow", "Streamed summary exceeded its finite numeric range."
                )
            return {
                "count": count,
                "missing": missing,
                "sum": energy_sum,
                "mean": mean,
                "min": minimum,
                "max": maximum,
                "streamed": True,
            }

        data = await asyncio.to_thread(calculate)
        return result.model_copy(
            update={
                "data": data,
                "kind": DataKind.CALCULATED,
                "unit": unit,
                "source": "workbench",
                "quantity_shape": None,
                "provenance": _lineage(args["artifact_id"], result, "dataset_summary"),
            }
        )

    definitions = [
        (
            "DATASET_IMPORT_CSV",
            "Stream an approved CSV into a private partitioned dataset with declared semantics; never load the complete file into memory.",
            schema(
                {
                    "file": column,
                    "kind": {"enum": [k.value for k in DataKind]},
                    "unit": column,
                    "timezone": column,
                    "quantity_shape": {"enum": ["interval", "instantaneous", "counter"]},
                    "resolution": column,
                    "start": {"type": "string", "format": "date-time"},
                    "end": {"type": "string", "format": "date-time"},
                    "timestamp": column,
                    "field_units": {"type": "object", "additionalProperties": column},
                },
                ["file", "kind", "unit", "timezone", "quantity_shape"],
            ),
            import_csv,
            Action.READ,
        ),
        (
            "DATASET_PAGE",
            "Read a bounded page of a session-scoped dataset, preserving source kind and row order.",
            schema(
                {
                    "artifact_id": artifact,
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                    "columns": {
                        "type": "array",
                        "items": column,
                        "minItems": 1,
                        "maxItems": 64,
                        "uniqueItems": True,
                    },
                },
                ["artifact_id"],
            ),
            page,
            Action.READ,
        ),
        (
            "DATASET_SUMMARIZE",
            "Stream a numeric column summary; sum only explicitly declared interval energy, never power or cumulative counters.",
            schema({"artifact_id": artifact, "column": column}, ["artifact_id", "column"]),
            summarize,
            Action.CALCULATE,
        ),
        (
            "DATASET_WINDOW",
            "Scan a large dataset for a bounded time window; preserve source semantics and report observed coverage without prorating intervals.",
            schema(
                {
                    "artifact_id": artifact,
                    "start": {"type": "string", "format": "date-time"},
                    "end": {"type": "string", "format": "date-time"},
                    "timestamp": column,
                    "end_column": column,
                },
                ["artifact_id", "start", "end"],
            ),
            select_dataset_window,
            Action.CALCULATE,
        ),
    ]
    for name, description, input_schema, handler, action in definitions:
        registry.add(
            Tool(
                name=name,
                toolkit="csv",
                description=description,
                input_schema=input_schema,
                capabilities=["large_timeseries"],
                actions={action},
            ),
            handler,
        )


async def select_dataset_window(args: Json, ctx: ExecutionContext) -> EnergyResult:
    dataset_id, result = _source(ctx, args["artifact_id"])
    left, right = bounds(args["start"], args["end"])
    timestamp = args.get("timestamp", "timestamp")
    end_column = args.get("end_column")
    step = None
    if result.resolution:
        try:
            candidate = pd.Timedelta(result.resolution).to_pytimedelta()
            if candidate.total_seconds() > 0:
                step = candidate
        except (ValueError, TypeError, OverflowError):
            pass

    def select() -> EnergyResult:
        rows: list[dict[str, Any]] = []
        for chunk in ctx.workbench.partitioned.iter_window_chunks(
            ctx.session,
            dataset_id,
            start=left,
            end=right,
            timestamp=timestamp,
            end_column=end_column,
        ):
            for row in chunk:
                if timestamp not in row:
                    raise EnergyError("column_not_found", "Timestamp column is missing.")
                point = instant(row[timestamp])
                if point < left:
                    if end_column and end_column not in row:
                        raise EnergyError(
                            "column_not_found", "Declared interval end column is missing."
                        )
                    edge = (
                        row[end_column]
                        if end_column
                        else next(
                            (row[key] for key in ("interval_end", "to", "end") if key in row), None
                        )
                    )
                    finish = instant(edge) if edge is not None else point + step if step else None
                    if finish is not None and left < finish:
                        raise EnergyError(
                            "interval_boundary_mismatch", "The start cuts an observed interval."
                        )
                if left <= point < right:
                    if len(rows) >= 100_000:
                        raise EnergyError(
                            "output_too_large",
                            "Select a narrower time window; at most 100000 rows can be materialized.",
                        )
                    rows.append(row)
        selected = select_window(
            result.model_copy(update={"data": rows}),
            args["artifact_id"],
            args["start"],
            args["end"],
            timestamp,
            args.get("end_column"),
        )
        return selected

    return await asyncio.to_thread(select)
