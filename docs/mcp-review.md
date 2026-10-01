# MCP review and schema policy

Energy Agent Tools treats an MCP server as an untrusted, versioned provider.
The bridge uses the official MCP client for stdio and streamable HTTP
connections, but an upstream server does not receive permission to read,
calculate, write, or control an energy system merely because it advertises an
annotation such as `readOnlyHint`.

## Review contract

An imported tool is `reviewed: false` unless its operator metadata contains all
of the following:

```json
{
  "reviewed": true,
  "action": "read-only",
  "kind": "metered",
  "unit": "kWh"
}
```

`actions` may be used instead of `action`. The metadata is the operator's
declaration of the action boundary and result semantics. Upstream annotations,
descriptions, and titles are retained only as untrusted hints. A tool with no
operator action metadata defaults to `configuration-write` and is denied by a
normal session policy. A declared kind without the complete review contract
does not populate `Tool.result_kind` or `Tool.result_unit` and does not make
the tool reviewed.

The names remain `<toolkit_id>.<upstream_name>` when a provider version
changes. The version is carried in the `Toolkit.version` and `Tool.version`
fields so an operator can pin or compare versions without silently changing
agent-facing names.

## Inspect before importing

Use `inspect_mcp` to discover a server without mutating a registry. It returns
a safe manifest containing upstream names, per-tool schema hashes, a sorted
aggregate SHA-256 `schema_digest`, `annotations_untrusted: true`, and the
review policy. It does not include upstream schemas, URLs, descriptions, or
credentials. `inspect_mcp_manifest` builds the same manifest from captured
tool records, and `mcp_schema_digest` computes the digest directly.

```python
from energy_agent_tools.connectors.mcp_bridge import inspect_mcp, import_mcp

manifest = await inspect_mcp(
    "power_server",
    command="python",
    args=["/opt/power/server.py"],
    version="2026.10",
)

await import_mcp(
    registry,
    "power_server",
    command="python",
    args=["/opt/power/server.py"],
    version="2026.10",
    expected_schema_digest=manifest["schema_digest"],
    tool_metadata={
        "read_meter": {
            "reviewed": True,
            "action": "read-only",
            "kind": "metered",
            "unit": "kWh",
        }
    },
)
```

The digest covers canonical JSON records of sorted upstream names and their
`inputSchema` values. Descriptions and MCP annotations are excluded because
they are presentation text and untrusted hints. If a server's schemas differ
from `expected_schema_digest`, import raises `mcp_schema_drift` before adding a
toolkit or any tools to the registry. This makes approval records useful in
deployment pipelines and keeps a failed upgrade atomic.

The bridge also fingerprints the selected tool again after every fresh MCP
session is initialized. If that tool disappeared, was duplicated, or its
`inputSchema` changed since import, execution raises `mcp_schema_drift` before
calling the upstream tool. Import-time approval therefore cannot be bypassed
by a server that changes its schema after registration.

## Credentials and manifests

Credentials are supplied at discovery or execution time through an operator
environment reference and a scoped `ConnectedAccount`; they are never tool
arguments. Static headers, sensitive environment values, dynamic discovery
credentials, result content, descriptions, and input schemas pass through the
bridge redactor. Inspection manifests contain hashes and review metadata only,
so they can be persisted for drift review without copying provider payloads.
Remote URLs are not placed in provenance. OAuth login and refresh remain an
operator responsibility until the connection lifecycle provides a token
store.

## Re-review triggers

Re-run inspection and review the resulting manifest when a provider changes
its tool list or schemas, when the declared version changes, or when the
operator changes the result kind, unit, action, or account binding. Keep the
manifest and the approved digest with the deployment configuration. A digest
approval is a schema compatibility check; it is not a claim that the upstream
implementation or its measurements are trustworthy.
