# Engineering evaluation environments

The engineering environment builders qualify frozen development scenarios in
`benchmarks/scenarios.py`. Each environment creates one isolated site,
publishes its model inputs in the asset's `metadata.engineering` object, binds
the asset to a reviewed production capability, and fixes the runtime clock.
`ENERGY_SITE_CONTEXT` can therefore expose the model assumptions before an
agent executes a study.

The evidence path is:

1. Read the reviewed model from the selected site's asset metadata.
2. Resolve the reviewed capability with the requested asset ID.
3. Execute the real production `EnergyAgent` handler or bounded job and native
   installed engine.
4. Check independent numeric truth, result scope, solver provenance, and the
   model's read-only constraints.

The qualified set is:

| Case | Production execution | Independent checks |
| --- | --- | --- |
| `dev_power_flow_two_bus` | `engineering.run_power_flow` with pandapower | Convergence, positive line loss, balance residual, load-bus voltage drop, asset/site lineage, pandapower provenance |
| `dev_pypsa_capacity_constraint` | `pypsa.optimize_dispatch` with PyPSA and HiGHS on two reviewed assets | Fixed one-hour snapshot; 1 MW case dispatches 1 MW remotely and 1 MW locally at 60 currency/hour; 2 MW case dispatches 2 MW remotely at 20 currency/hour; balance residual, solver provenance, asset/site lineage |
| `dev_heat_loss` | `engineering.calculate_heat_loss` | Independent UA × ΔT calculation equals 12 kW, explicit units, asset/site lineage, tool provenance |
| `dev_bounded_simulation` | `network_power_flow` durable subprocess job using `pypsa.power_flow` | Actual PyPSA result is simulated and converged; schema permits at most 100 buses; job manager process timeout is 30 seconds; private job files, user/site/toolkit access checks, owner-scoped cleanup, and store-lock release are exercised |
| `dev_optimization_advisory` | `engineering.schedule_battery_charging` with SciPy MILP | SOC and charge/discharge bounds, no simultaneous charge/discharge, final SOC, positive cost saving, advisory assumptions, asset/site lineage |
| `dev_network_asset_scope` | `pypsa.power_flow` | Two stable assets resolve separately, selected line model appears in the result, convergence, balance residual, PyPSA provenance, asset/site lineage |

The builders use the shared `BuiltEnvironment` and `ScenarioContext` lifecycle
classes from `benchmarks/environments.py`. They do not contact a provider or
use a fabricated solver response. The HTTP client is an unreachable local
transport because these cases are native calculations; it preserves the
common environment lifecycle.

## PyPSA qualification details

`dev_pypsa_capacity_constraint` uses the native `pypsa.optimize_dispatch`
toolkit operation, registered with a strict schema and a one-hour snapshot.
Each line-capacity case is a separate reviewed asset and binds the actual
`run_network_optimization` capability to that tool. The dispatch adapter uses
PyPSA's lossless linear network optimization with the fixed HiGHS backend. Its
input parser limits networks to 100 buses and 500 lines, and caps the combined
number of loads and generators at 500. This is a dispatch comparison; it does
not represent AC voltage, reactive power, or physical equipment control.

`dev_bounded_simulation` binds `run_power_flow` to the actual
`pypsa.power_flow` schema and records the durable job operation as
`network_power_flow`. The job manager starts the fixed numerical worker in a
subprocess and enforces its 30-second timeout; the PyPSA tool schema caps buses
at 100. The acceptance test submits and completes the real job, checks
`DataKind.SIMULATED` and PyPSA provenance, verifies private filesystem modes,
and exercises wrong-user, wrong-site, and disallowed-toolkit checks. Cleanup
is scoped to the owning user and session, and closing the environment releases
the job-store lock for a subsequent manager.

The durable job record retains its user, session, and site scope. The worker
result itself has neither a site ID nor a source asset ID because the job
protocol passes the network arguments and operation but no asset identifier
or site scope. The qualification therefore verifies asset inputs and job
scope separately and does not claim site or asset lineage on the returned job
result.
