# Energy Agent Tools

An MIT-licensed, self-hostable gateway for agents that work with energy data and
engineering models. Agents discover a few actions at a time, resolve reviewed
capabilities, and execute through one runtime with user, site, account, asset and
artifact scope.

The platform includes encrypted connections and OAuth PKCE, authenticated HTTP
and MCP hosting, Python and TypeScript SDKs, executable workflows, and local time-series
analysis. Results retain their physical unit, measurement kind, source, input
lineage and warnings. A forecast or simulation never becomes a meter reading.

Workspace managers can [connect reviewed custom MCP servers](docs/custom-mcp-onboarding.md), map them to sites, and expose selected tools through the gateway.

The [web console](apps/web/README.md) connects supported systems, maps sites and
assets, manages shared access and agent keys, and displays skills, jobs, and
execution activity. It requires the current-source gateway. Octopus, approved
Home Assistant instances, and reviewed custom MCP servers have managed connection
flows. Other provider forms and the complete control plane remain under development.

## Start locally

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/bferanmi806-sketch/energy-agent-tools.git
cd energy-agent-tools
uv sync --all-extras
uv run energy-agent validate
uv run energy-agent serve --config examples/config.json
```

The default MCP transport is stdio. The `engineering` extra supplies pvlib,
pandapower and windpowerlib. The `network-solvers` extra supplies PyPSA and
pandapipes. Missing optional packages produce explicit unavailability.
Private services need operator-configured credentials; no agent supplies secrets.

The example CSV is synthetic and its measurement label is declared input metadata.
Run `uv run python examples/reference_agent.py` for a deterministic MCP walkthrough
or `uv run python examples/bound_sdk.py` for the bound SDK.

The [consumption forecast workflow](docs/consumption-forecasting.md) retrieves
meter history, attempts relevant weather context, predicts future intervals
and can estimate a bill from covering tariffs. It preserves measured, estimated
and forecast source kinds. The offline reference is
`uv run python examples/reference_projects/forecast_workflow.py`.

## Agent interface

The endpoint exports eleven helpers:

- `ENERGY_SEARCH_TOOLS`
- `ENERGY_GET_TOOL`
- `ENERGY_MANAGE_CONNECTIONS`
- `ENERGY_MULTI_EXECUTE_TOOL`
- `ENERGY_LIST_TOOLKITS`
- `ENERGY_LIST_SKILLS`
- `ENERGY_SITE_CONTEXT`
- `ENERGY_RESOLVE_CAPABILITY`
- `ENERGY_EXECUTE_CAPABILITY`
- `ENERGY_RUN_SKILL`
- `ENERGY_SIMULATION_JOB`

Search returns bounded schemas. Capability resolution checks reviewed argument
mappings, credentials, account pins, asset scope, kind, unit, resolution and declared
coverage. Equal candidates require a source choice. Unreviewed telemetry remains
unavailable to generic execution until an operator defines its physical meaning.

Large results stay in scoped SQLite artifacts. Analysis includes bounded filtering,
UTC alignment, missing intervals, counter differences, power integration, cost,
carbon, baselines, calendar comparison, resampling and anomaly screening.
Twelve executable recipes combine these operations through the same runtime.
See [workflows](docs/workflows.md), [Python SDK usage](docs/sdk.md), and the
[TypeScript SDK](packages/typescript/README.md). The TypeScript package is an
initial developer release in the current source; it is not published to npm.

## Connections and hosting

For local clients, copy [the MCP configuration](examples/mcp-config.json) and replace
the repository path. `serve --transport streamable-http` binds a fixed identity to
loopback. For authenticated multi-user ingress use `energy-agent host`, with
operator-provisioned bearer-token digests and site permissions.

For a workspace that connects systems before creating sites, use
[`bootstrap` and managed hosting](docs/managed-workspaces.md). The management
key can provision owned sites and scoped agent keys through the gateway.

Connections support environment references or an encrypted local vault. OAuth
supports one-time PKCE state, a loopback callback, refresh and revocation. The
operator supplies the vault key and provider configuration. Credentials are absent
from agent schemas and public connection records.

Read [authentication](docs/authentication.md) and [self-hosting](docs/self-hosting.md)
for configuration, lifecycle commands, token rotation, session limits and retention.

## Connector coverage

Public HTTP adapters cover GB carbon intensity, Open-Meteo, Octopus tariffs,
Elexon and the NESO data portal. Credentialed adapters cover Octopus meters,
Home Assistant, Emoncms, Electricity Maps v4 and ENTSO-E. Numerical adapters use
real pvlib, windpowerlib, pandapower, PyPSA, pandapipes and SciPy. Local CSV,
read-only SQLite, reviewed MCP imports and fixed executables support operator data.
An optional EnergyPlus adapter requires a trusted installed executable and models.

[The catalogue](docs/connectors.md) distinguishes live public probes, contract
fixtures, numerical tests and unavailable engines. Private-provider fixtures do
not establish access to a real installation.

## Safety and evidence

Default sessions permit reads, calculations, simulations and external data.
Configuration changes and physical control require explicit operator policy.
No connector in this release dispatches a device. Imported MCP schemas are
fingerprinted; upstream annotations do not grant permissions.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv build
```

See [verification](docs/verification.md), [real-agent evaluation](docs/agent-benchmark.md),
[limitations](docs/limitations.md), [architecture](docs/architecture.md), [sites and assets](docs/sites-and-assets.md),
[connector development](docs/connector-development.md), and the
[Composio audit](docs/composio-audit.md). This is a self-hosted platform with bounded
models and an operator-managed trust boundary. It does not claim Composio's
connector scale or production service history.

Provider datasets and engineering dependencies retain their own terms. See
[third-party notices](THIRD_PARTY_NOTICES.md).
