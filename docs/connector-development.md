# Connector development

A connector contributes a `Toolkit`, one or more JSON Schemas, and async
handlers. The registry is local and the runtime owns scope, credentials,
action policy, result validation, redaction and provenance. Never put an HTTP,
executable or MCP credential in an agent-facing tool argument.

## A small native connector

```python
from energy_agent_tools.models import Action, DataKind, EnergyResult, Tool, Toolkit, schema
from energy_agent_tools.registry import Registry


def register(registry: Registry) -> None:
    registry.add_toolkit(
        Toolkit(
            id="thermal",
            name="Thermal calculation",
            description="Local thermal arithmetic",
            runtime="native",
            status="experimental",
            version="1.0.0",
        )
    )

    async def handler(args, ctx):
        watts = args["conductance_w_per_k"] * args["delta_temperature_k"]
        return EnergyResult(
            data={"heat_loss_w": watts},
            kind=DataKind.CALCULATED,
            unit="W",
            source="thermal",
            quality="derived",
            assumptions=["Steady-state heat transfer; conductance is supplied by caller."],
        )

    registry.add(
        Tool(
            name="THERMAL_HEAT_LOSS",
            toolkit="thermal",
            resource_scope="session",
            description="Calculate steady-state thermal heat loss",
            input_schema=schema(
                {
                    "conductance_w_per_k": {"type": "number", "minimum": 0},
                    "delta_temperature_k": {"type": "number"},
                },
                ["conductance_w_per_k", "delta_temperature_k"],
            ),
            capabilities=["perform_engineering_calculation"],
            actions={Action.CALCULATE},
            result_kind=DataKind.CALCULATED,
            result_unit="W",
        ),
        handler,
    )
```

Keep the provider contract explicit and bounded. The runtime injects an
`ExecutionContext` containing the scoped session, selected account, credential
(only at the boundary), HTTP client and workbench. A handler should return a
typed `EnergyResult`, preserve original units and timestamps, and add
assumptions and provider provenance. Raise `EnergyError(code, safe_message)`
for expected failures; never include response bodies, authenticated URLs or
secrets in an exception.

Every connector must declare `Tool.resource_scope` from its actual handler
resources. `session` covers caller-supplied inputs and already-authorized
artifacts, including pure calculations. `public` means an explicitly public
provider API. `account` requires a current owned connection as well as the
existing user/site account checks. `operator` covers configured shared file or
model roots, executable environments and MCP transports. The default
`unclassified` fails closed in hosted sessions. Action and review metadata do
not grant resource access. Local Python and CLI sessions retain operator use;
REST clients cannot choose the session's server-controlled access mode.

For a capability that can be selected across providers, add a reviewed
`CapabilityBinding` with the exact tool, account or asset, measurement kind,
unit, resolution, coverage interval, quality, argument mapping and version.
Labels alone do not make schemas compatible. Cumulative counters, interval
energy and power require different bindings and transformations. Equal
available candidates remain ambiguous until the caller chooses a source.

Local Python libraries must construct models from explicit bounded parameters,
report assumptions and library versions, and preserve `simulated` or
`estimated` kinds. Do not execute agent-supplied Python. Missing optional
libraries should remain discoverable with `status="unavailable"` or return a
structured `dependency_unavailable` error at execution time.

## HTTP connectors

HTTP handlers own a fixed provider origin or use an operator-configured account
origin. Validate provider JSON at the boundary, preserve nulls and provider
units, bound pages and rows, enforce time ranges, and reject cross-origin
credential forwarding. Account settings may contain identifiers and public
configuration, but model validation rejects credential-shaped settings. Use
the provider's official documentation URL in the toolkit metadata and record
whether the integration is stable, experimental or credential-gated.

## Existing MCP servers

MCP imports use the official Python client over local stdio or remote
streamable HTTP. Configure them asynchronously after building the agent:

```python
from energy_agent_tools.app import build_agent, configure_mcp

agent = build_agent(state_dir, config)
await configure_mcp(agent, config)
```

The lower-level bridge can be used directly:

```python
from energy_agent_tools.connectors.mcp_bridge import inspect_mcp, import_mcp

manifest = await inspect_mcp(
    "energy_server",
    command="python",
    args=["/opt/energy/server.py"],
    version="2026.10",
)

await import_mcp(
    registry,
    "energy_server",
    command="python",
    args=["/opt/energy/server.py"],
    version="2026.10",
    expected_schema_digest=manifest["schema_digest"],
    tool_metadata={
        "read_meter": {
            "reviewed": True,
            "action": "read-only",
            "kind": "metered",
            "unit": "kWh",
            "capabilities": ["get_energy_consumption"],
        }
    },
)
```

An imported name is `<toolkit_id>.<upstream_name>` and remains stable across
provider versions; version metadata is carried on the toolkit and tool. A
reviewed import requires explicit operator metadata with `reviewed: true`, a
kind, a unit and `action` or `actions`. Without that complete contract the
tool remains `reviewed: false`; without an action it defaults to
`configuration-write`, which a default session rejects. Upstream MCP
annotations such as `readOnlyHint` are untrusted and never grant permission.

`inspect_mcp` and `inspect_mcp_manifest` produce credential-safe manifests with
upstream names, per-tool schema hashes, a deterministic SHA-256 aggregate
digest and the review policy. Import rejects a mismatched
`expected_schema_digest` atomically. Each execution also re-lists the server's
tools after initialization and refuses to call a selected tool if its schema
has changed, disappeared or duplicated. Runtime connections are short-lived;
the bridge does not retain a long-lived MCP session.

For authenticated discovery, pass `discovery_auth` with an environment
credential reference. Runtime accounts use their own scoped auth configuration;
stdio injection requires `credential_env`. Remote URLs and credentials do not
enter provenance or inspection manifests. OAuth login and refresh are owned by
the connection lifecycle, not by the MCP schema importer.

## Fixed executables

Register an operator-owned command with
`register_executable(registry, toolkit_id, name, argv, ...)` or the equivalent
`command=`/`args=` aliases. The adapter sends agent arguments as JSON stdin,
never through a shell, and bounds output, timeout, inherited environment and
process cleanup. JSON stdout can be an `EnergyResult` or plain data wrapped by
declared metadata. Unreviewed executable actions default to
`configuration-write`; no executable is a physical-control connector by
default.

## Workbench and artifact lineage

Use workbench tools for operations over persisted results instead of returning
unbounded data. The local workbench provides summaries, anomaly screening,
DST-aware resampling, exact UTC joins, weather pivots and bounded filter,
missingness, counter, power integration, tariff, carbon, baseline,
comparison, normalization and alignment operations. Validate explicit offsets,
duplicate timestamps, compatible units and coverage before calculation. Derived
results retain input artifact IDs, kinds, units, sources and provenance.

## Plugins, scaffolding and validation

Installable connectors register an entry point in the
`energy_agent_tools.connectors` group. `energy-agent scaffold --output PATH
--toolkit NAME` creates a starter module. `energy-agent validate` runs
structural checks for valid JSON Schemas, registered handlers/toolkits,
nonempty versions, actions and object-shaped arguments. It does not prove live
provider correctness, numerical validity or production readiness.

Every connector contribution must include official docs links, a declared
status, successful and malformed-response tests, credential behavior where
relevant, bounded input/output tests, and meaningful numerical or protocol
assertions. Use the official MCP client for transport tests and keep fixture,
public-live and private-provider evidence separate. Run:

```sh
uv run energy-agent validate --config examples/config.json
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv build
```

Provider adapters format registered helper schemas for OpenAI Chat, OpenAI
Responses and Anthropic. Use `resolve_provider_name` to map provider aliases
back to canonical tool names; alias collisions must fail rather than execute
the wrong tool.
