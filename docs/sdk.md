# Bound Python SDK

`EnergyAgentTools` is the asynchronous, provider-bound entry point for a
self-hosted instance. It builds the same local registry and policy-enforcing
runtime used by MCP. It does not create an OpenAI or Anthropic client and it
does not send a provider request on its own. It formats the local helper
schemas for provider function calling and dispatches a returned function call
back through the scoped local runtime.

The complete synthetic example is
[`examples/bound_sdk.py`](../examples/bound_sdk.py). It creates its own CSV
fixture in a temporary directory, uses no credential, formats all three
provider dialects, imports two artifacts, and calculates exact-interval cost.

## Lifecycle

```python
from pathlib import Path

from energy_agent_tools import EnergyAgentTools


async def run() -> None:
    async with EnergyAgentTools(
        Path(".energy-agent"),
        config,
        data_root=Path("examples/data"),
    ) as tools:
        session = tools.session("user-1", site_id="home")
        schemas = await session.tools("openai-responses")
        result = await session.skill("energy-baseline", {"window": 4})
```

The constructor is:

```python
EnergyAgentTools(
    root: Path | str,
    config: dict | None = None,
    *,
    data_root: Path | None = None,
    agent: EnergyAgent | None = None,
)
```

`root` is the private state directory. `data_root`, when supplied, is the
operator-approved directory for `CSV_READ_TIMESERIES`; it is not an arbitrary
filesystem capability. `config` may contain the build configuration fields
`user_id`, `site_id`, `accounts`, `sites`, `assets`, `data_root`, `mcp_servers`,
`bindings`, `vault`, `hosting`, and `plugins`. Credentials belong in an
operator-managed environment reference or encrypted vault, never in a tool
argument or provider schema.

`async with` calls `await initialize()` on entry and `await close()` on exit.
The explicit form is:

```python
tools = EnergyAgentTools(".energy-agent", config)
await tools.initialize()
try:
    session = tools.session("user-1", "home")
finally:
    await tools.close()
```

The build step registers local toolkits synchronously. If `config` contains
`mcp_servers`, importing those servers and rebuilding the capability resolver is
asynchronous, so `session()` raises until `initialize()` has completed. A
caller-supplied `agent` is treated as already initialized. Without configured
MCP imports, the context manager is still the recommended lifecycle because it
owns the HTTP client and encrypted vault close.

`tools.session(user_id, site_id=None, **kwargs)` creates a `BoundSession`. The
optional keyword arguments are the scoped `Session` fields, such as
`toolkits`, `allowed_actions`, and `account_ids`; creating a session does not
authenticate a user. An authenticated host must establish identity before it
constructs the session.

## Bound session methods

| Method | Async | Contract |
| --- | --- | --- |
| `tools(provider="openai-responses")` | yes | Format the eleven local MCP helpers for `openai`, `openai-responses`, or `anthropic`. |
| `search(query, limit=5)` | no | Return up to 10 scoped tool schemas, ranked by the local registry and dependency/connection availability. |
| `resolve(capability, **kwargs)` | no | Return reviewed candidates, exact mapped arguments, schemas, reasons, and `status`; a tie is not selected. |
| `execute(tool, arguments, **kwargs)` | yes | Execute a canonical registry tool through scope, action, schema, credential, result, and artifact policy. `persist=True` stores a scoped artifact. |
| `capability(capability, arguments=None, *, persist=False, **kwargs)` | yes | Resolve and execute one uniquely selected reviewed capability. |
| `skill(skill_id, parameters=None)` | yes | Run one of the twelve bounded workflows. See [workflows](workflows.md). |
| `job(operation, **kwargs)` | yes | Submit, inspect, resume, cancel or delete a bounded numerical job under the current user/site/session policy. |
| `dispatch(name, arguments)` | yes | Map a provider function name back to one canonical helper and invoke it through the local MCP server. |

Direct tool execution returns an envelope such as:

```json
{
  "ok": true,
  "execution_id": "…",
  "result": {
    "data": {"artifact_id": "…", "row_count": 48, "preview": []},
    "kind": "metered",
    "unit": "kWh",
    "source": "local-csv",
    "provenance": []
  }
}
```

With `persist=False`, small results are returned inline and larger results are
compacted to an artifact preview. With `persist=True`, `result.data.artifact_id`
is the handle for the same user's later workbench call. `session.capability`
returns the same execution envelope and includes the selected binding in a
failure's `resolution` field when no source is available.

## Provider schemas and dispatch

`await session.tools(provider)` returns the same ten search-first helpers as the
MCP server. The input JSON Schema is preserved; only the provider envelope and
tool name are adapted:

| Provider argument | Shape of one function |
| --- | --- |
| `"openai"` | `{ "type": "function", "function": { "name", "description", "parameters", "strict": false } }` for Chat Completions. |
| `"openai-responses"` | `{ "type": "function", "name", "description", "parameters", "strict": false }` for the Responses API. |
| `"anthropic"` | `{ "name", "description", "input_schema" }`. |

Names are normalized by replacing characters outside ASCII letters, digits,
`_`, and `-` with `_`, and are limited to 64 characters. The formatter rejects
colliding aliases. For example, `engineering.run_power_flow` is exposed as
`engineering_run_power_flow`; `ENERGY_SEARCH_TOOLS` is unchanged.

The SDK leaves the provider call to the application. A normal function-calling
loop is:

```python
schemas = await session.tools("openai-responses")
provider_response = await your_openai_responses_client(input="Find the meter tool.", tools=schemas)

for call in provider_response.output:
    if call.type == "function_call":
        output = await session.dispatch(call.name, json.loads(call.arguments))
        # Send output back to your provider client according to its API.
```

For OpenAI Chat Completions, use `choice.message.tool_calls[*].function.name`
and JSON-decode `function.arguments`. For Anthropic, use a `tool_use` block's
`name` and `input`. For OpenAI Responses, use each `function_call` item's
`name` and JSON-decode its `arguments`. Pass the provider name exactly as
returned to `dispatch`; it resolves the alias against the session's canonical
helper list and rejects unknown or ambiguous aliases. `dispatch` does not
accept a provider name or bypass policy; it calls the same local MCP helper as
`execute`.

For a local application that already knows the canonical helper, call
`session.execute` directly:

```python
import json

meter = await session.execute(
    "CSV_READ_TIMESERIES",
    {"file": "meter.csv", "kind": "metered", "unit": "kWh", "timezone": "UTC"},
    persist=True,
)
if not meter["ok"]:
    raise RuntimeError(meter["error"])
artifact_id = meter["result"]["data"]["artifact_id"]
summary = await session.execute(
    "WORKBENCH_SUMMARIZE", {"artifact_id": artifact_id, "column": "value"}
)
```

## Capability resolution without invented sources

`session.resolve` accepts the `CapabilityRequest` fields `capability`,
`arguments`, `asset_id`, `account_id`, `kind`, `unit`, `resolution`, and
`tool`. The resolver checks user/site/toolkit scope, reviewed binding state,
optional dependencies, account availability, actions, kind, unit, resolution,
coverage, and the exact tool schema. The selected entry contains the canonical
tool name, mapped arguments, input schema, account and asset IDs, and binding
version.

Generic labels are deliberately insufficient. `get_generation`,
`get_grid_generation`, and `telemetry` may appear in connector capability lists,
but they do not identify an account, asset, measurement kind, unit, resolution,
coverage, or argument mapping. A `selected: null` response with
`status: "unavailable"` or `"ambiguous"` is a stop condition: show the
candidates or require an explicit reviewed binding. Never fabricate a tool,
source, unit, or site series to make a provider call succeed. Site PV
`get_generation` and grid `get_grid_generation` are separate reviewed
contracts; the latter may expose Elexon FUELHH grid MW data and cannot stand in
for site PV kWh.

The same rule applies to workflows. `session.skill` persists each selected
capability result, returns a blocked capability when it cannot resolve, and
passes only real artifact IDs to the workbench. A grid or forecast result never
becomes a metered site result by being passed through the SDK.

An operator binding records the exact capability, tool, account or asset, kind,
unit, resolution, coverage, quality, preference, argument mapping, review
state, and version. The binding contract also supports `fixed_arguments`: an
operator-owned map of provider arguments that callers cannot replace. Use it
for stable dataset or selector choices such as an approved grid-generation
dataset; keep user time windows and other request fields in the normal
`arguments` map. A fixed argument is part of the reviewed source contract, not
a scientific inference made from a capability label.

## Local time-series execution

Workbench helpers are ordinary SDK calls. For example, an exact tariff cost
calculation uses two persisted artifacts:

```python
cost = await session.execute(
    "WORKBENCH_ENERGY_OPERATION",
    {
        "operation": "cost",
        "artifact_ids": [energy_id, tariff_id],
        "parameters": {
            "timestamp": "timestamp",
            "column": "value",
            "second_timestamp": "timestamp",
            "second_column": "value",
        },
    },
    input_artifacts=[energy_id, tariff_id],
)
```

The operation requires exact UTC timestamp and compatible-resolution coverage;
it never performs nearest matching or implicit fill. Power must be explicitly
integrated, counters explicitly differenced, and units must belong to the
supported registry. Derived results are `calculated`, have
`source: "workbench"`, and carry operation-version lineage for every input
artifact.
See [workflows](workflows.md#workbench-time-series-operations) for the full
operation contract, DST behavior, interval-end rules, and unit semantics.

Current-power capabilities require an observation timestamp and default to a
300-second freshness limit. `max_age_seconds` can set a bounded requirement.
Retrieval time alone does not establish observation freshness. Results retain
`quantity_shape` (`interval`, `instantaneous`, or `counter`) when reviewed or
reported by the provider. See [job lifecycle](simulation-jobs.md).

## Model input lineage

`session.capability` accepts `input_artifacts`, a list of up to ten same-session
artifact IDs. `ENERGY_EXECUTE_CAPABILITY` exposes the same field through MCP.
The normal execution gateway reads these artifacts before invoking the selected
handler and retains their source kind, unit and provenance in the output.
Foreign or expired references block execution.

The power-flow workflow accepts the same list at the top level:

```python
result = await session.skill(
    "power-flow",
    {
        "arguments": {"run_power_flow": network_arguments},
        "tools": {"run_power_flow": "engineering.run_power_flow"},
        "input_artifacts": [load_artifact_id],
    },
)
```

The caller still supplies the network model and identifies the source artifacts
used to construct it. Other workflows record their derived inputs internally
and reject this top-level field.
