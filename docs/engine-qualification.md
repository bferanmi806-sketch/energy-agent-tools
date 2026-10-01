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

## EnergyPlus qualification

The existing fixed-executable EnergyPlus adapter has also been qualified against
the official EnergyPlus 26.2.0 macOS x86_64 release. The archive was downloaded
from the [NREL EnergyPlus release](https://github.com/NatLabRockies/EnergyPlus/releases/tag/v26.2.0)
and its SHA-256 matched the GitHub release digest before extraction. The run used
the release's published `1ZoneUncontrolled3SurfaceZone.idf` example and its
bundled Golden, Colorado TMY3 EPW. The binary reported
`EnergyPlus, Version 26.2.0-4bd7a1f26f`.

The adapter completed with `--readvars`, emitted 8,808 CSV rows, and returned
finite values with explicit source fields. The official example includes equal and
opposite monthly `Other Equipment` loads. Across 14 monthly rows, the positive and
negative energy sums were `11,161,497,600 J` and `-11,161,497,600 J`; the maximum
absolute net was `0 J`. The evidence also records finite zone temperatures and
surface-convection and air-storage rates in `C` and `W`.

The committed evidence was generated with:

```shell
uv run python scripts/qualify_engines.py \
  --energyplus-root /path/to/EnergyPlus-26.2.0-... \
  --energyplus-archive /path/to/EnergyPlus-26.2.0-...tar.gz
```

The EnergyPlus qualification covers one official steady-state/annual example
through the existing bounded executable boundary. It does not establish accuracy
for arbitrary building models, HVAC designs, weather locations, or operational
decisions.
