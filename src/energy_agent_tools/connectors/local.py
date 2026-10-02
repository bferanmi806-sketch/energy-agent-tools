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

    async def window(args: Json, ctx: ExecutionContext) -> EnergyResult:
        from ..windows import select_window

        return select_window(
            ctx.workbench.read(ctx.session, args["artifact_id"]),
            args["artifact_id"],
            args["start"],
            args["end"],
            args["timestamp"],
        )

    registry.add(
        Tool(
            name="WORKBENCH_WINDOW",
            toolkit="workbench",
            description="Select a half-open time window, preserve source kind and report observed coverage. Never prorates intervals.",
            input_schema=schema(
                {
                    "artifact_id": artifact,
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "timestamp": column,
                },
                ["artifact_id", "start", "end", "timestamp"],
            ),
            capabilities=["analyse_timeseries"],
            actions={Action.CALCULATE},
        ),
        window,
    )

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

    async def energy_operation(args: Json, ctx: ExecutionContext) -> EnergyResult:
        from ..timeseries import operate

        inputs = [
            (artifact_id, ctx.workbench.read(ctx.session, artifact_id))
            for artifact_id in args["artifact_ids"]
        ]
        return operate(args["operation"], inputs, args.get("parameters", {}))

    parameters = schema(
        {
            "timestamp": column,
            "column": column,
            "second_timestamp": column,
            "second_column": column,
            "second_end": column,
            "consumption_basis": {"enum": ["total_load"]},
            "storage_mode": {"enum": ["none"]},
            "frequency": {"enum": ["15min", "30min", "1h", "1D", "daily", "weekly", "monthly"]},
            "start": {"type": "string", "format": "date-time"},
            "end": {"type": "string"},
            "minimum": {"type": "number"},
            "maximum": {"type": "number"},
            "window": {"type": "integer", "minimum": 1, "maximum": 10000},
            "method": {"enum": ["left", "trapezoid"]},
            "unit": column,
            "finish": {"type": "string", "format": "date-time"},
            "timezone": column,
            "source": column,
            "standing_charge": schema(
                {
                    "amount_per_day": {"type": "number", "minimum": 0},
                    "currency": {"enum": ["GBP", "USD", "EUR"]},
                    "taxable": {"type": "boolean"},
                },
                ["amount_per_day", "currency", "taxable"],
            ),
            "tax": schema(
                {
                    "rate": {"type": "number", "minimum": 0, "maximum": 1},
                    "energy_taxable": {"type": "boolean"},
                },
                ["rate", "energy_taxable"],
            ),
        }
    )
    registry.add(
        Tool(
            name="WORKBENCH_ENERGY_OPERATION",
            toolkit="workbench",
            description="Filter, check missing intervals, convert counters, integrate power into energy, calculate tariff cost/carbon and explicit standing-charge/tax bills, reconcile declared solar load/generation, align, compare calendar periods or rolling baselines with strict units and lineage.",
            input_schema=schema(
                {
                    "operation": {
                        "enum": [
                            "filter",
                            "missing",
                            "counter",
                            "integrate_power",
                            "cost",
                            "bill",
                            "carbon",
                            "baseline",
                            "compare",
                            "normalize",
                            "align",
                            "solar_balance",
                        ]
                    },
                    "artifact_ids": {
                        "type": "array",
                        "items": artifact,
                        "minItems": 1,
                        "maxItems": 2,
                    },
                    "parameters": parameters,
                },
                ["operation", "artifact_ids"],
            ),
            capabilities=[
                "analyse_timeseries",
                "calculate_energy_cost",
                "calculate_electricity_bill",
                "calculate_carbon",
                "compare_energy_data",
            ],
            actions={Action.CALCULATE},
        ),
        energy_operation,
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
            rows: list[dict[str, str]] = []
            for row in csv.DictReader(stream):
                if len(rows) >= 100000:
                    raise EnergyError("input_too_large", "CSV exceeds the 100000 row limit.")
                rows.append(row)
        if args.get("start") or args.get("end"):
            import pandas as pd

            timestamp = args.get("timestamp", "timestamp")
            start = pd.Timestamp(args["start"]) if args.get("start") else None
            end = pd.Timestamp(args["end"]) if args.get("end") else None
            if any(value is not None and value.tzinfo is None for value in (start, end)):
                raise EnergyError("naive_timestamp", "CSV range requires explicit offsets.")
            filtered = []
            for row in rows:
                instant = pd.Timestamp(row[timestamp])
                if instant.tzinfo is None:
                    raise EnergyError("naive_timestamp", "CSV timestamps require explicit offsets.")
                if (start is None or instant >= start) and (end is None or instant < end):
                    filtered.append(row)
            rows = filtered
        return EnergyResult(
            data=rows,
            kind=DataKind(args["kind"]),
            unit=args["unit"],
            source="local-csv",
            timezone=args["timezone"],
            resolution=args.get("resolution"),
            quantity_shape=args.get("quantity_shape"),
            quality="user-declared",
            warnings=[
                "CSV measurement kind and units are declared by the caller, not verified against hardware."
            ],
            provenance=[
                {
                    "file": path.relative_to(root).as_posix(),
                    "declared_kind": args["kind"],
                    **(
                        {"declared_quantity_shape": args["quantity_shape"]}
                        if args.get("quantity_shape") is not None
                        else {}
                    ),
                    **(
                        {"declared_resolution": args["resolution"]}
                        if args.get("resolution") is not None
                        else {}
                    ),
                }
            ],
        )

    registry.add(
        Tool(
            name="CSV_READ_TIMESERIES",
            toolkit="csv",
            description="Import local CSV with caller-declared measurement kind, optional quantity shape and resolution.",
            capabilities=[
                "get_energy_consumption",
                "get_generation",
                "get_storage_state",
                "import_timeseries",
            ],
            input_schema=schema(
                {
                    "file": {"type": "string"},
                    "start": {"type": "string", "format": "date-time"},
                    "end": {"type": "string", "format": "date-time"},
                    "timestamp": {"type": "string"},
                    "kind": {"enum": [k.value for k in DataKind]},
                    "unit": {"type": "string", "minLength": 1},
                    "timezone": {"type": "string"},
                    "quantity_shape": {
                        "enum": ["interval", "instantaneous", "counter"],
                        "description": "Caller-declared quantity semantics; never inferred from CSV content.",
                    },
                    "resolution": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": r"\S",
                        "description": "Caller-declared sampling or interval resolution.",
                    },
                },
                ["file", "kind", "unit", "timezone"],
            ),
        ),
        read,
    )
