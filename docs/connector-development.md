# Connector development

A connector is a toolkit manifest, one or more JSON Schemas and async handlers.
No HTTP, executable or MCP credential may be an agent tool parameter.

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
        ),
        handler,
    )
```

The runtime injects `ExecutionContext`, containing the scoped session, selected
account settings, credential, HTTP client and workbench. HTTP connectors must own
their fixed provider origin or use an operator-configured account origin. Validate
provider JSON, preserve nulls and units, limit pages and rows, and forbid
cross-origin credential forwarding. Raise `EnergyError(code, safe_message)` for
expected failures. Never include response bodies, authenticated URLs or secrets
in exceptions.

Local Python libraries should build models from explicit, bounded parameters and
report assumptions, model/library version and numerical validity. Do not execute
agent-supplied code. A simulation must stay `simulated` even if its output resembles
meter readings. Missing optional libraries produce structured availability errors.

## Existing MCP servers

Import through the SDK or the `mcp_servers` array in local configuration:

```python
from energy_agent_tools.connectors.mcp_bridge import import_mcp

await import_mcp(
    registry,
    "energy_server",
    command="python",
    args=["/path/to/server.py"],
    tool_metadata={
        "read_meter": {
            "actions": ["read-only"],
            "kind": "metered",
            "unit": "kWh",
            "capabilities": ["get_energy_consumption"],
        }
    },
)
```

Remote servers use `url="https://your-server.example/mcp"`. For authenticated
schema discovery, pass `discovery_auth` as an `AuthConfig` or JSON object with an
environment credential reference. Runtime accounts use their own auth config so
execution credentials stay user scoped. Stdio credential injection also requires
`credential_env` naming the child environment variable. Upstream remote OAuth
login/refresh is not automated. Review schemas and actions before enabling them.

Imported tool names are `<toolkit_id>.<upstream_name>`. Unreviewed actions are
configuration writes and are denied in default sessions. Upstream MCP annotations
are not permission grants. Results default to estimated unless reviewed metadata
specifies a data kind, and carry an unverified-output warning. Import is atomic;
failed metadata cannot leave a partial toolkit. Connections initialize and close
on every execution in this release, so long-lived MCP server state is not retained.

[PowerMCP](https://github.com/Power-Agent/PowerMCP) is a candidate for this import
path. Its engineering software, licences and platform prerequisites need separate
validation. This release does not claim that PowerMCP itself was integration tested.

## Fixed executables

Register a toolkit with `runtime="executable"`, then call
`register_executable(registry, toolkit_id, tool_name, argv, input_schema=...,
actions=["simulation"], kind="simulated", unit=...)`. `argv` is operator owned;
agent inputs go to JSON stdin, never a shell command. JSON stdout can be an
EnergyResult or plain data wrapped using declared metadata. The adapter bounds
output, times out, restricts inherited environment and cleans up the process.
Unreviewed executable actions default to configuration writes and are denied.

## Capabilities and test requirements

Use canonical energy capability IDs where physical semantics match. They aid
search and skill guidance; they do not make unlike provider schemas automatically
interchangeable. A cumulative energy counter is not interval consumption.

Every connector must include official docs links, status, success tests, malformed
response tests, credentials behavior where relevant and meaningful numerical or
protocol assertions. Use the official MCP client for transport tests, not an
in-process function mock. Separate fixture evidence from live evidence. Regenerate
manifests with `uv run energy-agent manifests --config examples/config.json`.

Provider adapters normalize names for OpenAI/Anthropic function-call requirements.
Use `resolve_provider_name` to map provider aliases back to registry names. Alias
collisions fail rather than executing the wrong tool. `input_artifacts` on batch
calls links calculation/simulation outputs to the kinds, units and provenance of
input datasets when callers translate provider rows into solver inputs.
