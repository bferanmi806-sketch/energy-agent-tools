# Consumption forecasting and forecast billing

`consumption-forecast` and `forecast-bill` use the scoped gateway, reviewed
provider bindings and artifact store. Both require a site with an IANA timezone
and complete metered interval-energy history. Predicted consumption is
`forecast`; forecast cost is `calculated` with `calculation_basis` set to
`forecast_consumption`.

```python
session = energy.session("owner", "home")

# Defaults to eight forecast days, three calendar months of history, and
# optional automatic temperature context.
result = await session.skill("consumption-forecast", {})

bill = await session.skill(
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

With no future range, the workflow forecasts `days` local calendar days starting
at tomorrow's local midnight; `days` defaults to 8 and may be 1–31. Supply both
`start` and `end` for a custom range. The default `history_end` is the earlier
of forecast start and midnight at the start of today in the site's timezone.
For the default horizon, this uses completed local days through today's
midnight and leaves the current local day out of history; the forecast begins
tomorrow at local midnight. An explicit `history_end` must be at or before
forecast `start` and no more than seven days before it. `history_start` defaults
to `history_months` calendar months before that cutoff; `history_months`
defaults to 3 and accepts 1–24. The full requested meter-history window must be
available.

The workflow resolves meter history and tariffs through gateway capabilities.
Ambiguous sources can be selected with `account_ids` or `tools`; `asset_id`
selects a meter. Meter history is retrieved in gateway calls of at most 30 days
and retains chunk provenance. Complete observed intervals are aggregated to
the requested forecast cadence of 15, 30 or 60 minutes. Aggregation sums
measured Wh/kWh/MWh, preserves metered lineage, and refuses gaps or intervals
crossing an aggregation boundary. It does not fill or prorate history. Custom
meter columns can be mapped with `forecast.timestamp`, `forecast.end_column`
and `forecast.column`. Input rows are normalized to UTC interval starts/ends
and kWh. Native `WORKBENCH_AGGREGATE_ENERGY` provides the same operation for
scoped ordinary artifacts and partitioned datasets.

CSV meter bindings must supply `quantity_shape: interval` and explicit endpoint
columns. Tariff CSV bindings use `window_mode: overlap` so a validity period
beginning before the forecast horizon remains available. Octopus consumption
provides interval starts and ends. An Emoncms feed needs the reviewed account
settings `quantity_shape: interval`, `interval_position: start` and
`interval_seconds` matching the requested feed interval. A `kWh` unit label
alone does not establish interval semantics.

## Temperature context and model validation

`context_mode` controls weather retrieval. It defaults to `auto`: if no context
artifacts are supplied, the workflow attempts one `get_historical_weather`
request and one `get_weather` request for the history and forecast windows. Site
coordinates are used when configured. Choose a source with `account_ids` or
`tools`, or pass capability-specific request values in `arguments`. If either
context source is unavailable or cannot be aligned, `auto` records the
resolution evidence and uses the weekly calendar baseline. `required` makes the
same attempt but fails unless both contexts are available. `explicit` skips
automatic retrieval; pass both pre-aligned artifact IDs in `artifacts` under
`historical_context` and `future_context`. Explicit artifacts must already have
one temperature row for each meter or forecast interval start, in Celsius, and
belong to the same site. Supplying artifacts does not trigger alignment or
fetch a missing partner.

For example, use the default automatic mode with:

```python
result = await session.skill(
    "consumption-forecast",
    {"context_mode": "auto", "days": 8, "history_months": 3},
)
```

Change `context_mode` to `"required"` to require a complete pair. For explicit
context, use
`{"context_mode": "explicit", "artifacts": {"historical_context": "<history-artifact-id>", "future_context": "<forecast-artifact-id>"}}`.

The automatic weather workflow makes one historical-weather request. The
historical connector accepts at most 366 days per request and the workflow does
not split a longer weather window into multiple requests. With `auto`, a
weather-range failure leaves the calendar baseline available; `required`
returns an error. Meter-history retrieval has its separate 30-day chunking
behavior described above.

Open-Meteo historical temperature is hourly, `estimated` gridded
analysis/reanalysis data, not a physical thermometer reading. Future weather
must remain `forecast` data. Automatic context is aligned to the meter cadence
using bounded zero-order hold: each target interval start takes the latest
source value at or before it, while that value is younger than the declared
source cadence. It does not interpolate, use a future value or backfill. The
history request begins at the preceding UTC hour when needed to supply a value
for a non-hour-aligned start. Missing, null or stale readings make automatic
context unavailable. A held metered source is labeled `estimated` if any target
start lacks an exact source reading.

The model always builds a weekly calendar profile. When a complete context pair
is available, it also evaluates a temperature-adjusted candidate on the first
of two chronological seven-day holdout windows. It uses that candidate only if
its selection-window error improves by more than 2% (or the small numeric
tolerance) over the calendar profile. The following complete seven days
calibrate the interval error band. History must contain at least 70 days of
contiguous, non-overlapping, finite metered interval energy, plus complete
seven-day selection and calibration windows and at least eight earlier
training weeks. Each requested interval must match the selected cadence exactly.
After selection and calibration, the chosen point model is refit on all history
through `history_end`.

Empirical residual bands describe calibration errors; they do not guarantee
future interval coverage or model occupancy, equipment, weather or other
changes. Summing interval bounds gives scenarios, not a guaranteed confidence
interval for total use or a bill. Estimated historical weather does not prove
future weather accuracy.

## Billing coverage

Future tariffs must have explicit finite validity covering every predicted
interval. Rates can change within a consumption interval; the calculation
splits energy uniformly across each rate segment and reports that assumption.
Negative tariffs reverse the corresponding energy-cost bounds. Money uses
Decimal arithmetic without rounding to minor currency units.

Without explicit standing charges and tax treatment, the result is an energy
cost estimate and lists missing bill components. Supplied components produce a
bill for the declared scope. Unknown future tariff periods are refused. A
tariff validity period may begin before the prediction horizon; the workflow
keeps that validity rather than clipping away a covering rate.

Source references, data kinds, model assumptions and calculation lineage remain
in artifacts and evidence. Fixture acceptance checks contract and numerical
behavior; it does not qualify a physical meter or prove forecast accuracy for
a real installation.
