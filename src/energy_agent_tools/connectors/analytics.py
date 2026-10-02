"""Scoped local forecasting and contextual energy analyses."""

from __future__ import annotations

from ..models import Action, DataKind, EnergyResult, ExecutionContext, Json, Tool, Toolkit, schema
from ..registry import Registry


def register(registry: Registry) -> None:
    registry.add_toolkit(
        Toolkit(
            id="energy-analytics",
            name="Energy analytics",
            runtime="native",
            status="experimental",
            description="Offline consumption forecasts, evidence-supported spike screening and combined grid conditions.",
            categories=["forecasting", "time-series", "energy"],
        )
    )
    artifact = {"type": "string", "minLength": 1, "maxLength": 128}
    column = {"type": "string", "minLength": 1, "maxLength": 128}

    async def forecast_consumption(args: Json, ctx: ExecutionContext) -> EnergyResult:
        from ..consumption_forecast import forecast

        def source(key: str):
            ref = args.get(key)
            return (ref, ctx.workbench.read(ctx.session, ref)) if ref is not None else None

        history = source("history_artifact")
        assert history is not None
        parameters = {key: value for key, value in args.items() if not key.endswith("_artifact")}
        return forecast(
            history,
            parameters,
            historical_context=source("historical_context_artifact"),
            future_context=source("future_context_artifact"),
        )

    registry.add(
        Tool(
            name="analytics.forecast_consumption",
            toolkit="energy-analytics",
            description="Forecast future interval electricity consumption from scoped metered history with chronological validation, optional temperature context, uncertainty and provenance.",
            capabilities=["forecast_energy_consumption"],
            actions={Action.CALCULATE},
            result_kind=DataKind.FORECAST,
            result_unit="kWh",
            input_schema=schema(
                {
                    "history_artifact": artifact,
                    "historical_context_artifact": artifact,
                    "future_context_artifact": artifact,
                    "start": {"type": "string", "format": "date-time"},
                    "history_end": {"type": "string", "format": "date-time"},
                    "end": {"type": "string", "format": "date-time"},
                    "timezone": column,
                    "interval_minutes": {"enum": [15, 30, 60]},
                    "column": column,
                    "timestamp": column,
                    "end_column": column,
                    "temperature_column": column,
                    "context_timestamp": column,
                    "coverage": {"type": "number", "minimum": 0.5, "maximum": 0.99},
                },
                ["history_artifact", "start", "end", "timezone", "interval_minutes"],
            ),
        ),
        forecast_consumption,
    )

    async def explain_spike(args: Json, ctx: ExecutionContext) -> EnergyResult:
        from ..spike_analysis import explain

        ref = args["consumption_artifact"]
        weather_ref = args.get("weather_artifact")
        weather = (
            (weather_ref, ctx.workbench.read(ctx.session, weather_ref)) if weather_ref else None
        )
        equipment = [
            (ref, ctx.workbench.read(ctx.session, ref))
            for ref in args.get("equipment_artifacts", [])
        ]
        return explain(
            (ref, ctx.workbench.read(ctx.session, ref)),
            args.get("parameters", {}),
            weather=weather,
            equipment=equipment,
        )

    registry.add(
        Tool(
            name="analytics.explain_consumption_spike",
            toolkit="energy-analytics",
            description="Screen interval load spikes against available observed weather and equipment evidence, reporting supported associations and missing evidence without claiming causation.",
            capabilities=["explain_consumption_spike"],
            actions={Action.CALCULATE},
            result_kind=DataKind.CALCULATED,
            input_schema=schema(
                {
                    "consumption_artifact": artifact,
                    "weather_artifact": artifact,
                    "equipment_artifacts": {
                        "type": "array",
                        "items": artifact,
                        "maxItems": 8,
                        "uniqueItems": True,
                    },
                    "parameters": schema(
                        {
                            "column": column,
                            "timestamp": column,
                            "end_column": column,
                            "window": {"type": "integer", "minimum": 1, "maximum": 10000},
                            "weather_column": column,
                            "weather_timestamp": column,
                            "equipment_column": column,
                            "equipment_timestamp": column,
                            "equipment_end_column": column,
                            "spike_ratio": {"type": "number", "exclusiveMinimum": 1},
                            "min_excess_kwh": {"type": "number", "minimum": 0},
                        }
                    ),
                },
                ["consumption_artifact"],
            ),
        ),
        explain_spike,
    )

    async def grid_conditions(args: Json, ctx: ExecutionContext) -> EnergyResult:
        from ..grid_analysis import analyse

        generation_ref, carbon_ref = args["generation_artifact"], args["carbon_artifact"]
        return analyse(
            (generation_ref, ctx.workbench.read(ctx.session, generation_ref)),
            (carbon_ref, ctx.workbench.read(ctx.session, carbon_ref)),
            args.get("parameters", {}),
        )

    registry.add(
        Tool(
            name="analytics.analyse_grid_conditions",
            toolkit="energy-analytics",
            description="Combine timestamp-matched grid power and carbon intensity, compare generation peaks and lower-carbon intervals, and calculate explicit fuel shares without inferring grid stability or site emissions.",
            capabilities=["analyse_grid_conditions"],
            actions={Action.CALCULATE},
            result_kind=DataKind.CALCULATED,
            result_unit="mixed",
            input_schema=schema(
                {
                    "generation_artifact": artifact,
                    "carbon_artifact": artifact,
                    "parameters": schema(
                        {
                            "timestamp": column,
                            "column": column,
                            "second_timestamp": column,
                            "second_column": column,
                            "end_column": column,
                            "second_end": column,
                            "fuel_column": column,
                            "carbon_threshold_g_per_kwh": {"type": "number", "minimum": 0},
                            "low_carbon_fuels": {
                                "type": "array",
                                "items": column,
                                "maxItems": 50,
                                "uniqueItems": True,
                            },
                        }
                    ),
                },
                ["generation_artifact", "carbon_artifact"],
            ),
        ),
        grid_conditions,
    )

    async def estimate_forecast_bill(args: Json, ctx: ExecutionContext) -> EnergyResult:
        from ..forecast_billing import estimate

        forecast_ref, tariff_ref = args["forecast_artifact"], args["tariff_artifact"]
        return estimate(
            (forecast_ref, ctx.workbench.read(ctx.session, forecast_ref)),
            (tariff_ref, ctx.workbench.read(ctx.session, tariff_ref)),
            args.get("parameters", {}),
        )

    registry.add(
        Tool(
            name="analytics.estimate_forecast_bill",
            toolkit="energy-analytics",
            description="Calculate forecast electricity cost and explicit standing-charge/tax bill scenarios from future consumption intervals and covering tariff validity periods; retain forecast basis and uncertainty limitations.",
            capabilities=["estimate_forecast_bill"],
            actions={Action.CALCULATE},
            result_kind=DataKind.CALCULATED,
            input_schema=schema(
                {
                    "forecast_artifact": artifact,
                    "tariff_artifact": artifact,
                    "parameters": schema(
                        {
                            "tariff_timestamp": column,
                            "tariff_end": column,
                            "tariff_column": column,
                            "billing": schema(
                                {
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
                                    "source": column,
                                },
                                ["standing_charge", "tax", "source"],
                            ),
                        }
                    ),
                },
                ["forecast_artifact", "tariff_artifact"],
            ),
        ),
        estimate_forecast_bill,
    )
