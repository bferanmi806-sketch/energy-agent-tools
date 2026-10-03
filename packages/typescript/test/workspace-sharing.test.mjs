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

async function startHost() {
  const allocation = createServer();
  allocation.listen(0, '127.0.0.1');
  await once(allocation, 'listening');
  const address = allocation.address();
  assert.ok(address && typeof address !== 'string');
  const port = address.port;
  await new Promise(resolve => allocation.close(resolve));

  const stateDirectory = await mkdtemp(join(tmpdir(), 'energy-sdk-workspace-sharing-'));
  const tokenFile = join(stateDirectory, 'one-time-management-token');
  const child = spawn(python, [
    join(root, 'packages/typescript/test/managed_host.py'),
    '--port', String(port),
    '--state-dir', stateDirectory,
    '--token-file', tokenFile,
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

  try {
    const deadline = Date.now() + 15000;
    while (!managementToken) {
      if (exited) throw new Error('Workspace sharing acceptance host exited during startup.');
      try {
        managementToken = await readFile(tokenFile, 'utf8');
        await unlink(tokenFile);
      } catch { }
      if (!managementToken) {
        if (Date.now() > deadline) throw new Error('Workspace sharing host credential startup timed out.');
        await new Promise(resolve => setTimeout(resolve, 50));
      }
    }

    const headers = { Authorization: `Bearer ${managementToken}` };
    while (true) {
      if (exited) throw new Error('Workspace sharing host exited before becoming ready.');
      try {
        const response = await fetch(`${baseUrl}/me`, {
          headers,
          signal: AbortSignal.timeout(500),
        });
        if (response.ok) break;
      } catch { }
      if (Date.now() > deadline) throw new Error('Workspace sharing host startup timed out.');
      await new Promise(resolve => setTimeout(resolve, 50));
    }

    return {
      baseUrl,
      managementToken,
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
      ? 'Workspace sharing acceptance host could not launch its Python fixture.'
      : 'Workspace sharing host could not start; fixture stderr is suppressed to protect credentials.');
  }
}

function hasStatus(status) {
  return error => error instanceof EnergyHttpError && error.status === status;
}

test('workspace members receive scoped actor keys that follow current grants', async () => {
  const host = await startHost();
  try {
    const owner = new EnergyAgentTools({ baseUrl: host.baseUrl, token: host.managementToken });
    const workspace = owner.workspace();
    const site = await workspace.createSite({ name: 'Shared SDK Home', timezone: 'Europe/London' });
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
    const pending = await workspace.completeAuthorization({
      configuration_id: 'home-assistant',
      state: started.authorization.state,
      code: authorizationCode,
    });
    const connectionId = pending.account.id;
    const mapped = await workspace.mapConnection(connectionId, { site_id: site.site.id });
    assert.equal(mapped.account.state, 'active');
    assert.equal(mapped.account.enabled, true);
    assert.ok(!JSON.stringify(mapped).includes(authorizationCode));

    assert.deepEqual((await workspace.members()).members, []);
    const added = await workspace.addMember({ user_id: 'sdk-member' });
    assert.equal(added.member.user_id, 'sdk-member');
    assert.equal(added.member.name, 'SDK member');
    assert.deepEqual(added.member.grants, { site_ids: [], connection_ids: [] });
    assert.deepEqual((await workspace.members()).members, [added.member]);

    const granted = await workspace.setMemberGrants('sdk-member', {
      site_ids: [site.site.id],
      connection_ids: [connectionId],
    });
    assert.deepEqual(granted.member.grants.site_ids, [site.site.id]);
    assert.deepEqual(granted.member.grants.connection_ids, [connectionId]);

    const issued = await workspace.createMemberAgentKey('sdk-member', {
      name: 'SDK shared sensor agent',
      site_ids: [site.site.id],
    });
    assert.equal(issued.key.user_id, 'sdk-member');
    assert.equal(issued.key.access.kind, 'agent');
    assert.deepEqual(issued.key.access.site_ids, [site.site.id]);
    assert.notEqual(issued.token, host.managementToken);
    assert.equal(issued.key.token_prefix, issued.token.slice(0, 12));
    assert.equal(JSON.stringify(issued).split(issued.token).length - 1, 1);

    const listedMembers = await workspace.members();
    const listedKeys = await workspace.keys();
    for (const response of [added, granted, listedMembers, listedKeys]) {
      assert.equal(JSON.stringify(response).includes(issued.token), false);
    }
    assert.equal(listedKeys.keys.find(key => key.id === issued.key.id)?.user_id, 'sdk-member');

    const member = new EnergyAgentTools({ baseUrl: host.baseUrl, token: issued.token });
    const identity = await member.identity();
    assert.equal(identity.user_id, 'sdk-member');
    assert.equal(identity.can_manage_workspace, false);
    assert.deepEqual(identity.sites.map(item => item.id), [site.site.id]);
    assert.equal(JSON.stringify(identity).includes(issued.token), false);
    await assert.rejects(member.workspace().members(), hasStatus(403));

    const memberSession = await member.createSession({ site_id: site.site.id });
    const visibleConnections = await memberSession.connections();
    assert.deepEqual(visibleConnections.connections.map(item => item.id), [connectionId]);
    assert.equal(JSON.stringify(visibleConnections).includes(issued.token), false);

    const stateCall = {
      tool: 'home_assistant.get_state',
      arguments: { entity_id: 'sensor.power' },
      account_id: connectionId,
    };
    const execution = await memberSession.execute(stateCall);
    assert.equal(execution.ok, true);
    assert.equal(execution.result.data.state, '1.75');
    assert.equal(JSON.stringify(execution).includes(issued.token), false);

    const observationsBeforeGrantRemoval = await host.observations();
    const revokedGrant = await workspace.setMemberGrants('sdk-member', {
      site_ids: [site.site.id],
      connection_ids: [],
    });
    assert.deepEqual(revokedGrant.member.grants.connection_ids, []);
    await assert.rejects(memberSession.execute(stateCall), hasStatus(403));
    assert.deepEqual(await host.observations(), observationsBeforeGrantRemoval);

    const freshSession = await member.createSession({ site_id: site.site.id });
    assert.deepEqual((await freshSession.connections()).connections, []);
    const deniedExecution = await freshSession.execute(stateCall);
    assert.equal(deniedExecution.ok, false);
    assert.equal(deniedExecution.error.code, 'account_forbidden');
    assert.deepEqual(await host.observations(), observationsBeforeGrantRemoval);

    assert.deepEqual(await workspace.removeMember('sdk-member'), { removed: true });
    const readded = await workspace.addMember({ user_id: 'sdk-member' });
    assert.deepEqual(readded.member.grants, { site_ids: [], connection_ids: [] });
    await assert.rejects(member.identity(), hasStatus(401));
  } finally {
    await host.close();
  }
});
