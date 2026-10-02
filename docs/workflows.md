# Executable workflows

The bound SDK exposes the workflows through `BoundSession.skill`. The MCP
endpoint exposes the same implementation as `ENERGY_RUN_SKILL`. A workflow
resolves each capability through the reviewed capability resolver, persists
provider results as scoped artifacts, and then runs the bounded workbench or
engineering operation over those artifacts.

The workflow result is a JSON object with `ok`, `skill_id`, and `evidence`.
Successful provider calls appear in `evidence` with their capability name and
an artifact reference. A derived operation appears as an `analysis` evidence
item. A blocked capability returns `ok: false`, `blocked`, and the evidence
collected so far. Invalid parameters return `ok: false` with an `error` object.
Every workflow also returns the runtime's fixed safety notes when it reaches the
analysis stage: correlation does not establish a cause, plans and model output
cannot dispatch equipment, and forecast data remains forecast data.

## Request shape

The Python call is:

```python
result = await session.skill("electricity-cost", parameters)
```

`parameters` is the mapping itself; there is no extra `parameters` wrapper. The
runtime accepts exactly these top-level fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `start` | offset-aware date-time string | Start of the provider request window. Must be supplied with `end`. |
| `end` | offset-aware date-time string | End of the provider request window. Must be supplied with `start`. |
| `arguments` | object | Map each capability ID to that provider's exact arguments. A workflow's window is merged first; these arguments override it. |
| `asset_id` | string | Asset passed to `get_energy_consumption` and direct `get_solar_forecast`. |
| `account_ids` | object | Map capability ID to an explicitly selected account ID. |
| `tools` | object | Map capability ID to a canonical tool name when choosing between reviewed providers. |
| `artifacts` | object | Map capability ID to an existing artifact ID in this user's session. |
| `column` | string | Value column used by workbench analysis. Defaults to `value`. |
| `frequency` | string | Workbench frequency: `15min`, `30min`, `1h`, `1D`, `daily`, `weekly`, or `monthly`, subject to the operation. |
| `window` | integer | Previous-observation count for a baseline, from 1 through 10,000. |
| `battery` | object | Battery constraints for the battery workflows. |
| `solar` | object | PV model and weather overrides for `solar-forecast`. |
| `solar_balance` | object | Explicit `consumption_basis: total_load` and `storage_mode: none` for `solar-consumption` interval reconciliation. |
| `alternative_tariff` | string | Existing same-session tariff artifact for `tariff-comparison`. |
| `comparison_artifact` | string | Existing same-session metered interval-energy artifact for `building-comparison`. |
| `consumption_transform` | object | Explicit `counter` or `integrate_power` operation and parameters for a preloaded measured artifact; see the conversion example below. |
| `billing` | object | Explicit standing charge, tax treatment, and source for a complete local-day cost workflow; see [tariff components](tariff-components.md). |
| `alternative_billing` | object | Separate explicit schedule required with `billing` for `tariff-comparison`. |
| `input_artifacts` | array of strings | Up to ten same-session model input artifacts for `power-flow`; rejected by other recipes. |

Unknown top-level fields return `invalid_skill_parameters`. `start` and `end`
are passed to provider capabilities that support ranges (`get_energy_consumption`,
`get_tariff`, `get_weather`, `get_generation`, `get_grid_generation`, and
`get_carbon_intensity`). The
provider binding still owns the exact argument names and schema. For example,
do not assume that every tariff provider accepts `product_code` or that every
telemetry provider accepts `asset_id`; inspect the selected binding first.

This is the smallest safe pattern for inspecting and then running a capability:

```python
resolution = session.resolve("get_energy_consumption", kind="metered", unit="kWh", asset_id="meter")
if resolution["selected"] is None:
    # Stop and show resolution["status"] and resolution["candidates"].
    # Do not invent a provider, unit, or source.
    raise RuntimeError(resolution["status"])

result = await session.capability(
    "get_energy_consumption",
    arguments={"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"},
    asset_id="meter",
    kind="metered",
    unit="kWh",
    persist=True,
)
```

`resolution["selected"]` is present only for one uniquely ranked, available,
reviewed binding. `status` is `resolved`, `ambiguous`, or `unavailable`.
Candidates include the canonical `tool`, mapped `arguments`, `input_schema`,
kind, unit, resolution, coverage, quality, and reasons for rejection. Equal
available candidates remain ambiguous. Credentials are configured on the
operator side and must never be placed in `arguments`.

## The fourteen workflow IDs

The registry currently defines these fourteen IDs. The capability order in this
table is the order used for the workflow's `artifacts` map and evidence.

| ID | Capabilities | Operation | Additional input |
| --- | --- | --- | --- |
| `consumption-forecast` | `get_energy_consumption`, `forecast_energy_consumption` | future consumption | Requires complete historical interval energy and a site timezone. |
| `forecast-bill` | `get_energy_consumption`, `forecast_energy_consumption`, `get_tariff`, `estimate_forecast_bill` | cost calculated from forecast | Requires explicit tariff validity covering the future horizon. |
| `yesterday-consumption` | `get_energy_consumption` | summary | A site is required when `start`/`end` are omitted. |
| `building-spike` | `get_energy_consumption`, optional observed `get_weather`, `explain_consumption_spike` | supported spike evidence | Accepts explicit equipment/weather artifacts and screening parameters in `spike_context`. |
| `electricity-cost` | `get_energy_consumption`, `get_tariff` | exact-interval cost | Both inputs must cover the same UTC starts and resolution. |
| `cheapest-battery` | `get_tariff`, `get_carbon_intensity` | constrained battery schedule | Requires a complete battery object and matching explicit tariff/carbon intervals. Objective is `cost`. |
| `cleanest-battery` | `get_tariff`, `get_carbon_intensity` | constrained battery schedule | Same inputs as `cheapest-battery`; objective is `carbon`. |
| `solar-consumption` | `get_energy_consumption`, `get_generation` | alignment or declared solar balance | `get_generation` must be supplied by a reviewed binding. |
| `solar-forecast` | `get_solar_forecast` when resolved, otherwise `get_weather` | direct interval-energy forecast summary or weather plus PV estimate | Direct forecasts require reviewed forecast/kWh semantics. Otherwise the workflow needs explicit PV model inputs. Equal direct forecast sources require a choice. |
| `grid-conditions` | `get_grid_generation`, `get_carbon_intensity`, `analyse_grid_conditions` | combined grid comparison | Requires exact UTC starts; explicit endpoints must match when supplied. |
| `power-flow` | `run_power_flow` | source evidence | Executes the reviewed simulation tool and returns its result as evidence. |
| `building-comparison` | `get_energy_consumption` | calendar comparison or alignment | Without `comparison_artifact`, runs one-series calendar comparison. With it, requires matching metered units and aligns the two artifacts. |
| `tariff-comparison` | `get_energy_consumption`, `get_tariff` | cost plus alternative cost | Requires `alternative_tariff`, an existing same-session artifact with exact coverage. |
| `energy-baseline` | `get_energy_consumption` | rolling baseline | `window` is the number of previous observations; default is 4. |

The descriptions in `skills.py` are guidance; the table above follows the
executable `RECIPES` and `run_skill` branches. In particular, `building-spike`
fetches consumption and optionally observed weather. `solar-consumption` uses
consumption plus generation. A public grid series cannot silently substitute
for a site's generation or telemetry.

See [consumption forecasting](consumption-forecasting.md) for the two forecast
recipes and their separate request fields.

## Workflow-specific contracts and pitfalls

### `yesterday-consumption`

With an explicit range, pass both `start` and `end`. With no range, pass
`site_id` when creating the session. The workflow computes the previous local
calendar day from the site's IANA timezone, then sends explicit offset-aware
midnight bounds to the provider. A DST transition therefore has 23 or 25 local
hours; it is not forced into 24.

The selected source must be interval energy, normally metered `kWh`. A power
series needs explicit `integrate_power`, and a cumulative counter needs explicit
`counter` conversion before it can be summed. Missing or late intervals remain
visible in the evidence and should be checked before treating the sum as a
complete day.

### `building-spike`

The workflow compares each interval with the preceding available observations,
using a default window of four, a load/baseline ratio of two and a minimum excess
of 0.1 kWh. Inputs must declare interval energy and explicit matching endpoints.
Gaps remain visible; the baseline does not fill missing observations.

`spike_context` accepts `weather_artifact`, `equipment_artifacts` and a
`parameters` object. Parameters include `window`, `spike_ratio`,
`min_excess_kwh`, column names and `equipment_end_column`. Weather is resolved
as observed `get_weather` when available. Future weather is never relabeled as
observed evidence. Equipment artifacts must be explicitly supplied, belong to
the scoped user/session, match the site's intervals, and fit within site load.

The result contains `intervals`, `spikes` and `summary`. Supported explanations
identify coincident equipment increases or sufficiently supported observed
weather associations. They include source references and causal limits.
Missing evidence is reported alongside the explanations.

### `electricity-cost`

The first input must be interval energy and the second must be a price rate.
The current workbench accepts energy units `Wh`, `kWh`, and `MWh`, and rates in
`GBP_pence`, `p`, `GBP`, `USD`, or `EUR` per `Wh`, `kWh`, or `MWh`. It converts
both to kWh and major currency per kWh. Carbon rates use `gCO2` or `gCO2e`
(`g`/`kg`) per `Wh`, `kWh`, or `MWh` and are used by the `carbon` operation.

Cost uses exact UTC timestamp equality, matching declared or inferred
resolution, and complete rate coverage. There is no nearest timestamp, forward
fill, interpolation, or implicit interval conversion. If either input has an
explicit `end` column, both must have it and corresponding durations must match.
Missing energy produces a null cost; a missing rate or uncovered interval is an
error. A `tariff-comparison` call applies these same rules to its alternative.

### `cheapest-battery` and `cleanest-battery`

The tariff must use `p/kWh` or `GBP/kWh`; carbon must use `gCO2/kWh` or
`gCO2e/kWh`. Both sources require explicit interval ends, using `end`, `to`,
or `interval_end`. The shared alignment operation parses numeric CSV values
and requires matching UTC starts and ends, without duplicates. Intervals must
be contiguous and cover the full requested window. The workflow persists the
alignment as evidence before calculating positive `duration_hours` values.
The resulting intervals pass
`load_kw: 0` and `pv_kw: 0` because the workflow has no load/PV input field.
Carbon totals retain the source's CO2 or CO2-equivalent basis. The result records
`carbon_species` and explicit gram units for per-interval and total emissions.

Supply a `battery` object with this shape:

```json
{
  "capacity_kwh": 8,
  "initial_soc_kwh": 2,
  "min_soc_kwh": 1,
  "max_charge_kw": 4,
  "max_discharge_kw": 4,
  "charge_efficiency": 0.95,
  "discharge_efficiency": 0.95,
  "target_final_soc_kwh": 4
}
```

`capacity_kwh`, `initial_soc_kwh`, `max_charge_kw`, and `max_discharge_kw` are
required. Capacity and both power limits must be strictly positive. The other
fields default to `0`, `0.95`, `0.95`, and the initial state of charge,
respectively. The optimizer also supports the schema-level fields
`timezone`, `carbon_price_gbp_per_tonne`, `carbon_weight`, `carbon_species`,
`allow_grid_charging`, and `allow_grid_export`. The workflow supplies `intervals`,
`battery`, its objective, and the carbon species from the source unit. Plans are
simulated advice; they do not control equipment.

### `solar-consumption`

This workflow aligns interval consumption with site generation using exact UTC
starts and the optional `column` name. It does not estimate generation. A
generation result must come from a uniquely selected reviewed binding with a
declared kind and unit; `get_generation` and generic `telemetry` labels do not
authorize a source by themselves. Forecast, estimated, calculated, and metered
inputs retain their kinds in the evidence and lineage.

To calculate interval self-consumption and estimated grid exchange, supply:

```python
result = await session.skill(
    "solar-consumption",
    {
        "artifacts": {"get_energy_consumption": load_id, "get_generation": generation_id},
        "solar_balance": {"consumption_basis": "total_load", "storage_mode": "none"},
        "start": "2026-09-29T00:00:00Z",
        "end": "2026-09-29T03:00:00Z",
    },
)
```

This requires declared interval energy with explicit matching starts and ends.
Both series must cover the complete requested horizon without gaps. Wh and MWh
are converted to kWh. Negative, missing and nonfinite values are rejected.
Self-consumption is `min(load, generation)`; estimated import and export are
`max(load - generation, 0)` and `max(generation - load, 0)` respectively.
The calculated output contains `intervals` and a `summary`, including both
energy totals and fractions. Zero denominators produce null fractions.

The load source must represent total site demand. A grid-import reading alone
does not establish it. The caller must explicitly declare no storage. Interval
netting cannot recover opposing flows within an interval, so import/export
remain estimates rather than grid meter readings. Source forecasts retain their
kind in provenance. Without `solar_balance`, the workflow returns aligned values.

### `solar-forecast`

CSV forecast weather values are parsed through the same finite-number boundary
as workbench time series before entering the PV solver. Explicit missing values
are refused. Wind in km/h is converted to m/s; irradiance and temperature units
must satisfy the reviewed weather contract.

The weather source must be `forecast`. Its long-form rows must contain an
explicit timestamp, variable, value, and per-row unit. The current workflow
requires consistent units for each variable, shortwave radiation in `W/m²`,
`W/m2`, or `W/m^2`, temperature in `°C`, `C`, or `degC` when present, and wind
speed in `m/s` or `km/h` when present. It pivots the weather artifact and builds
`weather_rows` for the PV model. Duplicate timestamps or mixed variable units
are rejected.

The `solar` override is passed to `engineering.estimate_solar_generation`. Its
required fields are `latitude`, `longitude`, `timezone`, and `dc_capacity_kw`.
Optional fields include `elevation_m`, `surface_tilt_deg`,
`surface_azimuth_deg`, `inverter_capacity_kw`, `weather_source`, `weather_kind`,
`albedo`, `losses_fraction`, `gamma_pdc_per_c`, and `interval_hours`. The
workflow fills latitude, longitude, and timezone from the session site when
available. Model assumptions (PVWatts, losses, albedo, and missing DNI/DHI
handling) remain in the result. The final call uses the selected binding's
account, kind, unit, resolution, and asset expectations; a provider result
that changes any reviewed semantic is rejected instead of being relabeled.

### `grid-conditions` and `power-flow`

The grid workflow combines reviewed generation power and carbon intensity at
exact UTC starts. It reports aligned observations, generation statistics,
lower-carbon intervals and the generation peak. Explicit fuel classifications
can produce fuel shares; the workflow never invents a low-carbon taxonomy.
Source kinds, units and carbon species remain visible. These comparisons do not
establish grid stability, marginal emissions, or a site's generation/emissions.
A continuous horizon is claimed only when both sources have matching contiguous
explicit endpoints. `grid` accepts column mappings and an optional
`carbon_threshold_g_per_kwh`.

The built-in grid source is Elexon FUELHH in MW. Site PV `get_generation` remains
a separate reviewed capability. The power-flow workflow executes the reviewed
solver and returns simulation evidence.

When network arguments are constructed from saved source data, supply
`input_artifacts` with those artifact IDs. Capability execution checks each
reference in the current user/session before running the solver and retains
its source kind, unit and provenance in the result. These references do not
replace the explicit network arguments.

`power-flow` passes the provider-specific `arguments` to the selected simulation
tool. For the built-in pandapower tool, the request is an object with a required
`network` containing nonempty `buses`; each bus requires `id` and `vn_kv`.
Optional `lines` require `id`, `from_bus`, `to_bus`, `length_km`,
`r_ohm_per_km`, and `x_ohm_per_km`; optional `loads` require `id`, `bus`, and
`p_mw`; optional `ext_grid`/`external_grids` require `bus`; and optional
`generators` require `id`, `bus`, and `p_mw`. Network-level `sn_mva`, `f_hz`,
and `slack_bus` are optional. The result remains `simulated`; convergence and
balance error are evidence, not an instruction to switch equipment.

### `building-comparison`

Without `comparison_artifact`, this recipe is a calendar comparison of one
metered interval-energy series. Set `frequency` to `daily`, `weekly`, or
`monthly`; values are summed in the result's local timezone and compared with
the previous local period. Missing rows are excluded and counted, and a missing
prior period yields null comparison values.

With `comparison_artifact`, the second artifact must be in the same session,
must be `metered`, and must have the same unit as the first artifact. The
workflow changes the operation to exact `align`; it does not resample or fill
one building to match another.

### `tariff-comparison`

This is a cost calculation for the primary tariff plus a second cost calculation
for `alternative_tariff`. The alternative ID must already be an artifact owned
by the same user and session. Both tariffs are matched to the same energy
intervals with exact UTC starts, compatible resolution, and matching explicit
durations when `end` columns are present. No tariff is fetched from a name or
invented from a label.

### `energy-baseline`

The workflow calculates a rolling mean of the previous `window` observations,
excluding the current observation. It defaults to four observations and emits
the source value, `baseline`, and `residual`. The first `window` rows have a
null baseline. The `frequency` field is accepted and forwarded by the workflow
request, but the baseline itself is observation-based rather than calendar
resampled.

## Workbench time-series operations

For direct use, call `WORKBENCH_ENERGY_OPERATION` with one or two artifact IDs:

```json
{
  "operation": "cost",
  "artifact_ids": ["<energy-artifact>", "<rate-artifact>"],
  "parameters": {
    "timestamp": "timestamp",
    "column": "value",
    "second_timestamp": "timestamp",
    "second_column": "value",
    "frequency": "30min",
    "method": "trapezoid",
    "unit": "kWh"
  }
}
```

`parameters` accepts only `timestamp`, `column`, `second_timestamp`,
`second_column`, `frequency`, `start`, `end`, `minimum`, `maximum`, `window`,
`method`, and `unit`. The direct operation names are `filter`, `missing`,
`counter`, `integrate_power`, `cost`, `carbon`, `baseline`, `compare`,
`normalize`, `align`, and `solar_balance`. `frequency` is `15min`, `30min`, `1h`, `1D` for
interval operations and `daily`, `weekly`, or `monthly` for calendar comparison.

The pure workbench applies these rules:

- Every timestamp must be an explicit offset-aware ISO-8601 value. Values are
  normalized to UTC for equality; `NaT`, naive timestamps, duplicates, nested
  values, booleans, nonnumeric values, and nonfinite numbers are rejected.
- `filter` uses inclusive timestamp and numeric bounds. `missing` expands only
  between the first and last observed instant, marks absent rows with
  `missing: true`, enforces the requested grid, and rejects expansions over the
  output bound. It does not extrapolate before or after coverage.
- `counter` accepts energy-unit cumulative readings only. It needs a declared
  `resolution` on the input or a `frequency` parameter. The first and reset
  differences are null; a missing reading or a timestamp gap produces a null
  difference. Each row uses the previous observation as `timestamp` and the
  current observation as `end` and `counter_observed_at`. The initial null
  row covers one declared interval before the first observation; no energy
  is inferred there. Converted output declares `quantity_shape: interval`.
  Include a reading at the requested window end before differencing, then
  select the half-open window so its final interval is retained. Raw readings
  are nested under `counter_observation` with their unit so a numeric summary
  cannot mistake that supporting evidence for interval consumption.
- `integrate_power` accepts only `W`, `kW`, or `MW`. The default trapezoid method
  and optional `left` method use observed duration. A declared-resolution gap
  remains null; unobserved hours are never integrated. If every row has an
  explicit end column (the column name is `end` by default, selected with the
  `end` parameter), intervals must be positive, nonoverlapping, and match the
  declared frequency when one is supplied.
- `cost` and `carbon` require two exact aligned series. Rate/intensity coverage
  and UTC starts must match; mixed resolutions, mismatched explicit durations,
  missing rates, and duplicate or nearest matches are errors. Missing energy
  remains null rather than being invented. Cumulative or instantaneous energy
  is rejected until explicitly converted. Filtering, missing-row expansion,
  and unit normalization preserve the input quantity shape.
- `baseline` is a previous-observation rolling mean. `compare` groups energy in
  the result timezone by local daily, Monday-based weekly, or calendar monthly
  periods and records missing counts. `normalize` requires an explicit target
  unit in the same physical dimension, currency, and carbon species.
- `solar_balance` requires two declared interval-energy series and explicit
  `consumption_basis: total_load` and `storage_mode: none`. It returns kWh
  intervals and totals for load, generation, self-consumption and estimated
  import/export, retaining both source kinds and units in lineage.
- `align` is an exact UTC inner alignment with no fill or interpolation. A
  second column with the same name is emitted as `<column>_right` and the result
  unit is `mixed`. Explicit interval ends are retained after validation against
  the second source.

Operations are bounded to the workbench row and output limits. They produce a
calculated `EnergyResult` with `source: "workbench"`, `quality: "derived"`,
and a lineage entry containing the operation version and each input artifact's
ID, kind, source, unit, timezone, resolution, provider, site, asset,
`original_unit`, `field_units`, and provenance. Consistent provider/site/asset
and time coverage metadata is carried forward. Conflicting field units are
recorded as `mixed`; original units are retained when consistent. Derived
results never relabel input data as metered.

## Artifact lineage and source truth

Use `persist=True` on a provider or tool execution when a later step needs the
result. The returned `result.data.artifact_id` is scoped to the session's user;
passing an artifact from another user or session is rejected. Workflow-created
provider artifacts are persisted before analysis. The analysis call passes those
IDs as `input_artifacts`, so runtime provenance and workbench lineage both point
back to the original results.

An artifact records the source's declared `kind`, `unit`, timezone, optional
resolution and coverage metadata. `EnergyResult.original_unit` and
`field_units` preserve per-field semantics through normalization, joins, and
derived calculations. CSV imports explicitly warn that the caller declared the
kind and unit; the import is not hardware verification.

`get_generation`, `get_grid_generation`, and `telemetry` deserve special care.
They are connector capability labels, not interchangeable data sources. A
reviewed `CapabilityBinding` must identify the exact tool, account or asset,
kind, unit, resolution, coverage, and argument mapping. The grid-generation
binding is distinct from a site-PV generation binding even when both expose a
power-like series. If resolution is unavailable or ambiguous, return that
status and configure or select a real source. Never create a synthetic
artifact, change a forecast into a measurement, or substitute grid generation
for site generation.

When a workflow has a requested window, fetched and reused artifacts pass through
`WORKBENCH_WINDOW` using an inclusive start and exclusive end. This preserves
source kinds and values, records the original artifact in lineage, and reports
missing timestamp intervals against a compatible declared resolution. Unknown
resolution leaves completeness unverified. A boundary cutting an energy interval
fails instead of prorating it. Alternative tariffs and comparison artifacts use
the same window. Without a requested window, artifacts retain their full range.

## Explicit telemetry conversion in consumption workflows

A preloaded measured counter or power artifact can supply a consumption
workflow through an explicit `consumption_transform`. Conversion runs before
window selection so the final boundary observation remains available.

```python
result = await session.skill(
    "yesterday-consumption",
    {
        "start": "2026-09-29T00:00:00Z",
        "end": "2026-09-30T00:00:00Z",
        "artifacts": {"get_energy_consumption": counter_artifact_id},
        "consumption_transform": {
            "operation": "counter",
            "parameters": {"column": "cumulative_kwh", "frequency": "30min"},
        },
    },
)
```

The input must declare `kind: metered`, `quantity_shape: counter` and an
energy unit. Supply observations spanning both boundaries of the requested
window. Resets, missing observations and gaps produce null intervals.

For measured power, declare `quantity_shape: instantaneous` and W, kW or MW,
then use `operation: integrate_power`. Its parameters can specify `column`,
`frequency`, and `method: left` or `method: trapezoid`. The resulting numeric
column is `energy`. The workflow uses that column by default.

Both conversions produce calculated interval energy. The workflow records the
original measured artifact, operation and converted artifact in its evidence.
It does not relabel calculated energy as measured or accept simulated input.
Direct use of arbitrary calculated consumption artifacts remains rejected.
This option requires a preloaded artifact; it does not infer extra provider
history or silently extend a provider query.

Cost calculations still require matching tariff coverage and explicit interval
ends when consumption has explicit ends. The workbench's `second_end` parameter
selects the second input's end column. Workflows recognize `end`, `to`, and
`interval_end` without altering the supplied timestamps or durations.

## Partitioned sources

[Large time-series datasets](large-timeseries.md) can supply bounded windows
to the existing recipes. Approved CSV dataset bindings filter while streaming;
`WORKBENCH_WINDOW` materializes the selected observations and preserves their
kind. Cadence, units and coverage still need to satisfy each recipe.
