import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:net';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { EnergyAgentTools, EnergyHttpError, EnergyProtocolError } from '../dist/index.js';

const root = fileURLToPath(new URL('../../..', import.meta.url));
const python = process.env.ENERGY_AGENT_TEST_PYTHON ?? join(root, '.venv/bin/python');

async function startHost({ withOtherWorkspace = false } = {}) {
  const allocation = createServer();
  allocation.listen(0, '127.0.0.1');
  await once(allocation, 'listening');
  const address = allocation.address();
  assert.ok(address && typeof address !== 'string');
  const port = address.port;
  await new Promise(resolve => allocation.close(resolve));

  const state = await mkdtemp(join(tmpdir(), 'energy-sdk-managed-'));
  const tokenFile = join(state, 'one-time-management-token');
  const otherTokenFile = join(state, 'other-management-token');
  const hostArguments = [
    join(root, 'packages/typescript/test/managed_host.py'),
    '--port', String(port),
    '--state-dir', state,
    '--token-file', tokenFile,
  ];
  if (withOtherWorkspace) hostArguments.push('--other-token-file', otherTokenFile);
  const child = spawn(python, hostArguments, {
    cwd: root,
    env: {
      ...process.env,
      PYTHONPATH: [join(root, 'src'), process.env.PYTHONPATH].filter(Boolean).join(delimiter),
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let exited = false;
  child.once('exit', () => { exited = true; });
  let launchFailed = false;
  child.once('error', () => { launchFailed = true; exited = true; });
  const baseUrl = `http://127.0.0.1:${port}`;
  let managementToken;
  let otherManagementToken;

  try {
    const deadline = Date.now() + 15000;
    while (!managementToken || (withOtherWorkspace && !otherManagementToken)) {
      if (exited) throw new Error('Managed acceptance host exited during startup.');
      try {
        managementToken = await readFile(tokenFile, 'utf8');
        await unlink(tokenFile);
      } catch { }
      if (withOtherWorkspace && !otherManagementToken) {
        try {
          otherManagementToken = await readFile(otherTokenFile, 'utf8');
          await unlink(otherTokenFile);
        } catch { }
      }
      if (!managementToken || (withOtherWorkspace && !otherManagementToken)) {
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
      otherManagementToken,
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

async function waitForCompletedJob(gateway, jobId) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const response = await gateway.jobAction(jobId, { operation: 'status' });
    assert.equal(response.ok, true, JSON.stringify(response.error));
    if (response.job.status === 'completed') return response.job;
    assert.ok(
      ['pending', 'running'].includes(response.job.status),
      `Unexpected job status: ${response.job.status}`,
    );
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.fail(`Job ${jobId} did not complete before the acceptance timeout.`);
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
    const forecastSkill = (await workspace.skills()).skills.find(item => item.id === 'forecast-bill');
    assert.equal(forecastSkill.executable, true);
    assert.ok(forecastSkill.evidence_required.length > 0);

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
    await assert.rejects(scoped.workspace().skills(), hasStatus(403));

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

test('managed gateway discovers scoped simulation jobs and acts on completed results', async () => {
  const host = await startHost({ withOtherWorkspace: true });
  try {
    const management = new EnergyAgentTools({ baseUrl: host.baseUrl, token: host.managementToken });
    const workspace = management.workspace();
    const foreignGateway = new EnergyAgentTools({
      baseUrl: host.baseUrl,
      token: host.otherManagementToken,
    });
    const identity = await management.identity();
    const foreignIdentity = await foreignGateway.identity();
    assert.equal(foreignIdentity.user_id, identity.user_id);
    assert.notEqual(
      (await foreignGateway.workspace().details()).workspace.id,
      (await workspace.details()).workspace.id,
    );

    assert.throws(() => management.jobHistory({ limit: 0 }), EnergyProtocolError);
    assert.throws(() => management.jobHistory({ limit: 101 }), EnergyProtocolError);
    assert.throws(() => management.jobHistory({ before: 'invalid+cursor' }), EnergyProtocolError);
    assert.throws(() => management.jobHistory({ status: 'unknown' }), EnergyProtocolError);
    assert.throws(
      () => management.jobAction('0'.repeat(32), { operation: 'resume' }),
      EnergyProtocolError,
    );

    const site = await workspace.createSite({
      name: 'Job history site',
      timezone: 'Europe/London',
    });
    const issued = await workspace.createAgentKey({
      name: 'Job history agent',
      site_ids: [site.site.id],
    });
    const agent = new EnergyAgentTools({ baseUrl: host.baseUrl, token: issued.token });
    const session = await agent.createSession({ site_id: site.site.id });
    const arguments_ = {
      indoor_temp_c: 21,
      outdoor_temp_c: 2,
      components: [{ name: 'wall', area_m2: 100, u_value_w_m2k: 0.2 }],
      air_changes_per_hour: 0.4,
    };
    const submitted = [];
    for (let index = 0; index < 3; index += 1) {
      const response = await session.job({
        operation: 'submit',
        simulation: 'heat_loss',
        arguments: arguments_,
      });
      assert.equal(response.ok, true, JSON.stringify(response.error));
      assert.equal(response.job.operation, 'heat_loss');
      submitted.push(response.job.job_id);
    }
    await session.close();

    for (const jobId of submitted) await waitForCompletedJob(management, jobId);

    const metadataFields = [
      'access_mode', 'created_at', 'error_code', 'finished_at', 'input_bytes', 'job_id',
      'operation', 'output_bytes', 'session_id', 'site_id', 'started_at', 'status', 'user_id',
      'workspace_id',
    ].sort();
    const firstPage = await management.jobHistory({ limit: 1 });
    assert.equal(firstPage.jobs.length, 1);
    assert.match(firstPage.next_before, /^[A-Za-z0-9_-]+$/);
    const pages = [firstPage];
    while (pages.at(-1).next_before !== null) {
      pages.push(await management.jobHistory({ limit: 1, before: pages.at(-1).next_before }));
    }
    assert.equal(pages.length, 3);
    const metadata = pages.flatMap(page => page.jobs);
    assert.deepEqual(new Set(metadata.map(job => job.job_id)), new Set(submitted));
    for (const job of metadata) {
      assert.deepEqual(Object.keys(job).sort(), metadataFields);
      assert.equal(job.status, 'completed');
      const encoded = JSON.stringify(job);
      for (const privateField of [
        'arguments', 'indoor_temp_c', 'outdoor_temp_c', 'components', 'gross_heat_loss_kw',
        'result', 'error_message', 'message', 'path',
      ]) assert.equal(encoded.includes(privateField), false);
    }

    const completed = await management.jobHistory({ status: 'completed', limit: 100 });
    assert.deepEqual(new Set(completed.jobs.map(job => job.job_id)), new Set(submitted));
    assert.deepEqual((await management.jobHistory({ status: 'failed' })).jobs, []);
    await assert.rejects(
      management.jobHistory({ before: 'AAAA' }),
      error => error instanceof EnergyHttpError && error.status === 400 && error.code === 'invalid_cursor',
    );

    const result = await management.jobAction(submitted[0], { operation: 'result' });
    assert.equal(result.ok, true);
    assert.equal(result.result.data.gross_heat_loss_kw, 0.38);

    const terminalCancel = await management.jobAction(submitted[1], { operation: 'cancel' });
    assert.equal(terminalCancel.ok, true);
    assert.equal(terminalCancel.job.status, 'completed');
    const deletion = await management.jobAction(submitted[2], { operation: 'delete' });
    assert.equal(deletion.ok, true);
    assert.equal(deletion.deleted, true);
    assert.equal(
      (await management.jobHistory({ limit: 100 })).jobs.some(job => job.job_id === submitted[2]),
      false,
    );

    assert.deepEqual((await foreignGateway.jobHistory({ limit: 100 })).jobs, []);
    await assert.rejects(
      foreignGateway.jobAction(submitted[0], { operation: 'status' }),
      hasStatus(403),
    );
    assert.equal(
      (await management.jobAction(submitted[0], { operation: 'status' })).job.status,
      'completed',
    );
  } finally {
    await host.close();
  }
});

test('managed SDK requests scoped start after its originating session closes', async () => {
  const host = await startHost({ withOtherWorkspace: true });
  try {
    const management = new EnergyAgentTools({ baseUrl: host.baseUrl, token: host.managementToken });
    const foreignGateway = new EnergyAgentTools({
      baseUrl: host.baseUrl,
      token: host.otherManagementToken,
    });
    const workspace = management.workspace();
    const site = await workspace.createSite({
      name: 'Job start site',
      timezone: 'Europe/London',
    });
    const issued = await workspace.createAgentKey({
      name: 'Job start agent',
      site_ids: [site.site.id],
    });
    const agent = new EnergyAgentTools({ baseUrl: host.baseUrl, token: issued.token });
    const session = await agent.createSession({ site_id: site.site.id });
    const submitted = await session.job({
      operation: 'submit',
      simulation: 'heat_loss',
      arguments: {
        indoor_temp_c: 21,
        outdoor_temp_c: 2,
        components: [{ name: 'wall', area_m2: 100, u_value_w_m2k: 0.2 }],
        air_changes_per_hour: 0.4,
      },
    });
    assert.equal(submitted.ok, true, JSON.stringify(submitted.error));
    await session.close();

    const started = await management.jobAction(submitted.job.job_id, { operation: 'start' });
    if (started.ok) {
      assert.equal(started.job.job_id, submitted.job.job_id);
      assert.equal(started.job.session_id, submitted.job.session_id);
      assert.ok(['pending', 'running'].includes(started.job.status));
    } else {
      assert.deepEqual(started.error, {
        code: 'job_not_pending',
        message: 'Only pending jobs can be started.',
      });
      const alreadyFinished = await management.jobAction(submitted.job.job_id, { operation: 'status' });
      assert.equal(alreadyFinished.ok, true);
      assert.equal(alreadyFinished.job.status, 'completed');
    }
    assert.equal(JSON.stringify(started).includes('indoor_temp_c'), false);

    const completed = await waitForCompletedJob(management, submitted.job.job_id);
    assert.equal(completed.session_id, submitted.job.session_id);
    const result = await management.jobAction(submitted.job.job_id, { operation: 'result' });
    assert.equal(result.ok, true, JSON.stringify(result.error));
    assert.equal(result.result.data.gross_heat_loss_kw, 0.38);

    const history = await management.jobHistory({ limit: 100 });
    assert.ok(history.jobs.some(job => job.job_id === submitted.job.job_id));
    assert.deepEqual((await foreignGateway.jobHistory({ limit: 100 })).jobs, []);
    await assert.rejects(
      foreignGateway.jobAction(submitted.job.job_id, { operation: 'start' }),
      hasStatus(403),
    );
  } finally {
    await host.close();
  }
});
