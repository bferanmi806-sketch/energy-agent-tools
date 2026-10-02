# Six offline model workflow references

`examples/reference_projects/model_workflows.py` runs six Energy Agent Tools
recipes against synthetic CSVs. It uses the regular `EnergyAgentTools` SDK,
reviewed capability bindings, CSV import, session-scoped workbench artifacts,
and `session.skill(...)` calls. The script makes no network requests and
prints one JSON report with the six successful runs, source evidence, and a
real rejected-input outcome for each recipe.

It demonstrates the composable, self-hosted path: reviewed source mappings,
private session-scoped artifacts, offline models, and transparent provenance.

The model calls use the local libraries already used by the SDK: SciPy's MILP
solver for battery schedules, pvlib for solar estimates, and pandapower for AC
power flow. The CSV bindings make the source mapping explicit, including the
declared kind, unit, quantity shape, interval, site, asset, and file
provenance. Each imported row is synthetic. `physical_meter` is `false`, even
where a fixture is declared as metered.

Run from the repository root in an environment with the project dependencies:

```sh
PYTHONPATH=src:. python examples/reference_projects/model_workflows.py
```

The report contains exactly these recipes:

| Recipe | Demonstrated result | Rejected input |
| --- | --- | --- |
| `cheapest-battery` | SciPy schedule charges in the lowest-price hour. | Forecast coverage ends before the requested horizon. |
| `cleanest-battery` | SciPy schedule charges in the lowest-carbon hour, which differs from the cheapest hour. | Charge power cannot reach the requested final state of charge. |
| `solar-consumption` | Declared total load and no storage produce interval self-consumption and estimated import/export. | A requested value column does not exist. |
| `solar-forecast` | pvlib estimates AC output from long-form irradiance, temperature, and wind forecast rows. | Temperature is supplied in kelvin where the workflow requires Celsius. |
| `grid-conditions` | The workflow combines matched grid generation and carbon values and reports the generation peak and lower-carbon intervals. | The bound grid generation CSV is removed before a second fetch. |
| `power-flow` | pandapower solves a two-bus feeder with the CSV load snapshot. | A network without a slack reference is rejected. |

The battery schedule is advisory and cannot control a device. It includes
explicit charge and discharge limits, efficiencies, initial state, capacity,
and target state; it does not include degradation, standing charges, or demand
charges. The solar result is an estimate from forecast weather, not a
generation meter reading. The `solar-consumption` reference declares total load and no storage. Its
interval netting estimates cannot recover opposing flows within an interval
and are not grid meter readings. The `grid-conditions`
workflow compares source power and carbon intensity; it cannot establish local grid stability or marginal emissions. A balanced
steady-state power flow does not cover protection, transient behavior, or
operational switching. Its feeder input is identified in the report by its
session artifact and CSV provenance. The power-flow call supplies that artifact
as an input reference, and the solver result retains its source kind and unit
through the normal scoped capability gateway.

Run the subprocess acceptance check with the same project environment:

```sh
PYTHONPATH=src:. python -m pytest tests/test_model_workflow_reference_projects.py
```

Solar balance acceptance independently checks 1.65 kWh load, 0.95 kWh
generation, 0.85 kWh self-consumption, 0.80 kWh estimated import and 0.10 kWh
estimated export. These are synthetic interval-netting results.

Acceptance checks also compare feeder voltage drop and resistive loss against
first-order equations for the declared 11 kV line and 0.8 MW/0.2 Mvar load.
The standalone headless script uses the same supported bundled-font startup
mode described in [simulation jobs](simulation-jobs.md), avoiding macOS system
font discovery during numerical initialization.
