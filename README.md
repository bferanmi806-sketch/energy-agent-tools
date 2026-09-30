# Energy Agent Tools

An open, self-hostable integration layer giving AI agents one interoperable gateway
into energy data, metering, engineering software, simulations and energy tools.

Version 0.1 is a local Python service. Agents discover tools by intent through a
small MCP interface, then execute the selected actions. Every energy result has an
explicit data kind, unit, source, timezone, assumptions and provenance. Large
results stay in a private local workbench.

## Quickstart

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/bferanmi806-sketch/energy-agent-tools.git
cd energy-agent-tools
uv sync --extra dev --extra engineering
uv run energy-agent catalogue
uv run energy-agent serve --config examples/config.json
```

The default transport is MCP stdio. Public API tools need network access. Private
meter and telemetry tools need locally configured accounts and environment
credentials. Engineering dependencies are optional; install the `engineering`
extra for pvlib and pandapower.

See [architecture](docs/architecture.md), [Composio audit](docs/composio-audit.md),
[connector catalogue](docs/connectors.md), [connector guide](docs/connector-development.md),
[verification](docs/verification.md), and [limitations](docs/limitations.md).

## What is included

The shipped catalogue covers GB carbon intensity, weather/radiation/wind forecasts,
Octopus meter intervals and tariffs, Home Assistant and OpenEnergyMonitor
telemetry, Elexon grid data, pvlib solar estimates, pandapower AC studies, battery
scheduling and thermal calculations. Local CSV, executable and MCP adapters let
operators add approved data and existing tools. See the catalogue for status and
exact test evidence; private integrations require credentials and use fixtures in
our test suite.

The MCP endpoint exposes seven helpers:

- `ENERGY_SEARCH_TOOLS`
- `ENERGY_GET_TOOL`
- `ENERGY_MANAGE_CONNECTIONS`
- `ENERGY_MULTI_EXECUTE_TOOL`
- `ENERGY_LIST_TOOLKITS`
- `ENERGY_LIST_SKILLS`
- `ENERGY_SITE_CONTEXT`

Only selected search results include action schemas. Batch calls can persist data
locally and link `input_artifacts` so model results retain their input provenance.
The workbench supports summaries, resampling, timestamp joins, weather pivots and
anomaly screening. No tool accepts arbitrary Python code.

Six independent workflow guides cover yesterday's consumption, building spikes,
battery economics, solar versus consumption, grid conditions and power flow. The
Python SDK also exports schemas for OpenAI Chat, OpenAI Responses and Anthropic.

## Connect an MCP client

Copy [examples/mcp-config.json](examples/mcp-config.json) into your client's MCP
configuration and replace the repository path. The equivalent command is:

```sh
uv --directory /absolute/path/to/energy-agent-tools run energy-agent serve --config examples/config.json
```

The example imports a small synthetic CSV. Its metered label is a declared example
input, not data obtained from real hardware. Run the deterministic reference client:

```sh
uv run python examples/reference_agent.py
uv run python examples/sdk.py
```

For local streamable HTTP, use `uv run energy-agent serve --transport
streamable-http --config examples/config.json`. It binds to `127.0.0.1:8765/mcp`.
One process serves one configured identity. Internet-facing, multi-user hosting
needs application authentication and isolation outside this initial release.

## Configure a private account

Copy `examples/config.json` to ignored `local-config.json`, then add an account:

```json
{
  "id": "octopus-home",
  "user_id": "local",
  "site_id": "home",
  "toolkit": "octopus-energy-account",
  "auth": {"scheme": "basic", "credential_env": "OCTOPUS_API_KEY"},
  "settings": {"mpan": "YOUR_MPAN", "serial_number": "YOUR_METER_SERIAL"}
}
```

Set `OCTOPUS_API_KEY` in the server environment using your preferred secret manager.
Never supply its value to an agent. Home Assistant uses toolkit `home-assistant`,
bearer auth and `settings.base_url`. Emoncms uses `openenergymonitor`, API-key auth
and a base URL. Account settings can declare known units and data kind where a
provider's feed lacks physical metadata. Credentials stay outside connection lists.

## Safety and verification

Default sessions permit reads, calculations and simulations. Configuration writes,
physical-control and safety-critical actions require operator policy enablement.
There are no physical-control connectors in this release. Imported MCP and
executable actions are denied until their permissions are reviewed.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv build
```

Public live probes are opt-in: `uv run python examples/live_probe.py`. Offline
fixtures and real local solver tests run in CI. No autonomous LLM benchmark or
live private-meter integration is claimed. See the exact evidence in
[verification](docs/verification.md).

MIT licensed. Provider data and engineering software retain their own terms;
see [third-party notices](THIRD_PARTY_NOTICES.md).
