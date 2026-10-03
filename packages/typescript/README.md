# Energy Agent Tools TypeScript SDK

This package connects TypeScript applications to the self-hosted Python gateway.
The gateway discovers providers, checks account/site scope, executes tools and
preserves energy semantics. Provider credentials remain on the gateway.

The SDK is under development. Build it locally before installing it in an app;
it has not been published to the npm registry. It targets the current `main`
gateway. The REST `runSkill` and `toolkits` routes were added after v0.3.0; released v0.3.0
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

The Connect Apps web application and persistent workspace/connection control
plane remain part of the project plan. This SDK does not qualify physical
meters, independent agent benchmarks or sustained deployment reliability.

Current development source also exposes `energy.identity()` over authenticated
`GET /me`. It returns only the token's allowed sites and their assets, so web
clients can choose a site without asking users to know its identifier. This
route requires the current gateway and SDK source; it is absent from v0.3.0
and from the earlier locally qualified 0.1.0 tarball.
