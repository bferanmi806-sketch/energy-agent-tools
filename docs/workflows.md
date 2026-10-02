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
| `alternative_tariff` | string | Existing same-session tariff artifact for `tariff-comparison`. |
| `comparison_artifact` | string | Existing same-session metered interval-energy artifact for `building-comparison`. |

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

## The twelve workflow IDs

The registry currently defines these twelve IDs. The capability order in this
table is the order used for the workflow's `artifacts` map and evidence.

| ID | Capabilities | Operation | Additional input |
| --- | --- | --- | --- |
| `yesterday-consumption` | `get_energy_consumption` | summary | A site is required when `start`/`end` are omitted. |
| `building-spike` | `get_energy_consumption` | anomaly screening | Uses the workbench anomaly defaults; there is no workflow-level threshold field. |
| `electricity-cost` | `get_energy_consumption`, `get_tariff` | exact-interval cost | Both inputs must cover the same UTC starts and resolution. |
| `cheapest-battery` | `get_tariff`, `get_carbon_intensity` | constrained battery schedule | Requires a complete battery object and interval ends on tariff rows. Objective is `cost`. |
| `cleanest-battery` | `get_tariff`, `get_carbon_intensity` | constrained battery schedule | Same inputs as `cheapest-battery`; objective is `carbon`. |
| `solar-consumption` | `get_energy_consumption`, `get_generation` | exact alignment | `get_generation` must be supplied by a reviewed binding. |
| `solar-forecast` | `get_solar_forecast` when resolved, otherwise `get_weather` | direct interval-energy forecast summary or weather plus PV estimate | Direct forecasts require reviewed forecast/kWh semantics. Otherwise the workflow needs explicit PV model inputs. Equal direct forecast sources require a choice. |
| `grid-conditions` | `get_grid_generation`, `get_carbon_intensity` | source evidence | Fetches and returns the reviewed grid sources; no additional derived analysis branch currently runs. |
| `power-flow` | `run_power_flow` | source evidence | Executes the reviewed simulation tool and returns its result as evidence. |
| `building-comparison` | `get_energy_consumption` | calendar comparison or alignment | Without `comparison_artifact`, runs one-series calendar comparison. With it, requires matching metered units and aligns the two artifacts. |
| `tariff-comparison` | `get_energy_consumption`, `get_tariff` | cost plus alternative cost | Requires `alternative_tariff`, an existing same-session artifact with exact coverage. |
| `energy-baseline` | `get_energy_consumption` | rolling baseline | `window` is the number of previous observations; default is 4. |

The descriptions in `skills.py` are guidance; the table above follows the
executable `RECIPES` and `run_skill` branches. In particular, `building-spike`
fetches only consumption, `solar-consumption` uses consumption plus generation,
and the evidence workflows do not silently substitute a public grid series for
a site's generation or telemetry.

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

The workflow persists the consumption source and calls `WORKBENCH_ANOMALY` on
the selected `column`. Its current executable request has no `threshold`,
seasonality, weather, occupancy, or equipment-join field. The workbench uses a
robust median/MAD screen with the tool's default threshold and returns a bounded
preview. A counter reset, a missing interval, or a timezone error can look like
a spike; an identified outlier is a screening result, not a causal diagnosis.

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
`gCO2e/kWh`. Each tariff row needs `timestamp` and `to_price` (or `to`) so the
workflow can calculate a positive `duration_hours`. Carbon must cover every
tariff start exactly, without duplicates. The resulting intervals pass
`load_kw: 0` and `pv_kw: 0` because the workflow has no load/PV input field.

The optional `battery` object has this exact shape:

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
required. The other fields default to `0`, `0.95`, `0.95`, and the initial state
of charge, respectively. The optimizer also supports the schema-level fields
`timezone`, `carbon_price_gbp_per_tonne`, `carbon_weight`,
`allow_grid_charging`, and `allow_grid_export`, but the current workflow passes
only `intervals`, `battery`, and its objective (`cost` or `carbon`). Plans are
simulated advice; they do not control equipment.

### `solar-consumption`

This workflow aligns interval consumption with site generation using exact UTC
starts and the optional `column` name. It does not estimate generation. A
generation result must come from a uniquely selected reviewed binding with a
declared kind and unit; `get_generation` and generic `telemetry` labels do not
authorize a source by themselves. Forecast, estimated, calculated, and metered
inputs retain their kinds in the evidence and lineage.

### `solar-forecast`

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

These workflows return source evidence rather than a hidden conclusion. The
grid workflow requires reviewed `get_grid_generation` and carbon bindings and
preserves their actual source, units, coverage, and kinds. The built-in grid
generation binding is backed by the native Elexon FUELHH dataset and returns
grid MW data; a regional or national grid series is not a site's PV generation.
Site PV `get_generation` remains a separate operator-reviewed capability,
normally with kWh semantics. Generic `telemetry` requests and any generation
request without a reviewed source must stop with `capability_unavailable` or
`capability_ambiguous`; never fabricate a provider, an asset, or a measurement.

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
`normalize`, and `align`. `frequency` is `15min`, `30min`, `1h`, `1D` for
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
- `align` is an exact UTC inner alignment with no fill or interpolation. A
  second column with the same name is emitted as `<column>_right` and the result
  unit is `mixed`.

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
