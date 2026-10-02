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
| `solar-consumption` | Session artifacts align synthetic interval consumption and generation by timestamp. | A requested value column does not exist. |
| `solar-forecast` | pvlib estimates AC output from long-form irradiance, temperature, and wind forecast rows. | Temperature is supplied in kelvin where the workflow requires Celsius. |
| `grid-conditions` | The workflow returns the grid generation and carbon source outputs as separate evidence. | The bound grid generation CSV is removed before a second fetch. |
| `power-flow` | pandapower solves a two-bus feeder with the CSV load snapshot. | A network without a slack reference is rejected. |

The battery schedule is advisory and cannot control a device. It includes
explicit charge and discharge limits, efficiencies, initial state, capacity,
and target state; it does not include degradation, standing charges, or demand
charges. The solar result is an estimate from forecast weather, not a
generation meter reading. The `solar-consumption` workflow currently aligns
values only; import/export reconciliation remains missing. The `grid-conditions`
workflow is evidence-only and produces no combined grid metric. A balanced
steady-state power flow does not cover protection, transient behavior, or
operational switching. Its feeder input is identified in the report by its
session artifact and CSV provenance, while the current workflow result does
not copy that artifact ID into the solver provenance.

Run the subprocess acceptance check with the same project environment:

```sh
PYTHONPATH=src:. python -m pytest tests/test_model_workflow_reference_projects.py
```
