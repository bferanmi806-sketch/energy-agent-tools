import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:net';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { EnergyAgentTools, EnergyHttpError } from '../dist/index.js';

const root = fileURLToPath(new URL('../../..', import.meta.url));
const python = process.env.ENERGY_AGENT_TEST_PYTHON ?? join(root, '.venv/bin/python');
const authorizationCode = 'fixture-authorization-code-private';
const accessToken = 'fixture-access-token-private';
const refreshedAccessToken = 'fixture-refreshed-access-token-private';
const refreshToken = 'fixture-refresh-token-private';

async function startHost() {
  const allocation = createServer();
  allocation.listen(0, '127.0.0.1');
  await once(allocation, 'listening');
  const address = allocation.address();
  assert.ok(address && typeof address !== 'string');
  const port = address.port;
  await new Promise(resolve => allocation.close(resolve));

  const stateDirectory = await mkdtemp(join(tmpdir(), 'energy-sdk-managed-oauth-'));
  const tokenFile = join(stateDirectory, 'one-time-management-token');
  const otherTokenFile = join(stateDirectory, 'other-management-token');
  const child = spawn(python, [
    join(root, 'packages/typescript/test/managed_host.py'),
    '--port', String(port),
    '--state-dir', stateDirectory,
    '--token-file', tokenFile,
    '--other-token-file', otherTokenFile,
  ], {
    cwd: root,
    env: {
      ...process.env,
      PYTHONPATH: [join(root, 'src'), process.env.PYTHONPATH].filter(Boolean).join(delimiter),
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let exited = false;
  let launchFailed = false;
  child.once('exit', () => { exited = true; });
  child.once('error', () => { launchFailed = true; exited = true; });
  const baseUrl = `http://127.0.0.1:${port}`;
  let managementToken;
  let otherManagementToken;

  try {
    const deadline = Date.now() + 15000;
    while (!managementToken || !otherManagementToken) {
      if (exited) throw new Error('Managed OAuth acceptance host exited during startup.');
      try {
        managementToken ??= await readFile(tokenFile, 'utf8');
        await unlink(tokenFile);
      } catch { }
      try {
        otherManagementToken ??= await readFile(otherTokenFile, 'utf8');
        await unlink(otherTokenFile);
      } catch { }
      if (!managementToken || !otherManagementToken) {
        if (Date.now() > deadline) throw new Error('Managed OAuth host credential startup timed out.');
        await new Promise(resolve => setTimeout(resolve, 50));
      }
    }

    const headers = { Authorization: `Bearer ${managementToken}` };
    while (true) {
      if (exited) throw new Error('Managed OAuth host exited before becoming ready.');
      try {
        const response = await fetch(`${baseUrl}/me`, {
          headers,
          signal: AbortSignal.timeout(500),
        });
        if (response.ok) break;
      } catch { }
      if (Date.now() > deadline) throw new Error('Managed OAuth host startup timed out.');
      await new Promise(resolve => setTimeout(resolve, 50));
    }

    return {
      baseUrl,
      managementToken,
      otherManagementToken,
      async observations() {
        return JSON.parse(await readFile(join(stateDirectory, 'observations.json'), 'utf8'));
      },
      async close() {
        if (!exited) {
          const closed = once(child, 'exit');
          child.kill('SIGTERM');
          await closed;
        }
        await rm(stateDirectory, { recursive: true, force: true });
      },
    };
  } catch {
    if (!exited) {
      const closed = once(child, 'exit');
      child.kill('SIGTERM');
      await closed;
    }
    await rm(stateDirectory, { recursive: true, force: true });
    throw new Error(launchFailed
      ? 'Managed OAuth acceptance host could not launch its Python fixture.'
      : 'Managed OAuth acceptance host could not start; fixture stderr is suppressed to protect credentials.');
  }
}

function hasStatus(status) {
  return error => error instanceof EnergyHttpError && error.status === status;
}

function hasClientError(error) {
  return error instanceof EnergyHttpError && error.status >= 400 && error.status < 500;
}

function includesNoCredentials(value) {
  const encoded = JSON.stringify(value);
  return [authorizationCode, accessToken, refreshedAccessToken, refreshToken]
    .every(secret => !encoded.includes(secret));
}

test('managed OAuth SDK completes a scoped Home Assistant connection through the production host', async () => {
  const host = await startHost();
  try {
    const management = new EnergyAgentTools({ baseUrl: host.baseUrl, token: host.managementToken });
    const workspace = management.workspace();
    const configurations = await workspace.authConfigurations();

    assert.deepEqual(configurations.configurations, [{
      id: 'home-assistant',
      name: 'Local Home Assistant fixture',
      toolkit: 'home-assistant',
      protocol: 'home_assistant',
      pending_cleanup: 0,
    }]);
    assert.equal(JSON.stringify(configurations).includes('127.0.0.1:18123'), false);
    assert.equal((await management.identity()).sites.length, 0);

    const started = await workspace.beginAuthorization({
      configuration_id: 'home-assistant',
      entity_id: 'sensor.power',
      mapping: {
        telemetry_role: 'current_power',
        unit: 'kW',
        quantity_shape: 'instantaneous',
        measurement_kind: 'metered',
      },
    });
    const authorization = started.authorization;
    assert.ok(authorization.connection_id);
    assert.ok(authorization.expires_at);
    const authorizationUrl = new URL(authorization.authorization_url);
    assert.equal(authorizationUrl.origin, 'http://127.0.0.1:18123');
    assert.equal(authorizationUrl.pathname, '/auth/authorize');
    assert.equal(authorizationUrl.searchParams.get('client_id'), 'http://127.0.0.1:18123/');
    assert.equal(authorizationUrl.searchParams.get('redirect_uri'), 'http://127.0.0.1:18123/oauth/callback');
    assert.equal(authorizationUrl.searchParams.get('state'), authorization.state);
    assert.equal(authorizationUrl.searchParams.has('code_challenge'), false);
    assert.deepEqual((await workspace.connections()).connections, []);
    assert.equal((await host.observations()).auth_code, 0);

    const site = await workspace.createSite({ name: 'SDK Home', timezone: 'Europe/London' });
    const issued = await workspace.createAgentKey({ name: 'SDK site agent', site_ids: [site.site.id] });
    const otherWorkspace = new EnergyAgentTools({
      baseUrl: host.baseUrl,
      token: host.otherManagementToken,
    }).workspace();
    const completionInput = {
      configuration_id: 'home-assistant',
      state: authorization.state,
      code: authorizationCode,
    };

    await assert.rejects(otherWorkspace.completeAuthorization(completionInput), hasClientError);
    assert.equal((await host.observations()).auth_code, 0);

    const agent = new EnergyAgentTools({ baseUrl: host.baseUrl, token: issued.token });
    await assert.rejects(agent.workspace().completeAuthorization(completionInput), hasStatus(403));
    assert.equal((await host.observations()).auth_code, 0);

    const pending = await workspace.completeAuthorization(completionInput);
    assert.equal(pending.account.id, authorization.connection_id);
    assert.equal(pending.account.state, 'pending_mapping');
    assert.equal(pending.account.site_id, null);
    assert.equal(pending.account.enabled, false);
    assert.equal(pending.account.verified, true);
    assert.equal(JSON.stringify(pending).includes(authorizationCode), false);
    assert.ok(includesNoCredentials(pending));
    assert.equal((await workspace.connections()).connections[0]?.id, authorization.connection_id);
    assert.ok(includesNoCredentials(await workspace.connections()));
    assert.deepEqual(await host.observations(), {
      auth_code: 1,
      refresh: 0,
      state_reads: 1,
      revocations: 0,
    });

    const agentSession = await agent.createSession({ site_id: site.site.id });
    const stateCall = {
      tool: 'home_assistant.get_state',
      arguments: { entity_id: 'sensor.power' },
      account_id: authorization.connection_id,
    };
    const pendingExecution = await agentSession.execute(stateCall);
    assert.equal(pendingExecution.ok, false);
    assert.equal(pendingExecution.error.code, 'account_forbidden');
    assert.equal((await host.observations()).state_reads, 1);

    const mapped = await workspace.mapConnection(authorization.connection_id, { site_id: site.site.id });
    assert.equal(mapped.account.state, 'active');
    assert.equal(mapped.account.site_id, site.site.id);
    assert.equal(mapped.account.enabled, true);
    assert.ok(includesNoCredentials(mapped));

    const power = await agentSession.capability({
      capability: 'get_current_power',
      account_id: authorization.connection_id,
    });
    assert.equal(power.ok, true);
    assert.equal(power.result.data.state, '1.75');
    assert.equal(power.result.unit, 'kW');
    assert.ok(includesNoCredentials(power));
    assert.deepEqual(await host.observations(), {
      auth_code: 1,
      refresh: 1,
      state_reads: 3,
      revocations: 0,
    });

    const disconnected = await workspace.disconnectConnection(authorization.connection_id);
    assert.equal(disconnected.ok, true);
    assert.equal(disconnected.account.enabled, false);
    assert.equal(disconnected.account.state, 'revoked');
    assert.equal(disconnected.upstream_revoked, true);
    assert.ok(includesNoCredentials(disconnected));
    assert.equal((await host.observations()).revocations, 1);

    const readsAfterDisconnect = (await host.observations()).state_reads;
    const revokedExecution = await agentSession.execute(stateCall);
    assert.equal(revokedExecution.ok, false);
    assert.equal(revokedExecution.error.code, 'account_forbidden');
    assert.equal((await host.observations()).state_reads, readsAfterDisconnect);
    assert.equal((await workspace.connections()).connections[0]?.state, 'revoked');
    assert.ok(includesNoCredentials(await workspace.connections()));
  } finally {
    await host.close();
  }
});
