import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:net';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { EnergyAgentTools, EnergyHttpError } from '../dist/index.js';

const root = fileURLToPath(new URL('../../..', import.meta.url));
const python = process.env.ENERGY_AGENT_TEST_PYTHON ?? join(root, '.venv/bin/python');

async function startHost() {
  const allocation = createServer();
  allocation.listen(0, '127.0.0.1');
  await once(allocation, 'listening');
  const address = allocation.address();
  assert.ok(address && typeof address !== 'string');
  const port = address.port;
  await new Promise(resolve => allocation.close(resolve));

  const state = await mkdtemp(join(tmpdir(), 'energy-sdk-managed-'));
  const tokenFile = join(state, 'one-time-management-token');
  const child = spawn(python, [
    join(root, 'packages/typescript/test/managed_host.py'),
    '--port', String(port),
    '--state-dir', state,
    '--token-file', tokenFile,
  ], {
    cwd: root,
    env: { ...process.env },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let exited = false;
  child.once('exit', () => { exited = true; });
  let launchFailed = false;
  child.once('error', () => { launchFailed = true; exited = true; });
  const baseUrl = `http://127.0.0.1:${port}`;
  let managementToken;

  try {
    const deadline = Date.now() + 15000;
    while (!managementToken) {
      if (exited) throw new Error('Managed acceptance host exited during startup.');
      try {
        managementToken = await readFile(tokenFile, 'utf8');
        await unlink(tokenFile);
      } catch { }
      if (!managementToken) {
        if (Date.now() > deadline) throw new Error('Managed host credential startup timed out.');
        await new Promise(resolve => setTimeout(resolve, 50));
      }
    }

    const authorization = { Authorization: `Bearer ${managementToken}` };
    while (true) {
      if (exited) throw new Error('Managed acceptance host exited before becoming ready.');
      try {
        const response = await fetch(`${baseUrl}/me`, {
          headers: authorization,
          signal: AbortSignal.timeout(500),
        });
        if (response.ok) break;
      } catch { }
      if (Date.now() > deadline) throw new Error('Managed acceptance host startup timed out.');
      await new Promise(resolve => setTimeout(resolve, 50));
    }

    return {
      baseUrl,
      managementToken,
      octopusCredential: `fixture-${randomBytes(32).toString('hex')}`,
      async close() {
        if (!exited) {
          const closed = once(child, 'exit');
          child.kill('SIGTERM');
          await closed;
        }
        await rm(state, { recursive: true, force: true });
      },
    };
  } catch {
    if (!exited) {
      const closed = once(child, 'exit');
      child.kill('SIGTERM');
      await closed;
    }
    await rm(state, { recursive: true, force: true });
    throw new Error(launchFailed
      ? 'Managed acceptance host could not launch its Python fixture.'
      : 'Managed acceptance host could not start; fixture stderr is suppressed to protect credentials.');
  }
}

function hasStatus(status) {
  return error => error instanceof EnergyHttpError && error.status === status;
}

test('managed workspace SDK browses, maps, executes, scopes, and revokes through the production host', async () => {
  const host = await startHost();
  try {
    const management = new EnergyAgentTools({ baseUrl: host.baseUrl, token: host.managementToken });
    const workspace = management.workspace();

    const identity = await management.identity();
    assert.equal(identity.can_manage_workspace, true);
    assert.deepEqual(identity.sites, []);
    assert.equal((await workspace.details()).workspace.mode, 'managed');
    assert.deepEqual((await workspace.sites()).sites, []);
    assert.deepEqual((await workspace.assets()).assets, []);
    assert.deepEqual((await workspace.connections()).connections, []);
    assert.ok((await workspace.toolkits()).toolkits.some(item => item.id === 'octopus-energy-account'));
    assert.ok((await workspace.connectionSetups()).setups.some(item => item.provider === 'octopus'));

    await assert.rejects(management.createSession(), hasStatus(400));

    const staged = await workspace.connectAccount({
      provider: 'octopus',
      credential: host.octopusCredential,
      mpan: '1234567890123',
      serial_number: 'TEST123',
    });
    assert.equal(staged.account.state, 'pending_mapping');
    assert.equal(staged.account.site_id, null);
    assert.equal(staged.account.enabled, false);
    assert.equal(staged.account.verified, true);
    assert.equal(JSON.stringify(staged).includes(host.octopusCredential), false);
    assert.equal((await workspace.connections()).connections[0]?.id, staged.account.id);

    const site = await workspace.createSite({ name: 'SDK Home', timezone: 'Europe/London' });
    assert.equal((await workspace.sites()).sites[0]?.id, site.site.id);
    const mapped = await workspace.mapConnection(staged.account.id, { site_id: site.site.id });
    assert.equal(mapped.account.state, 'active');
    assert.equal(mapped.account.site_id, site.site.id);
    assert.equal((await workspace.verifyConnection(staged.account.id)).health.status, 'healthy');

    const asset = await workspace.createAsset({
      site_id: site.site.id,
      name: 'SDK meter',
      kind: 'meter',
      account_ids: [staged.account.id],
    });
    assert.equal((await workspace.assets()).assets[0]?.id, asset.asset.id);

    const issued = await workspace.createAgentKey({ name: 'SDK site agent', site_ids: [site.site.id] });
    assert.equal(issued.key.access.kind, 'agent');
    assert.deepEqual(issued.key.access.site_ids, [site.site.id]);
    assert.ok((await workspace.keys()).keys.some(key => key.id === issued.key.id));

    const scoped = new EnergyAgentTools({ baseUrl: host.baseUrl, token: issued.token });
    const scopedIdentity = await scoped.identity();
    assert.deepEqual(scopedIdentity.sites.map(item => item.id), [site.site.id]);
    await assert.rejects(scoped.workspace().details(), hasStatus(403));

    const session = await scoped.createSession({ site_id: site.site.id });
    const result = await session.capability({ capability: 'get_energy_consumption' });
    assert.equal(result.ok, true);
    assert.equal(result.result.data[0]?.value, 1.25);

    assert.equal((await workspace.revokeKey(issued.key.id)).revoked, true);
    await assert.rejects(session.capability({ capability: 'get_energy_consumption' }), hasStatus(401));

    const disconnected = await workspace.disconnectConnection(staged.account.id);
    assert.equal(disconnected.ok, true);
    assert.equal(disconnected.account.enabled, false);
  } finally {
    await host.close();
  }
});
