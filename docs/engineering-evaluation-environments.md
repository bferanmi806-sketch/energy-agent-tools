# Engineering evaluation environments

The engineering environment builders are a qualification layer for the
frozen development scenarios in `benchmarks/scenarios.py`.  Each qualified
environment creates one isolated site, publishes its model inputs in the
asset's `metadata.engineering` object, binds that asset to the reviewed
production tool, and fixes the runtime clock.  `ENERGY_SITE_CONTEXT` can
therefore expose the exact model assumptions before an agent executes a
study.

The evidence path is:

1. Read the reviewed model from the selected site's asset metadata.
2. Resolve the reviewed capability with the requested asset ID.
3. Execute the real production `EnergyAgent` handler and native installed
   engine.
4. Check independent numeric truth, result scope, solver provenance, and the
   model's read-only constraints.

The qualified set is:

| Case | Production execution | Independent checks |
| --- | --- | --- |
| `dev_power_flow_two_bus` | `engineering.run_power_flow` with pandapower | Convergence, positive line loss, balance residual, load-bus voltage drop, asset/site lineage, pandapower provenance |
| `dev_heat_loss` | `engineering.calculate_heat_loss` | Independent UA × ΔT calculation equals 12 kW, explicit units, asset/site lineage, tool provenance |
| `dev_optimization_advisory` | `engineering.schedule_battery_charging` with SciPy MILP | SOC and charge/discharge bounds, no simultaneous charge/discharge, final SOC, positive cost saving, advisory assumptions, asset/site lineage |
| `dev_network_asset_scope` | `pypsa.power_flow` | Two stable assets resolve separately, selected line model appears in the result, convergence, balance residual, PyPSA provenance, asset/site lineage |

The builders use the shared `BuiltEnvironment` and `ScenarioContext` lifecycle
classes from `benchmarks/environments.py`.  They do not contact a provider or
use a fabricated solver response.  The HTTP client is an unreachable local
transport because these cases are native calculations; it exists only to
preserve the common environment lifecycle.

## Explicit exclusions

`dev_pypsa_capacity_constraint` remains unavailable.  Its prompt asks for a
comparison of dispatch under 1 MW and 2 MW line capacities, while the current
PyPSA connector only solves a supplied steady-state AC power flow.  The line
capacity is an input field, not an optimization constraint that produces a
dispatch comparison.  Substituting pandapower or reporting two identical
power-flow results would misrepresent the case.

`dev_bounded_simulation` remains unavailable.  Its prompt requires a read-only
network simulation with a 100-bus bound and an enforceable 30-second timeout.
The current durable job API exposes a bounded timeout for its pandapower
`power_flow` operation, but it has no PyPSA `run_network` operation.  Direct
PyPSA execution cannot provide the required timeout contract.  The builder
raises `EnvironmentUnavailable` with this reason so the parent harness cannot
silently count it as an agent success.

These exclusions are implementation evidence, not model-scoring results.  A
future qualification can add either case after the missing solver or job
contract is implemented and independently tested.
