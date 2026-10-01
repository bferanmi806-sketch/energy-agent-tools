# Third-party notices

Composio's session/toolkit/account and search-first gateway concepts informed the
design. Audited upstream source is MIT, copyright 2025 Sampark Inc. No Composio
source code is copied or bundled here. See docs/composio-audit.md. Future copying
of substantial code must preserve its full MIT licence notice.

Runtime dependencies retain their own licences. They are installed by the package
manager rather than vendored. Consult the installed versions for definitive terms.

| Dependency | Licence |
|---|---|
| MCP Python SDK | MIT |
| Pydantic | MIT |
| HTTPX | BSD-3-Clause |
| jsonschema | MIT |
| pandas | BSD-3-Clause |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| pvlib | BSD-3-Clause |
| pandapower | BSD-3-Clause |
| [windpowerlib](https://github.com/wind-python/windpowerlib) | MIT |
| [PyPSA](https://github.com/pypsa/pypsa) | MIT |
| [pandapipes](https://github.com/e2nIEE/pandapipes/blob/develop/LICENSE) | BSD-3-Clause |

Fetched data is not covered by this repository's MIT licence. Carbon Intensity
API data requires CC BY 4.0 attribution to the Carbon Intensity API/NESO. Open-Meteo
requires attribution and its free endpoint is intended for noncommercial use;
check its terms for commercial deployments. Elexon, Octopus, telemetry platforms
and imported MCP servers retain their respective data/service terms. Results
include provider names and provenance so applications can retain attribution.
