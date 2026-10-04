# MCP review and schema policy

Energy Agent Tools treats an MCP server as an untrusted, versioned provider.
The bridge uses the official MCP client for stdio and streamable HTTP
connections, but an upstream server does not receive permission to read,
calculate, write, or control an energy system merely because it advertises an
annotation such as `readOnlyHint`.

Default imports remain operator resources. Authenticated REST and hosted MCP
sessions hide and deny them, including imports with static credentials or no
credential declaration. Direct local Python/CLI use retains its review policies.

A trusted integration can now bind explicitly reviewed HTTP tools to one managed
connection with `ToolAccountScope`. Schema discovery, capability resolution and
execution require its current workspace, resource owner, site and connection
grant. An unrelated account preference cannot retarget that tool. Stored disable,
revoke and removal state refreshes before schema or catalogue publication.
Ownership fields stay out of public schemas and the TypeScript contract.

This is a backend foundation. The managed Add Custom MCP onboarding API, persisted
review definitions, recovery, health and web review journey remain open.
`reviewed: true` alone does not make a shared transport tenant-safe.

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

## Connection-owned HTTP imports

The account profile requires all of the following:

- `account_scope=ToolAccountScope(workspace_id=..., user_id=..., account_id=...)`.
  The user ID is the resource owner; a member still acts under their own identity.
- An `approved_target` returned by `approve_mcp_target` and its exact canonical URL.
- An approved aggregate `expected_schema_digest` and a nonempty `frozenset` of
  selected raw upstream names, bounded to 100 tools.
- Complete reviewed action, kind and unit metadata for every selected tool.
  Units must be nonblank strings of at most 128 characters.

Only the selected tools register. Review, schema or name failures leave the
registry unchanged. The connection is required even when the server needs no
credential. Stdio, process environment, static headers and credential environment
references are rejected for this profile. Credential headers cannot replace
Host, framing, proxy or connection-control headers.

```python
from energy_agent_tools.connectors.mcp_network import approve_mcp_target
from energy_agent_tools.models import AuthConfig, ToolAccountScope

target = await approve_mcp_target("https://power.example/mcp")
# This assumes an existing, verified and mapped managed connection in AuthStore.
credential = vault.credential(owner_id, connection_id, site_id)
manifest = await inspect_mcp(
    namespace,
    remote_url=target.url,
    approved_target=target,
    discovery_auth=AuthConfig(scheme="bearer"),
    discovery_credential=credential,
)
await import_mcp(
    registry,
    namespace,
    remote_url=target.url,
    approved_target=target,
    discovery_auth=AuthConfig(scheme="bearer"),
    discovery_credential=credential,
    account_scope=ToolAccountScope(
        workspace_id=workspace_id,
        user_id=owner_id,
        account_id=connection_id,
    ),
    selected_tools=frozenset({"read_meter"}),
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

The approved transport validates HTTPS and every DNS answer, then pins actual
socket connections to those addresses while preserving the hostname for TLS.
Mixed public/private DNS answers, user information, query strings, fragments,
scoped IPv6 and special-purpose translation addresses are rejected. DNS has a
five-second deadline and a 16-answer bound. Requests cannot change origin, Host
or TLS hostname. Environment proxies and automatic redirects are disabled.

Private targets require explicit trusted operator approval through
`allow_private=True`; that parameter must never be accepted as a hosted user
permission. Approval objects are process-local and cannot be restored from JSON.
A durable lifecycle must resolve and approve the target again after restart and
bind private endpoint permission to the current workspace. That lifecycle and
bounded provider-payload handling remain follow-up work before arbitrary hosted
URLs can be onboarded.

Discovery credentials are ephemeral. Registered handlers retain the approved
transport and reviewed metadata, not the discovery token. Each fresh MCP session
uses the current authorized execution context credential and rechecks the selected
tool schema before calling it.

## Credentials and manifests

Credentials are supplied at discovery or execution time through an operator
environment reference and a scoped `ConnectedAccount`; they are never tool
arguments. Static headers, sensitive environment values, dynamic discovery
credentials, result content, descriptions, and input schemas pass through the
bridge redactor. Inspection manifests contain hashes and review metadata only,
so they can be persisted for drift review without copying provider payloads.
Remote URLs are not placed in provenance. Trusted managed bindings use the
existing encrypted credential store and runtime authorization. Generic custom
MCP OAuth discovery and onboarding remain open; local imports retain their
operator-owned credential setup.

## Re-review triggers

Re-run inspection and review the resulting manifest when a provider changes
its tool list or schemas, when the declared version changes, or when the
operator changes the result kind, unit, action, or account binding. Keep the
manifest and the approved digest with the deployment configuration. A digest
approval is a schema compatibility check; it is not a claim that the upstream
implementation or its measurements are trustworthy.
