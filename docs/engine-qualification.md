# Engineering engine qualification

Energy Agent Tools now has a bounded OpenDSS adapter that runs the real
DSS-Extensions engine in memory. The adapter is exposed as
`opendss.power_flow` when the optional `opendssdirect.py` dependency is installed.
It accepts an explicit JSON topology and returns simulated power-flow evidence with
per-phase voltages, line currents, losses, power-balance checks, and engine
provenance.

The supported boundary covers steady-state snapshot studies for small balanced and
unbalanced distribution networks. The adapter accepts buses, lines, an explicit
source, and constant-power loads. It builds those objects itself inside a fresh
OpenDSS context. It does not accept DSS text, file paths, redirects, includes,
arbitrary commands, or solver options. Topologies are bounded to 100 buses, 500
lines, and 500 loads; numeric values and identifiers are validated before the
engine is called.

Install the optional dependency in the environment that runs the connector:

```shell
uv pip install 'opendssdirect.py>=0.9.4,<0.10'
```

The qualification used OpenDSSDirect.py 0.9.4, DSS-Python 0.15.7, and the
DSS-Extensions C-API backend 0.14.5 on macOS. The package publishes native wheels
for macOS, Linux, and Windows, including Apple ARM and x86 variants. The adapter
keeps the integration experimental because the engine is an alternative OpenDSS
implementation and the contract intentionally excludes protection, fault, dynamic,
time-series, controller, and operational studies.

Run the qualification from a checkout with the optional dependency installed:

```shell
uv run python scripts/qualify_engines.py
```

The resulting evidence is checked into
[`docs/evidence/engine-qualification.json`](evidence/engine-qualification.json).
It records the exact engine version, the complete result envelopes, and independent
checks for convergence, power balance, balanced phase symmetry, and unbalanced
phase response. The same cases are executable through
[`tests/test_dss.py`](../tests/test_dss.py).

The upstream API references used for the boundary are [OpenDSSDirect.py
documentation](https://dss-extensions.org/OpenDSSDirect.py/), the [OpenDSSDirect.py
repository](https://github.com/dss-extensions/OpenDSSDirect.py), and the
[DSS-Extensions Python API overview](https://github.com/dss-extensions/dss-extensions/blob/main/docs/python_apis.md).

EnergyPlus remains outside this qualification. Its existing connector has a
trusted executable boundary, but this work did not install or claim a real
EnergyPlus engine/model run.
