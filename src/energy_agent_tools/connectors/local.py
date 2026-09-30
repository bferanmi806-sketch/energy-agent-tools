from __future__ import annotations

import csv
from pathlib import Path

from ..models import (
    Action,
    DataKind,
    EnergyError,
    EnergyResult,
    ExecutionContext,
    Json,
    Tool,
    Toolkit,
    schema,
)
from ..registry import Registry


def register(registry: Registry) -> None:
    registry.add_toolkit(
        Toolkit(
            id="workbench",
            name="Local workbench",
            runtime="native",
            status="stable",
            description="Private time-series artifact analysis",
        )
    )
    artifact = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
    column = {"type": "string", "minLength": 1}

    async def summarize(args: Json, ctx: ExecutionContext) -> EnergyResult:
        return ctx.workbench.summarize(ctx.session, args["artifact_id"], args["column"])

    async def resample(args: Json, ctx: ExecutionContext) -> EnergyResult:
        return ctx.workbench.resample(
            ctx.session,
            args["artifact_id"],
            args["timestamp"],
            args["column"],
            args["frequency"],
            args["aggregation"],
        )

    async def join(args: Json, ctx: ExecutionContext) -> EnergyResult:
        return ctx.workbench.join(ctx.session, args["left"], args["right"], args["timestamp"])

    async def pivot(args: Json, ctx: ExecutionContext) -> EnergyResult:
        return ctx.workbench.pivot(
            ctx.session, args["artifact_id"], args["timestamp"], args["variable"], args["value"]
        )

    async def anomaly(args: Json, ctx: ExecutionContext) -> EnergyResult:
        import pandas as pd

        frame, result = ctx.workbench.frame(ctx.session, args["artifact_id"])
        col = args["column"]
        if col not in frame:
            raise EnergyError("column_not_found", "Value column is missing.")
        values = pd.to_numeric(frame[col], errors="raise")
        median = float(values.median())
        mad = float((values - median).abs().median())
        threshold = args.get("threshold", 3.5)
        # A zero MAD is common for a flat base load; flag only changes from the median.
        score = (values - median).abs() / (1.4826 * mad) if mad > 0 else (values - median).abs()
        mask = score > threshold if mad > 0 else score > 0
        records = frame.loc[mask].head(50).to_dict(orient="records")
        return ctx.workbench.derived(
            {"median": median, "mad": mad, "anomaly_count": int(mask.sum()), "preview": records},
            [(args["artifact_id"], result)],
            result.unit,
            [
                "Median absolute deviation screening; correlations cannot establish cause.",
                "No seasonality or occupancy adjustment.",
            ],
        )

    entries = [
        (
            "WORKBENCH_SUMMARIZE",
            "Summarize interval energy consumption, weather, tariff or carbon time series locally.",
            schema({"artifact_id": artifact, "column": column}, ["artifact_id", "column"]),
            ["analyse_timeseries", "get_energy_consumption"],
            summarize,
        ),
        (
            "WORKBENCH_RESAMPLE",
            "Aggregate or resample energy/weather series in its local timezone with explicit DST offsets.",
            schema(
                {
                    "artifact_id": artifact,
                    "timestamp": column,
                    "column": column,
                    "frequency": {"enum": ["15min", "30min", "1h", "1D"]},
                    "aggregation": {"enum": ["sum", "mean"]},
                },
                ["artifact_id", "timestamp", "column", "frequency", "aggregation"],
            ),
            ["analyse_timeseries"],
            resample,
        ),
        (
            "WORKBENCH_PIVOT",
            "Pivot long-form weather/telemetry into one row per timestamp with separate variable columns.",
            schema(
                {"artifact_id": artifact, "timestamp": column, "variable": column, "value": column},
                ["artifact_id", "timestamp", "variable", "value"],
            ),
            ["analyse_timeseries"],
            pivot,
        ),
        (
            "WORKBENCH_JOIN",
            "Compare consumption against weather and tariff data by exact UTC timestamp join.",
            schema(
                {"left": artifact, "right": artifact, "timestamp": column},
                ["left", "right", "timestamp"],
            ),
            ["compare_energy_data"],
            join,
        ),
        (
            "WORKBENCH_ANOMALY",
            "Investigate abnormal building consumption spike using robust statistical anomaly screening.",
            schema(
                {
                    "artifact_id": artifact,
                    "column": column,
                    "threshold": {"type": "number", "minimum": 0.1, "maximum": 20},
                },
                ["artifact_id", "column"],
            ),
            ["detect_anomaly"],
            anomaly,
        ),
    ]
    for name, description, inputs, capabilities, handler in entries:
        registry.add(
            Tool(
                name=name,
                toolkit="workbench",
                description=description,
                input_schema=inputs,
                capabilities=capabilities,
                actions={Action.CALCULATE},
            ),
            handler,
        )


def register_csv(registry: Registry, root: Path) -> None:
    """Operator grants access to one directory. Agents cannot read arbitrary host paths."""
    root = root.resolve()
    registry.add_toolkit(
        Toolkit(
            id="csv",
            name="Local energy CSV",
            runtime="native",
            status="stable",
            description="Read operator-approved CSV files with declared semantics",
        )
    )

    async def read(args: Json, ctx: ExecutionContext) -> EnergyResult:
        path = (root / args["file"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise EnergyError(
                "file_forbidden", "CSV file is outside the configured data directory."
            )
        if path.stat().st_size > 10_000_000:
            raise EnergyError("input_too_large", "CSV exceeds the 10 MB import limit.")
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        return EnergyResult(
            data=rows,
            kind=DataKind(args["kind"]),
            unit=args["unit"],
            source="local-csv",
            timezone=args["timezone"],
            quality="user-declared",
            warnings=[
                "CSV measurement kind and units are declared by the caller, not verified against hardware."
            ],
            provenance=[{"file": path.relative_to(root).as_posix(), "declared_kind": args["kind"]}],
        )

    registry.add(
        Tool(
            name="CSV_READ_TIMESERIES",
            toolkit="csv",
            description="Import local meter consumption, PV, battery, tariff or building telemetry CSV.",
            capabilities=[
                "get_energy_consumption",
                "get_generation",
                "get_storage_state",
                "import_timeseries",
            ],
            input_schema=schema(
                {
                    "file": {"type": "string"},
                    "kind": {"enum": [k.value for k in DataKind]},
                    "unit": {"type": "string", "minLength": 1},
                    "timezone": {"type": "string"},
                },
                ["file", "kind", "unit", "timezone"],
            ),
        ),
        read,
    )
