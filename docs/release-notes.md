# Energy Agent Tools 0.1.0

Initial public preview of a self-hostable, search-first gateway to energy data and
engineering tools. Includes 18 actions across 10 configured toolkits, seven MCP
helpers, Python SDK/provider schema adapters, six energy workflow guides, private
local artifacts and conservative control policies.

HTTP integrations cover Carbon Intensity GB, Open-Meteo, Octopus consumption and
public tariffs, Home Assistant, Emoncms and Elexon. Local engineering includes
pvlib PVWatts, pandapower AC power flow, constrained battery scheduling and thermal
calculations. Existing MCP servers and fixed executables can be registered with
reviewed permissions and result semantics.

Local verification: 60 tests passed, Ruff checks/formatting passed, mypy passed
for 17 source files, wheel/source builds passed, and an isolated base-wheel install
passed. Five public live probes returned nonempty data. Private-provider tests
use fixtures; no real energy infrastructure was controlled. See
[verification](verification.md) for exact evidence and [catalogue](connectors.md)
for integration status.

This is a preview release. Interactive OAuth/token refresh, public multi-user
service authentication, isolated untrusted Python execution and several researched
energy engines remain deferred. The provided MCP service binds to local stdio or
loopback HTTP. Read [limitations](limitations.md) before deploying it.

Install from source with `uv sync --extra engineering`, or install the attached
wheel with `pip install energy_agent_tools-0.1.0-py3-none-any.whl`. Add the
`engineering` extra to install pvlib/pandapower. The repository is MIT licensed;
provider data and dependencies retain their own licences.
