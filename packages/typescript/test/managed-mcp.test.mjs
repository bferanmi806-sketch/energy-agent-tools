import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { randomBytes } from 'node:crypto';
import {
  EnergyAgentTools,
  EnergyHttpError,
  EnergyProtocolError,
} from '../dist/index.js';

async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  return `http://127.0.0.1:${address.port}`;
}

async function close(server) {
  server.closeAllConnections();
  await new Promise((resolve, reject) => {
    server.close(error => error ? reject(error) : resolve());
  });
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString('utf8');
}

function sendJson(response, status, value) {
  response.writeHead(status, { 'content-type': 'application/json' });
  response.end(JSON.stringify(value));
}

test('managed MCP methods send exact management requests and accept safe responses', async () => {
  const received = [];
  const gateway = createServer(async (request, response) => {
    received.push({
      method: request.method,
      url: request.url,
      authorization: request.headers.authorization,
      body: await readBody(request),
    });

    if (request.url === '/api/workspace/mcp/inspect') {
      sendJson(response, 200, {
        inspection: {
          schema_digest: 'a'.repeat(64),
          annotations_untrusted: true,
          tools: [{ name: 'read_energy', schema_hash: 'b'.repeat(64) }],
        },
      });
      return;
    }
    if (request.url === '/api/workspace/mcp/stage') {
      sendJson(response, 201, {
        ok: true,
        account: {
          id: 'custom-mcp-1234',
          toolkit: 'custom-mcp-1234',
          site_id: null,
          enabled: false,
          auth_scheme: 'bearer',
          display_name: 'Energy reader',
          state: 'pending_mapping',
          verified: true,
        },
        schema_digest: 'a'.repeat(64),
        selected_tool_count: 1,
      });
      return;
    }
    if (request.url === '/api/workspace/mcp/recover') {
      sendJson(response, 200, {
        ok: true,
        connections: [
          { connection_id: 'custom-mcp-1234', status: 'ready' },
          { connection_id: 'custom-mcp-5678', status: 'skipped', error_code: 'connection_inactive' },
        ],
      });
      return;
    }
    sendJson(response, 404, { error: { code: 'not_found', message: 'Not found.' } });
  });

  const providerRequests = [];
  const provider = createServer(async (request, response) => {
    providerRequests.push({ url: request.url, authorization: request.headers.authorization });
    sendJson(response, 500, { error: 'The SDK must not connect to this provider URL.' });
  });

  const gatewayUrl = await listen(gateway);
  const providerUrl = await listen(provider);
  const managementToken = randomBytes(32).toString('hex');
  const providerCredential = `provider-${randomBytes(32).toString('hex')}`;
  const workspace = new EnergyAgentTools({
    baseUrl: `${gatewayUrl}/api`,
    token: managementToken,
  }).workspace();

  try {
    const inspectionRequest = {
      url: `${providerUrl}/mcp`,
      auth_scheme: 'bearer',
      auth_header: 'Authorization',
      credential: providerCredential,
    };
    const inspection = await workspace.inspectMcp(inspectionRequest);
    assert.deepEqual(inspection, {
      inspection: {
        schema_digest: 'a'.repeat(64),
        annotations_untrusted: true,
        tools: [{ name: 'read_energy', schema_hash: 'b'.repeat(64) }],
      },
    });
    assert.doesNotMatch(JSON.stringify(inspection), new RegExp(providerCredential));

    const stageRequest = {
      ...inspectionRequest,
      display_name: 'Energy reader',
      schema_digest: 'a'.repeat(64),
      reviews: [{
        name: 'read_energy',
        reviewed: true,
        actions: ['read-only'],
        kind: 'metered',
        unit: 'kWh',
      }],
    };
    const staged = await workspace.stageMcp(stageRequest);
    assert.equal(staged.ok, true);
    assert.equal(staged.account.state, 'pending_mapping');
    assert.equal(staged.selected_tool_count, 1);
    assert.doesNotMatch(JSON.stringify(staged), new RegExp(providerCredential));

    const recovered = await workspace.recoverMcp();
    assert.deepEqual(recovered, {
      ok: true,
      connections: [
        { connection_id: 'custom-mcp-1234', status: 'ready' },
        { connection_id: 'custom-mcp-5678', status: 'skipped', error_code: 'connection_inactive' },
      ],
    });

    assert.deepEqual(received, [
      {
        method: 'POST',
        url: '/api/workspace/mcp/inspect',
        authorization: `Bearer ${managementToken}`,
        body: JSON.stringify(inspectionRequest),
      },
      {
        method: 'POST',
        url: '/api/workspace/mcp/stage',
        authorization: `Bearer ${managementToken}`,
        body: JSON.stringify(stageRequest),
      },
      {
        method: 'POST',
        url: '/api/workspace/mcp/recover',
        authorization: `Bearer ${managementToken}`,
        body: '{}',
      },
    ]);
    assert.deepEqual(providerRequests, []);
  } finally {
    await Promise.all([close(gateway), close(provider)]);
  }
});

test('invalid managed MCP requests fail before token resolution or network IO', () => {
  let tokenCalls = 0;
  let fetchCalls = 0;
  const workspace = new EnergyAgentTools({
    baseUrl: 'http://127.0.0.1:1/api',
    token: () => { tokenCalls += 1; return 'management-token'; },
    fetch: async () => { fetchCalls += 1; throw new Error('unexpected request'); },
  }).workspace();

  assert.throws(
    () => workspace.inspectMcp({ url: '', credential: 'provider-secret' }),
    EnergyProtocolError,
  );
  assert.throws(
    () => workspace.stageMcp({
      url: 'https://provider.example/mcp',
      display_name: 'Energy reader',
      schema_digest: 'a'.repeat(64),
      reviews: [],
    }),
    EnergyProtocolError,
  );
  assert.equal(tokenCalls, 0);
  assert.equal(fetchCalls, 0);
});

test('managed MCP responses enforce schemas and HTTP errors omit provider diagnostics', async () => {
  const providerCredential = `provider-${randomBytes(32).toString('hex')}`;
  let inspectionResponses = 0;
  const gateway = createServer(async (request, response) => {
    await readBody(request);
    if (request.url === '/api/workspace/mcp/inspect' && inspectionResponses++ === 0) {
      sendJson(response, 200, {
        inspection: {
          schema_digest: 'not-a-digest',
          annotations_untrusted: true,
          tools: [],
        },
      });
      return;
    }
    sendJson(response, 502, {
      error: {
        code: 'mcp_discovery_failed',
        message: 'MCP server discovery failed.',
        provider_diagnostic: providerCredential,
      },
    });
  });
  const gatewayUrl = await listen(gateway);
  const workspace = new EnergyAgentTools({
    baseUrl: `${gatewayUrl}/api`,
    token: 'management-token',
  }).workspace();

  try {
    await assert.rejects(
      workspace.inspectMcp({ url: 'https://provider.example/mcp' }),
      error => error instanceof EnergyProtocolError && !String(error).includes(providerCredential),
    );

    await assert.rejects(
      workspace.inspectMcp({
        url: 'https://provider.example/mcp',
        auth_scheme: 'bearer',
        credential: providerCredential,
      }),
      error => {
        assert.ok(error instanceof EnergyHttpError);
        assert.equal(error.status, 502);
        assert.equal(error.code, 'mcp_discovery_failed');
        assert.equal(error.message, 'MCP server discovery failed.');
        assert.doesNotMatch(String(error), new RegExp(providerCredential));
        assert.doesNotMatch(JSON.stringify(error), new RegExp(providerCredential));
        return true;
      },
    );
  } finally {
    await close(gateway);
  }
});
