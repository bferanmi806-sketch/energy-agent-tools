import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer as createHttpServer } from 'node:http';
import { createServer as createNetServer } from 'node:net';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { EnergyMcpClient, EnergyMcpError } from '../dist/index.js';

const root = fileURLToPath(new URL('../../..', import.meta.url));
const python = process.env.ENERGY_AGENT_TEST_PYTHON ?? join(root, '.venv/bin/python');

async function availablePort() {
  const reservation = createNetServer();
  reservation.listen(0, '127.0.0.1');
  await once(reservation, 'listening');
  const address = reservation.address();
  assert.ok(address && typeof address !== 'string');
  await new Promise((resolve, reject) => reservation.close(error => error ? reject(error) : resolve()));
  return address.port;
}

async function stopChild(child, exited) {
  if (exited()) return;
  const exit = once(child, 'exit');
  child.kill('SIGTERM');
  await exit;
}

async function startHost() {
  const port = await availablePort();
  const state = await mkdtemp(join(tmpdir(), 'energy-mcp-sdk-'));
  const token = randomBytes(32).toString('hex');
  const child = spawn(python, [join(root, 'packages/typescript/test/host.py'), '--port', String(port), '--state-dir', state], {
    cwd: root,
    env: { ...process.env, ENERGY_AGENT_TEST_TOKEN: token },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let exited = false;
  child.once('exit', () => { exited = true; });
  let errorOutput = '';
  child.stderr.on('data', chunk => { errorOutput += String(chunk); });
  const baseUrl = `http://127.0.0.1:${port}`;

  try {
    const deadline = Date.now() + 15000;
    while (true) {
      if (exited) {
        const safeOutput = errorOutput.replaceAll(token, '[redacted]');
        throw new Error(`Acceptance host exited before becoming ready.${safeOutput ? ` ${safeOutput}` : ''}`);
      }
      try {
        const response = await fetch(`${baseUrl}/sessions`, { signal: AbortSignal.timeout(500) });
        await response.body?.cancel();
        break;
      } catch { }
      if (Date.now() > deadline) throw new Error('Acceptance host startup timed out.');
      await new Promise(resolve => setTimeout(resolve, 50));
    }

    return {
      baseUrl,
      token,
      async close() {
        await stopChild(child, () => exited);
        await rm(state, { recursive: true, force: true });
      },
    };
  } catch (error) {
    await stopChild(child, () => exited);
    await rm(state, { recursive: true, force: true });
    throw error;
  }
}

function isDiagnostic(message) {
  return error => error instanceof EnergyMcpError && error.message === message;
}

test('MCP client initializes with the official SDK, lists helpers, searches and executes a scoped calculation', async () => {
  const host = await startHost();
  let client;
  try {
    client = await EnergyMcpClient.connect({ url: `${host.baseUrl}/mcp/sdk-site`, token: () => host.token });
    const tools = await client.listTools();
    const helperNames = new Set(tools.map(tool => tool.name));
    assert.ok(helperNames.has('ENERGY_SEARCH_TOOLS'));
    assert.ok(helperNames.has('ENERGY_EXECUTE_CAPABILITY'));

    await assert.rejects(
      client.callHelper('ENERGY_SEARCH_TOOLS', { query: 'fixture', omittedByJson: undefined }),
      isDiagnostic('MCP helper call failed.'),
    );

    const search = await client.callHelper('ENERGY_SEARCH_TOOLS', {
      query: 'fixture calculate value',
      limit: 5,
    });
    assert.ok(Array.isArray(search.tools));
    assert.ok(search.tools.some(tool => tool.name === 'FIXTURE_CALCULATE'));

    const calculation = await client.callHelper('ENERGY_EXECUTE_CAPABILITY', {
      capability: 'calculate_fixture_value',
      arguments: { base: 17, multiplier: 3 },
    });
    assert.equal(calculation.ok, true);
    assert.deepEqual(calculation.result.data, { value: 51 });
    assert.equal(calculation.result.kind, 'calculated');
  } finally {
    if (client) await client.close();
    await host.close();
  }
});

test('MCP authentication failures and cancellation use fixed diagnostics without exposing tokens', async () => {
  const host = await startHost();
  const invalidToken = randomBytes(32).toString('hex');
  try {
    await assert.rejects(
      EnergyMcpClient.connect({ url: `${host.baseUrl}/mcp/sdk-site`, token: invalidToken }),
      error => error instanceof EnergyMcpError && error.message === 'MCP connection failed; check the gateway URL and credentials.' && !error.message.includes(invalidToken),
    );

    const controller = new AbortController();
    controller.abort();
    await assert.rejects(
      EnergyMcpClient.connect({ url: `${host.baseUrl}/mcp/sdk-site`, token: host.token, signal: controller.signal }),
      isDiagnostic('MCP connection was cancelled.'),
    );

    const client = await EnergyMcpClient.connect({ url: `${host.baseUrl}/mcp/sdk-site`, token: host.token });
    const helperController = new AbortController();
    const pendingCall = client.callHelper(
      'ENERGY_SEARCH_TOOLS',
      { query: 'fixture' },
      { signal: helperController.signal },
    );
    helperController.abort();
    await assert.rejects(
      pendingCall,
      isDiagnostic('MCP helper call was cancelled.'),
    );
    await client.close();
    await assert.rejects(client.listTools(), isDiagnostic('MCP client is closed.'));
    await client.close();
  } finally {
    await host.close();
  }
});

test('MCP transport blocks cross-origin redirects before replaying an authenticated POST', async () => {
  const destination = createHttpServer((_request, response) => {
    response.end('unexpected redirect target');
  });
  destination.listen(0, '127.0.0.1');
  await once(destination, 'listening');
  const destinationAddress = destination.address();
  assert.ok(destinationAddress && typeof destinationAddress !== 'string');

  let initializePosts = 0;
  let authorizationHeader;
  const source = createHttpServer((request, response) => {
    if (request.method === 'GET') {
      response.writeHead(405).end();
      return;
    }
    if (request.method === 'POST') {
      initializePosts += 1;
      authorizationHeader = request.headers.authorization;
      response.writeHead(307, { location: `http://127.0.0.1:${destinationAddress.port}/energy/mcp/sdk-site` }).end();
      return;
    }
    response.writeHead(405).end();
  });
  source.listen(0, '127.0.0.1');
  await once(source, 'listening');
  const sourceAddress = source.address();
  assert.ok(sourceAddress && typeof sourceAddress !== 'string');
  const token = randomBytes(32).toString('hex');

  try {
    await assert.rejects(
      EnergyMcpClient.connect({ url: `http://127.0.0.1:${sourceAddress.port}/energy/mcp/sdk-site`, token }),
      isDiagnostic('MCP connection failed; check the gateway URL and credentials.'),
    );
    assert.equal(initializePosts, 1);
    assert.equal(authorizationHeader, `Bearer ${token}`);
  } finally {
    await Promise.all([
      new Promise(resolve => source.close(resolve)),
      new Promise(resolve => destination.close(resolve)),
    ]);
  }
});
