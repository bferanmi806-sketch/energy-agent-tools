# Network solver connectors

Energy Agent Tools exposes two optional local solver adapters for small, explicit
network studies:

| Tool | Solver | Result | Status |
| --- | --- | --- | --- |
| `pypsa.power_flow` | PyPSA non-linear AC power flow | `simulated`, MW/Mvar/pu/degree | Experimental |
| `pandapipes.pipeflow` | pandapipes hydraulic pipeflow | `simulated`, bar/K/kg/s/m/s | Experimental |

Both adapters are self-hosted. They construct a solver model from validated JSON
and never accept a path to a solver file, arbitrary solver code, or free-form
solver options. Each handler has explicit bounds of 100 buses or junctions, 500
branches, and 500 node elements. Numerical results include the solver version,
model name, assumptions, and convergence quality in the result envelope.

## PyPSA power flow

The input uses bus nominal voltage in kV, line resistance and reactance in total
ohms (or both per-kilometre with `length_km`), load setpoints in MW/Mvar, and a
declared `slack_bus`. A line's `s_nom_mva` defaults to the network `base_mva`
(100 MVA). PyPSA's `Line.r` and `Line.x` inputs are physical ohms; PyPSA derives
the dependent values as `r_pu = r / v_nom_kv²` and `x_pu = x / v_nom_kv²`. The
adapter passes the caller's ohms through unchanged and returns both physical and
dependent per-unit values for review.

```json
{
  "network": {
    "buses": [
      {"id": "grid", "v_nom_kv": 11},
      {"id": "load", "v_nom_kv": 11}
    ],
    "lines": [
      {
        "id": "feeder",
        "from_bus": "grid",
        "to_bus": "load",
        "r_ohm": 0.2,
        "x_ohm": 0.4,
        "s_nom_mva": 100
      }
    ],
    "loads": [{"id": "house", "bus": "load", "p_mw": 1.0, "q_mvar": 0.2}],
    "slack_bus": "grid"
  }
}
```

The adapter adds one slack generator at the declared bus. Additional generators
may use fixed `PQ` or voltage-controlled `PV` setpoints. The output contains bus
voltages and angles, branch flows and losses, generation, loads, and active/reactive
balance totals. PyPSA reports the dispatch of a slack generator as a missing
time-series value; the adapter fills that value from the solved load, branch-loss,
and non-slack-generation balance so the public result does not contain `NaN`.

This connector is an engineering simulation. It does not certify protection,
thermal ratings, fault levels, dynamic stability, or operational safety.

## pandapipes hydraulic pipeflow

The input uses a fixed preset fluid (`water`, `gas`, `lgas`, `hgas`, `hydrogen`, or
`methane`), junction nominal pressures in absolute bar, fluid temperatures in K,
pipe lengths in km, diameters in metres, roughness in mm, and source/sink mass flows
in kg/s. A declared reference junction and pressure create the fixed external grid
needed by the hydraulic solver.

```json
{
  "network": {
    "fluid": "water",
    "junctions": [
      {"id": "source", "pn_bar": 5.0},
      {"id": "load", "pn_bar": 5.0}
    ],
    "pipes": [{
      "id": "branch",
      "from_junction": "source",
      "to_junction": "load",
      "length_km": 0.1,
      "diameter_m": 0.1
    }],
    "sources": [{"id": "inlet", "junction": "source", "mdot_kg_per_s": 0.1}],
    "sinks": [{"id": "demand", "junction": "load", "mdot_kg_per_s": 0.1}],
    "reference_junction": "source",
    "reference_pressure_bar": 5.0
  }
}
```

The output contains junction pressures and temperatures, pipe flows and velocities,
and a mass-balance check that includes the solved external-grid flow. The connector
uses the upstream fluid presets and hydraulic mode; it does not claim to model
temperature transport, transient operation, pump controls, or gas composition
changes. Use `EnergyPlus` or a dedicated thermal-network model when those effects
are required.

## Optional dependencies and qualification

The base package does not import either solver. Install the optional network-solver
dependencies in the environment that runs the connector, then inspect the toolkit
status through the registry. When a dependency is missing, the toolkit is advertised
as `unavailable` and execution returns `dependency_unavailable` without attempting a
fallback calculation.

The repository tests run a real two-bus PyPSA AC flow and real two-junction water and
gas pandapipes pipeflows when the optional dependencies are installed. The test
fixtures assert convergence, voltage or pressure response, flow direction, and
power/mass balance. The current release keeps both toolkits experimental because
larger topologies, model-file import, dynamic studies, thermal transport, and
operational validation are intentionally outside this bounded connector contract.

## Upstream contracts

The adapter follows the documented APIs and unit conventions of the upstream
projects:

* [PyPSA power flow guide](https://docs.pypsa.org/latest/user-guide/power-flow/)
  documents `Network.pf()`, PQ/PV/Slack controls, and the solved bus and branch
  quantities.
* [PyPSA quickstart power-flow example](https://docs.pypsa.org/latest/examples/example-2/)
  shows explicit buses, loads, generators, lines, and slack balancing.
* [pandapipes pipeflow guide](https://pandapipes.readthedocs.io/en/latest/pipeflow/)
  documents hydraulic pipeflow modes and convergence behavior.
* [pandapipes pipe component](https://pandapipes.readthedocs.io/en/latest/components/pipe/pipe_component.html)
  defines pipe length, diameter, roughness, pressure, and flow result fields.
* [pandapipes external grid component](https://pandapipes.readthedocs.io/en/latest/components/ext_grid/ext_grid_component.html)
  defines fixed pressure/temperature reference nodes and external-grid mass flow.
* [pandapipes unit conventions](https://pandapipes.readthedocs.io/en/latest/about/units.html)
  records the upstream bar, km, metre, and kg/s conventions used by the adapter.
