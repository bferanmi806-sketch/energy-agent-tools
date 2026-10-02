# Consumption forecasting and forecast billing

`consumption-forecast` and `forecast-bill` use the existing scoped gateway,
reviewed provider bindings and artifact store. They require a site with an IANA
timezone. Historical interval energy remains `metered`; predicted consumption
is `forecast`; its cost is `calculated` with `calculation_basis` set to
`forecast_consumption`.

```python
session = energy.session("owner", "home")
result = await session.skill(
    "forecast-bill",
    {
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-09T00:00:00Z",
        "history_months": 3,
        "billing": {
            "standing_charge": {"amount_per_day": 0.30, "currency": "GBP", "taxable": False},
            "tax": {"rate": 0.05, "energy_taxable": True},
            "source": "Reviewed tariff components",
        },
    },
)
```

The workflow resolves meter history and tariffs without requiring provider tool
names. Ambiguous sources require a selection using `account_ids`, `tools` or an
asset. With no explicit future range, it predicts eight local days beginning at
tomorrow's local midnight. `days` and `history_months` configure those defaults.
Historical range subtraction uses calendar months, including February and DST.
History retrieval uses gateway calls covering at most 30 days each and preserves
all chunk provenance. The full requested history must be present. Explicit starts/ends, finite values
and declared interval-energy semantics are required; cumulative counters and
power samples are not silently treated as interval kWh.

CSV bindings must supply `quantity_shape: interval` and explicit endpoint
columns. Tariff CSV bindings use `window_mode: overlap` so a validity period
starting before the forecast horizon remains available. Octopus consumption provides interval starts and ends. An Emoncms feed
requires operator-reviewed account settings `quantity_shape: interval`,
`interval_position: start` and `interval_seconds` matching the requested feed
interval. A `kWh` unit label alone does not establish interval semantics.

## Context and uncertainty

The model uses a local weekly calendar baseline. A temperature-conditioned
candidate is evaluated when both historical observed and future forecast
context are supplied. Context artifacts use the `historical_context` and
`future_context` roles in `artifacts`. They must belong to the same known site;
the weather asset can differ from the electricity meter. Context timestamps
must align with the selected consumption cadence. Context fetching and temporal
aggregation are not currently automatic in this recipe. Missing context leaves
the calendar baseline available rather than inventing a weather effect.

Chronological model selection and error calibration use distinct windows.
After evaluation, the chosen method is fitted on the complete historical input
for future prediction. Returned diagnostics identify the evaluation and final
training windows. Empirical residual bands describe calibration errors; they
are not guaranteed future coverage and do not model every occupancy, equipment
or weather change. Summing per-interval bounds gives scenarios, not a guaranteed
confidence interval for total usage or a bill.

## Billing coverage

Future tariffs must have explicit finite validity covering every predicted
interval. Rates can change within a consumption interval; the calculation
splits energy uniformly across each rate segment and reports that assumption.
Negative tariffs reverse the corresponding energy-cost bounds. Money is
calculated with Decimal arithmetic without rounding to minor currency units.

Without explicit standing charges and tax treatment, the result is an energy
cost estimate and lists missing bill components. Supplied components produce a
complete bill for the declared scope. Unknown future tariff periods are refused.
A tariff validity period may begin before the prediction horizon; the workflow
keeps that validity rather than clipping away a covering rate.

All source references, original data kinds, model assumptions and calculation
lineage remain in artifacts and evidence. Offline fixture acceptance establishes
contract and numerical behavior; it does not qualify a physical meter or prove
forecast accuracy for a real installation.
