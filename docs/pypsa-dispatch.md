# PyPSA economic dispatch

`pypsa.optimize_dispatch` finds the least-cost generator dispatch for one fixed
one-hour snapshot on a small, caller-supplied electricity network. It builds a
PyPSA network in memory and solves a linear, lossless optimal power flow with
HiGHS.

The tool accepts one `network` object containing bus nominal voltages, lines
with reactance and MVA capacity, fixed real-power loads, and generators with
capacity and marginal cost. Marginal costs use the caller's currency per MWh;
the result reports the objective in that same caller currency per hour. No
currency is inferred or converted.

```json
{
  "network": {
    "buses": [
      {"id": "remote", "v_nom_kv": 11},
      {"id": "load", "v_nom_kv": 11}
    ],
    "lines": [
      {
        "id": "feeder",
        "from_bus": "remote",
        "to_bus": "load",
        "x_ohm": 0.4,
        "s_nom_mva": 2
      }
    ],
    "loads": [{"id": "demand", "bus": "load", "p_mw": 2}],
    "generators": [
      {
        "id": "remote-cheap",
        "bus": "remote",
        "p_nom_mw": 10,
        "marginal_cost": 10
      },
      {
        "id": "local-dear",
        "bus": "load",
        "p_nom_mw": 10,
        "marginal_cost": 50
      }
    ]
  }
}
```

The result includes each generator's MW dispatch, each line's signed MW flow
from `from_bus` to `to_bus`, line capacity and utilization, the load-balance
residual, and `objective_currency_per_hour`. The `assumptions` and provenance
fields identify the linear lossless model and the PyPSA and HiGHS versions.

Input bounds are 100 buses, 500 lines, and 500 total loads plus generators.
IDs are unique across all component types; line ends and generator/load buses
must reference declared buses. Bus voltage is at most 1,000 kV, generator and
line capacity is at most 10,000 MW/MVA, line reactance is at most 10,000 ohm,
load and marginal cost values are finite and nonnegative, and marginal cost is
at most 1,000,000 caller-currency/MWh. Generator capacities, line reactance,
line capacities, and nominal bus voltage must be positive.

The request cannot choose solver parameters or provide files or code. The
connector fixes the HiGHS solver, a 30-second solver time limit, and one solver
thread. This budget limits the optimizer; the simulation job gateway also
isolates the process and enforces its own wall-clock timeout when dispatch is
run as a job.

This model has no AC voltage magnitude, reactive power, losses, unit commitment,
or dynamic controls. A successful dispatch is a simulation result and does not
certify network feasibility or operating safety under AC conditions.
