import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:net';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { EnergyAgentTools, EnergyHttpError, EnergyProtocolError } from '../dist/index.js';

const root = fileURLToPath(new URL('../../..', import.meta.url));
const python = process.env.ENERGY_AGENT_TEST_PYTHON ?? join(root, '.venv/bin/python');

async function startHost() {
  const allocation = createServer(); allocation.listen(0, '127.0.0.1'); await once(allocation, 'listening');
  const address = allocation.address(); assert.ok(address && typeof address !== 'string');
  const port = address.port; await new Promise(resolve => allocation.close(resolve));
  const state = await mkdtemp(join(tmpdir(), 'energy-sdk-'));
  const token = randomBytes(32).toString('hex'); const foreignToken = randomBytes(32).toString('hex');
  const child = spawn(python, [join(root, 'packages/typescript/test/host.py'), '--port', String(port), '--state-dir', state], {
    cwd: root, env: { ...process.env, ENERGY_AGENT_TEST_TOKEN: token, ENERGY_AGENT_TEST_FOREIGN_TOKEN: foreignToken }, stdio: ['ignore', 'ignore', 'pipe'],
  });
  let exited = false; child.once('exit', () => { exited = true; });
  let errorOutput = ''; child.stderr.on('data', chunk => { errorOutput += String(chunk); });
  const baseUrl = `http://127.0.0.1:${port}`;
  try {
    const deadline = Date.now() + 15000;
    while (true) {
      if (exited) throw new Error(`Acceptance host exited: ${errorOutput.replaceAll(token, '[redacted]').replaceAll(foreignToken, '[redacted]')}`);
      try { await fetch(`${baseUrl}/sessions`, { signal: AbortSignal.timeout(500) }); break; } catch { }
      if (Date.now() > deadline) throw new Error('Acceptance host startup timed out.');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return { baseUrl, token, foreignToken, async close() {
      if (!exited) { const closed = once(child, 'exit'); child.kill('SIGTERM'); await closed; }
      await rm(state, { recursive: true, force: true });
    } };
  } catch (error) {
    if (!exited) { const closed = once(child, 'exit'); child.kill('SIGTERM'); await closed; }
    await rm(state, { recursive: true, force: true }); throw error;
  }
}

test('SDK uses authenticated production REST routes and preserves scoped calculated results', async () => {
  const host = await startHost();
  try {
    const energy = new EnergyAgentTools({ baseUrl: host.baseUrl, token: () => host.token });
    const identity = await energy.identity();
    assert.equal(identity.user_id, 'sdk-user');
    assert.deepEqual(identity.sites.map(site => site.id), ['sdk-site']);
    assert.deepEqual(identity.assets, []);
    const session = await energy.createSession({ site_id: 'sdk-site' });
    assert.equal(session.siteId, 'sdk-site');
    const search = await session.search({ query: 'fixture calculate value' });
    assert.equal(search.tools[0].name, 'FIXTURE_CALCULATE');
    const executed = await session.execute({ tool: 'FIXTURE_CALCULATE', arguments: { base: 17, multiplier: 3 } });
    assert.equal(executed.ok, true); assert.equal(executed.result.kind, 'calculated');
    assert.deepEqual(executed.result.data, { value: 51 });
    const request = { capability: 'calculate_fixture_value', arguments: { base: 17, multiplier: 3 } };
    const resolution = await session.resolve(request); assert.equal(resolution.status, 'resolved');
    const result = await session.capability(request); assert.equal(result.ok, true); assert.equal(result.result.data.value, 51);
    const unavailable = await session.capability({ capability: 'missing_fixture' });
    assert.equal(unavailable.ok, false); assert.equal(unavailable.error.code, 'capability_unavailable');
    const unsupported = await session.runSkill({ skill_id: 'missing_fixture_skill' });
    assert.equal(unsupported.ok, false); assert.equal(unsupported.error.code, 'skill_not_found');
    const catalogue = await session.toolkits();
    assert.equal(catalogue.toolkits[0].id, 'fixture_local');
    assert.equal(catalogue.toolkits[0].runtime, 'native');
    assert.equal(catalogue.toolkits[0].auth_required, false);
    const connections = await session.connections(); assert.equal(connections.connections[0].id, 'fixture-local-connection');
    assert.ok((await session.skills()).skills.some(skill => skill.id === 'forecast-bill'));
    assert.equal((await session.skills({ query: 'forecast', limit: 1 })).skills.length, 1);
    assert.deepEqual((await session.artifacts()).artifacts, []);
    const stored = await session.execute({ tool: 'FIXTURE_CALCULATE', arguments: { base: 2, multiplier: 3 }, persist: true });
    assert.equal(stored.ok, true); const artifact = stored.result.data.artifact_id;
    assert.ok((await session.artifacts()).artifacts.some(row => row.artifact_id === artifact));
    assert.equal((await session.deleteArtifact(artifact)).deleted, true);
    const foreign = new EnergyAgentTools({ baseUrl: host.baseUrl, token: host.foreignToken });
    assert.equal((await foreign.identity()).user_id, 'sdk-foreign-user');
    const foreignSession = foreign.session({ sessionId: session.id });
    await assert.rejects(foreignSession.artifacts(), error => error instanceof EnergyHttpError && error.status === 404);
    await assert.rejects(foreignSession.runSkill({ skill_id: 'missing_fixture_skill' }), error => error instanceof EnergyHttpError && error.status === 404);
    const wrong = new EnergyAgentTools({ baseUrl: host.baseUrl, token: 'invalid-acceptance-token' });
    await assert.rejects(wrong.createSession({ site_id: 'sdk-site' }), error => error instanceof EnergyHttpError && error.status === 401);
    assert.throws(() => session.search({ query: '', limit: 11 }), EnergyProtocolError);
    assert.equal((await session.close()).deleted, true);
    await assert.rejects(session.artifacts(), error => error instanceof EnergyHttpError && error.status === 404);
  } finally { await host.close(); }
});
