# Telemetry evaluation environments

`benchmarks.telemetry_environments` contains deterministic environments for
development scenarios that exercise the shipped connectors and workbench.
Each environment uses a fixed clock, a private site, a private account when a
provider needs one, and provider-shaped data stored below the test root.

The builder has this signature:

```python
build_telemetry_environment(case_id, root, state_dir) -> BuiltEnvironment
```

`BuiltEnvironment.agent` is the production `EnergyAgent` instance.
`BuiltEnvironment.context` contains the user, site, provider, timezone, fixed
clock, and half-open request window.
Call `await built.close()` after every run.

## Qualified cases

The current set is explicit in `QUALIFIED_TELEMETRY_CASE_IDS`.

| Case | Provider or source | Production boundary | Independent check |
| --- | --- | --- | --- |
| `dev_consumption_home_assistant` | Home Assistant history | `home_assistant.get_history` returns a `total_increasing` counter | 25 cumulative readings produce 24 kWh after the explicit `counter` operation |
| `dev_consumption_local_day_dublin` | OpenEnergyMonitor feed | `openenergymonitor.get_feed` returns interval kWh with a reviewed shape | The Europe/Dublin local day and the equivalent UTC day select different timestamps and totals |
| `dev_units_kw_kwh` | Operator-approved CSV | `CSV_READ_TIMESERIES` reads kW and `WORKBENCH_ENERGY_OPERATION` integrates an explicit interval | 5 kW for 30 minutes produces 2.5 kWh |
| `dev_counter_reset_quality` | OpenEnergyMonitor feed | `openenergymonitor.get_feed` returns a reviewed cumulative counter | The 9,998 to 12 kWh drop is marked `counter_reset`, and valid later deltas total 4 kWh |
| `dev_field_units_provenance` | Operator-approved CSV | `CSV_READ_TIMESERIES` reads the fields and `WORKBENCH_PIVOT` records units by variable | `power_kw` remains kW and `energy_kwh` remains kWh in `column_units` provenance |

The tests read `provider-truth.json` or the CSV before invoking the production
connector. That keeps numeric assertions independent from adapter output.
The tests also check site and asset scope, encrypted provider credentials,
source provenance, and a failure boundary for each transformation.

## Deliberate exclusion

`dev_consumption_counter_reset` remains unavailable. The production CSV
connector preserves the caller's declared kind and unit, but it does not carry
a reviewed `quantity_shape`. A cumulative counter must not enter a generic
consumption binding without an explicit counter declaration. The builder raises
`EnvironmentUnavailable` and records the reason in
`EXCLUDED_TELEMETRY_CASES`.

Held-out scenarios and the shared scenario readiness fields remain unchanged.

## Verification

Run the focused qualification suite with the branch source ahead of any
editable installation:

```bash
PYTHONPATH="$PWD/src:$PWD" path/to/python -m pytest -q tests/test_telemetry_environments.py
```

The suite currently contains seven tests. It passed with the repository's
Python 3.12 environment on 2026-10-02.
