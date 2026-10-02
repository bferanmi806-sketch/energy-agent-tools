# Offline reference projects

These three executable projects show a complete path from a scoped site and
source artifact to a supported answer. They use the shipped `EnergyAgentTools`
SDK, reviewed capability bindings, local CSV import, and the same runtime used by
the MCP endpoint. The CSV rows are synthetic examples. Their `metered` label is
declared fixture metadata and does not claim that a physical meter was read.

Run them from the repository root after installing the engineering extra:

```sh
uv sync --extra engineering --extra dev
PYTHONPATH="$PWD/src:$PWD" uv run python examples/reference_projects/home_energy.py
PYTHONPATH="$PWD/src:$PWD" uv run python examples/reference_projects/building_energy.py
PYTHONPATH="$PWD/src:$PWD" uv run python examples/reference_projects/network_engineering.py
```

The projects write state only to temporary directories. They need no provider
credentials, account, API, model service, benchmark module, or network access.
`home_energy.py` uses pvlib for a PVWatts estimate and SciPy's local MILP battery
optimizer; `network_engineering.py` uses pandapower for an AC power flow. Those
packages are provided by the `engineering` extra. The building heat-loss
calculation is a base installation feature.

## Home meter, PV and battery

`home_energy.py` imports four half-hourly interval-energy rows, four caller-set
tariff rows and four rows of caller-supplied weather forecast data. It converts
each metered kWh interval to average kW for the battery model, asks pvlib to
estimate rooftop AC generation, and runs a locally constrained battery schedule
against the tariff assumptions.

The fixture's metered energy totals 1.30 kWh. Tariff and weather artifacts remain
`forecast`; the PV result is `estimated` and records the input weather kind; the
battery schedule is `simulated`. The example checks final state of charge,
non-increasing modeled cost, site and asset scope, and artifact lineage. It also
shows the capability refusal when a generic consumption request has no provider
account or reviewed CSV-to-capability mapping.

The example also runs the `electricity-cost` SDK workflow against the imported
meter and tariff artifacts. Exact interval alignment produces 0.28 GBP; the
workflow reads those local artifacts and makes no provider request. The meter
source is attached to `home-meter` and declares interval energy at 30-minute
resolution. Tariff rows declare forecast interval rates at the same resolution;
site weather rows declare instantaneous forecast values at 30-minute resolution.

The weather and tariff values are hand-authored assumptions, not current
forecasts or supplier prices. PV output depends on those weather rows and the
explicit 2 kW fixed-tilt model. Battery output is an advisory schedule, not a
control command; degradation, demand charges and export are omitted.

## Building consumption, weather and equipment

`building_energy.py` imports a four-hour synthetic consumption series and a
caller-supplied outdoor temperature forecast. A reviewed, asset-scoped
`calculate_heat_loss` binding applies explicit envelope areas and U-values at
20°C indoors and the final forecast temperature outdoors. The example
independently calculates the envelope UA and the 1.638 kW heat loss, then checks
the result against a 2 kW heat-pump rating supplied as asset metadata.

Consumption totals 4.2 kWh. The meter source is attached to
`office-main-meter` and declares interval energy at one-hour resolution. The
outdoor temperature source declares instantaneous forecast values at hourly
resolution; it remains a `forecast` artifact. The engineering result is
`calculated` and carries the forecast artifact in its provenance. Heat-pump
capacity and COP are caller metadata, not a product database lookup. The
steady-state calculation does not model thermal mass, schedules, solar gains,
internal gains, or a dynamic heat-pump performance curve.

## Engineering network

`network_engineering.py` imports a synthetic metered feeder snapshot (0.8 MW and
0.2 Mvar) and supplies an explicit two-bus, 11 kV, 1 km line model to
`engineering.run_power_flow`. A reviewed, asset-scoped capability binding
resolves the production pandapower handler. Assertions check the unchanged load,
solver convergence, a positive line loss, voltage drop, near-zero power balance
residual, pandapower provenance, and the source artifact link.

The topology, impedance and 50 Hz frequency are example assumptions. This is a
balanced steady-state snapshot; it does not model protection, faults, controls,
unbalanced phases, or equipment switching. The solver result is `simulated` and
must not be presented as a meter reading. The feeder load artifact is attached to
`feeder-head-meter` and declares instantaneous MW/Mvar values; its sampling
cadence is unknown and no resolution is declared.

## Evidence boundary

All local CSV access is restricted to the fixture directory by the production
connector. Import kind, unit, asset, quantity shape, and resolution are caller
declarations preserved with source filename provenance. The examples use
interval energy and an instantaneous network snapshot; they do not derive a
counter or integrate power. A deployed operator must review the physical
meaning, unit and scope of each real source before adding a capability binding.
A missing account or unreviewed mapping remains unavailable rather than being
inferred from a file name or asset name.

Run the focused executable check with:

```sh
PYTHONPATH="$PWD/src:$PWD" uv run pytest -q tests/test_reference_projects.py
```
