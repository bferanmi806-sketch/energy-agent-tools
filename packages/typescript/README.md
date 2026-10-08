# Energy Agent Tools TypeScript SDK

Use this 0.5.0 SDK with the matching 0.5.0 Python gateway and web source.

This package connects TypeScript applications to the self-hosted Python gateway.
The gateway discovers providers, checks account/site scope, executes tools and
preserves energy semantics. Provider credentials remain on the gateway.

The SDK is distributed as matching source and a release tarball; it has not
been published to the npm registry. Build it locally when using the repository.
Use the matching 0.5.0 gateway. The REST `runSkill` and `toolkits` routes were added after v0.3.0; released v0.3.0
gateways can run workflows through the existing MCP `ENERGY_RUN_SKILL` helper.

```sh
npm ci
npm run build
npm test
```

Run those commands in `packages/typescript`. The acceptance tests also need the
repository's Python environment at `.venv/bin/python`, or an installed runtime
selected through `ENERGY_AGENT_TEST_PYTHON`. They launch an authenticated local
production host with generated tokens and synthetic data.

## Use the REST gateway

```typescript
import { EnergyAgentTools } from '@energy-agent-tools/sdk';

const token = process.env.ENERGY_GATEWAY_TOKEN;
if (!token) throw new Error('Configure your gateway API token.');

const energy = new EnergyAgentTools({
  baseUrl: 'http://127.0.0.1:8765',
  token,
});
const session = await energy.createSession({ site_id: 'home' });
const discovery = await session.search({ query: 'electricity consumption' });
const response = await session.capability({
  capability: 'get_energy_consumption',
  arguments: {
    start: '2026-10-01T00:00:00Z',
    end: '2026-10-02T00:00:00Z',
  },
});
if (response.ok) {
  const kind = response.result.kind;
  // Provider-specific data is unknown until parsed against its own schema.
}
```

HTTP transport failures raise SDK errors. A gateway execution refusal returns
`ok: false` with an error code, preserving the gateway's resolution evidence.
A successful result retains `kind`, units, timestamps, provenance, assumptions
and warnings. Types come from generated schemas; malformed wire payloads fail
validation rather than silently becoming typed results.

Use `session.resolve`, `execute`, `toolkits`, `connections`, `artifacts`, `skills`, `runSkill`, `job`
and `deleteArtifact` for the other REST operations. `energy.session` attaches
to an existing session ID; the host still checks ownership and expiration.
Calling `session.close()` deletes that session's artifacts on the server.

## Read execution history

`energy.activity({ limit: 50 })` reads durable tool execution metadata for the
current actor, workspace, allowed sites and connection grants. Pass the returned
`next_before` as `before` to read older entries. History survives session closure
and gateway restarts. It contains no tool arguments, results or raw error
messages. See the [activity guide](../../docs/execution-activity.md) for retention,
recording health and access boundaries. This route requires matching current
gateway source.

## Manage a workspace

For an explicitly [managed workspace](../../docs/managed-workspaces.md), use
`energy.workspace()` with its management key. This client works before the
workspace has any sites; it does not create an execution session. `workspace.skills()`
returns workflow questions, sequences and optional execution/parameter/evidence
metadata without needing a site. This describes supported recipes; it does not
prove the necessary providers are connected. Agent clients browse and execute
with the scoped `session.skills()` and `session.runSkill()` interfaces.

Use `details`, `toolkits`, `connectionSetups`, `connections`, `sites`, `assets`
and `keys` to read current workspace records. `connectAccount` verifies an
Octopus meter and saves a disabled `pending_mapping` connection. Create a site
with `createSite`, then call `mapConnection(connectionId, { site_id })` to verify
and activate it. `createAsset` can associate active connections at that site.

`createAgentKey({ name, site_ids })` returns the raw scoped key once. Save it
before discarding the response. `keys()` omits raw tokens. Use `revokeKey` to
withdraw access, and `verifyConnection` or `disconnectConnection` for connection
lifecycle actions. The host derives workspace ownership from the current key
on every request. Agent keys cannot use management operations.

Use `members`, `addMember({ user_id })`, `setMemberGrants(userId, grants)`,
`removeMember(userId)` and `createMemberAgentKey(userId, { name, site_ids })`
to share selected connections with an existing instance user. New membership
has empty grants. Set explicit `site_ids` and `connection_ids` before issuing
a member key. The key belongs to the member; the provider credential stays with
the owner. Member sessions can execute granted accounts but cannot call workspace
management routes. Permission changes invalidate captured sessions, so open a
fresh REST session or reconnect MCP. Removing and readding a member leaves old
keys revoked. See the [sharing guide](../../docs/managed-workspaces.md#share-selected-connections).

Tokens may be provided by a callback so rotation takes effect on each request.
`timeoutMs`, `maxResponseBytes` and an injected `fetch` can be configured on the
client. Individual requests accept an `AbortSignal`. The transport does not
replay requests automatically. Set a longer timeout explicitly for a lengthy
workflow or simulation.


Run a composed energy workflow through the same scoped session:

```typescript
const forecast = await session.runSkill({
  skill_id: 'forecast-bill',
  parameters: { context_mode: 'auto' },
});
```

The workflow resolves history, optional weather context and tariffs behind the
gateway. The source history must be available and complete. A missing provider
or unknown tariff validity returns a refusal with evidence.

## Use the MCP gateway

```typescript
import { EnergyMcpClient } from '@energy-agent-tools/sdk';

const gateway = await EnergyMcpClient.connect({
  url: 'http://127.0.0.1:8765/mcp/home',
  token,
});
try {
  const tools = await gateway.listTools();
  const discovered = await gateway.callHelper('ENERGY_SEARCH_TOOLS', {
    query: 'forecast electricity consumption',
  });
  const forecast = await gateway.callHelper('ENERGY_RUN_SKILL', {
    skill_id: 'forecast-bill',
    parameters: { context_mode: 'auto' },
  });
} finally {
  await gateway.close();
}
```

The adapter uses the official MCP client and Streamable HTTP transport. Helper
responses are validated JSON objects. Provider-specific fields still need their
own schema before application code treats them as typed domain data. Cancellation
uses an `AbortSignal`; closing terminates the MCP session and its transport.

## Contract generation

From the repository root:

```sh
uv run python scripts/export_typescript_contracts.py
uv run python scripts/export_typescript_contracts.py --check
uv run python scripts/verify_typescript_sdk.py
```

Request and result schemas come from the production Python models. Response
envelopes follow the host routes and are checked by real-host SDK acceptance.
`src/contracts.ts` is generated; change the Python source rather than editing
it directly. The SDK uses [json-schema-to-ts](https://github.com/ThomasAribart/json-schema-to-ts)
for schema-derived types and [Ajv](https://ajv.js.org/guide/typescript.html) for
runtime validation.

The matching web application and persistent workspace/connection control plane
are included in 0.5.0. This SDK does not qualify physical
meters, independent agent benchmarks or sustained deployment reliability.

Current development source also exposes `energy.identity()` over authenticated
`GET /me`. It returns only the token's allowed sites and their assets, so web
clients can choose a site without asking users to know its identifier. This
route requires the current gateway and SDK source; it is absent from v0.3.0
and from the earlier locally qualified 0.1.0 tarball.

## Durable numerical jobs

`energy.jobHistory({ limit: 50, status: "completed" })` discovers current-actor
job metadata across sessions. Pass the returned `next_before` as `before` to
read older work. `energy.jobAction(jobId, { operation: "result" })` recovers a
completed result under current workspace and site access. `status`, `cancel`
and `delete` use the same interface. Discovery excludes input arguments,
result data, raw messages and private paths. See the
[persistent job guide](../../docs/jobs-product.md) for lifecycle and limits.
