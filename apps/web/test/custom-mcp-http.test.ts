import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { once } from "node:events";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { EnergyAgentTools } from "@energy-agent-tools/sdk";
import type {
  MCPConnectionInspectionRequest,
  MCPConnectionInspectionResponse,
  MCPConnectionStageRequest,
  MCPConnectionStageResponse,
} from "@energy-agent-tools/sdk";

const projectRoot = fileURLToPath(new URL("../../../", import.meta.url));
const webRoot = join(projectRoot, "apps/web");
const gatewayFixture = join(projectRoot, "scripts/test_managed_mcp_web_host.py");

type ProviderObservation = {
  path: string;
  provider_auth: boolean;
  management_auth: boolean;
};

async function freePort(): Promise<number> {
  const server = createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const port = address.port;
  await new Promise<void>((resolve) => server.close(() => resolve()));
  return port;
}

async function waitUntilReady(url: string, child: ChildProcess, details: string[]): Promise<void> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(`Managed MCP acceptance service exited. ${details.join("")}`);
    }
    try {
      await fetch(url, { signal: AbortSignal.timeout(1_000) });
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw new Error(`Managed MCP acceptance service startup timed out. ${details.join("")}`);
}

async function stop(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = once(child, "exit");
  child.kill("SIGTERM");
  await exited;
}

function responseCookie(response: Response, name: string): string | null {
  const header = response.headers.get("set-cookie") ?? "";
  const value = new RegExp(`(?:^|,\\s*)${name}=([^;,\\s]+)`).exec(header)?.[1];
  return value ? `${name}=${value}` : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isInspection(value: unknown): value is MCPConnectionInspectionResponse {
  if (!isRecord(value) || !isRecord(value.inspection)) return false;
  const inspection = value.inspection;
  if (
    typeof inspection.schema_digest !== "string" ||
    inspection.annotations_untrusted !== true ||
    !Array.isArray(inspection.tools)
  ) return false;
  return inspection.tools.every((tool: unknown) =>
    isRecord(tool) && typeof tool.name === "string" && typeof tool.schema_hash === "string"
  );
}

function isStageResponse(value: unknown): value is MCPConnectionStageResponse {
  if (!isRecord(value) || value.ok !== true || !isRecord(value.account)) return false;
  return typeof value.account.id === "string" &&
    typeof value.account.toolkit === "string" &&
    typeof value.schema_digest === "string" &&
    typeof value.selected_tool_count === "number";
}

async function readProviderObservations(path: string): Promise<ProviderObservation[]> {
  const contents = await readFile(path, "utf8");
  if (contents.trim() === "") return [];
  const observations: ProviderObservation[] = [];
  for (const line of contents.trim().split("\n")) {
    const value: unknown = JSON.parse(line);
    if (
      !isRecord(value) ||
      typeof value.path !== "string" ||
      typeof value.provider_auth !== "boolean" ||
      typeof value.management_auth !== "boolean"
    ) assert.fail("Provider fixture returned an invalid safe observation.");
    observations.push({
      path: value.path,
      provider_auth: value.provider_auth,
      management_auth: value.management_auth,
    });
  }
  return observations;
}

test("production web reviews, stages, maps, scopes, recovers, and disconnects a synthetic MCP provider", { timeout: 240_000 }, async () => {
  const gatewayPort = await freePort();
  const webPort = await freePort();
  const upstreamPort = await freePort();
  const gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
  const webUrl = `http://127.0.0.1:${webPort}`;
  const upstreamUrl = `http://127.0.0.1:${upstreamPort}/mcp`;
  const stateDir = await mkdtemp(join(tmpdir(), "energy-managed-mcp-web-"));
  const providerCredential = `fixture-${randomBytes(32).toString("hex")}`;
  const webSessionKey = randomBytes(32).toString("hex");
  const python = process.env.ENERGY_WEB_TEST_PYTHON ?? join(projectRoot, ".venv/bin/python");
  const gatewayErrors: string[] = [];
  let gateway: ChildProcess | null = null;
  const web = spawn(
    process.execPath,
    [join(webRoot, "node_modules/next/dist/bin/next"), "start", "--hostname", "127.0.0.1", "--port", String(webPort)],
    {
      cwd: webRoot,
      env: {
        ...process.env,
        NODE_ENV: "production",
        ENERGY_GATEWAY_URL: gatewayUrl,
        ENERGY_WEB_ORIGIN: webUrl,
        ENERGY_PUBLIC_GATEWAY_URL: gatewayUrl,
        ENERGY_WEB_SESSION_KEY: webSessionKey,
      },
      stdio: "ignore",
    },
  );

  function startGateway(): ChildProcess {
    const child = spawn(
      python,
      [gatewayFixture, "--port", String(gatewayPort), "--upstream-port", String(upstreamPort), "--state-dir", stateDir],
      {
        cwd: projectRoot,
        env: {
          ...process.env,
          ENERGY_MCP_WEB_TEST_PROVIDER_CREDENTIAL: providerCredential,
          PYTHONPATH: [join(projectRoot, "src"), process.env.PYTHONPATH].filter(Boolean).join(delimiter),
        },
        stdio: ["ignore", "ignore", "pipe"],
      },
    );
    child.stderr?.on("data", (chunk: Buffer) => {
      gatewayErrors.push(chunk.toString("utf8").replaceAll(providerCredential, "[redacted]"));
    });
    return child;
  }

  async function postForm(
    path: string,
    fields: Record<string, string>,
    cookie?: string,
    origin: string | null = webUrl,
  ): Promise<Response> {
    const headers: Record<string, string> = { "content-type": "application/x-www-form-urlencoded" };
    if (origin !== null) headers.origin = origin;
    if (cookie !== undefined) headers.cookie = cookie;
    return fetch(webUrl + path, {
      method: "POST",
      headers,
      body: new URLSearchParams(fields),
      redirect: "manual",
    });
  }

  async function postJson(
    path: string,
    body: unknown,
    cookie?: string,
    origin: string | null = webUrl,
  ): Promise<Response> {
    const serialized = JSON.stringify(body);
    assert.ok(serialized !== undefined);
    return postRawJson(path, serialized, cookie, origin);
  }

  async function postRawJson(
    path: string,
    body: string,
    cookie?: string,
    origin: string | null = webUrl,
  ): Promise<Response> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (origin !== null) headers.origin = origin;
    if (cookie !== undefined) headers.cookie = cookie;
    return fetch(webUrl + path, { method: "POST", headers, body, redirect: "manual" });
  }

  let managerToken = "";
  let foreignManagerToken = "";
  const sessions: Array<Awaited<ReturnType<EnergyAgentTools["createSession"]>>> = [];

  try {
    gateway = startGateway();
    const initialGateway = gateway;
    await Promise.all([
      waitUntilReady(`${gatewayUrl}/me`, initialGateway, gatewayErrors),
      waitUntilReady(webUrl, web, []),
    ]);
    managerToken = await readFile(join(stateDir, "manager.token"), "ascii");
    foreignManagerToken = await readFile(join(stateDir, "foreign-manager.token"), "ascii");
    const ownerSiteId = await readFile(join(stateDir, "fixture.json"), "utf8").then((contents) => {
      const value: unknown = JSON.parse(contents);
      if (!isRecord(value) || typeof value.site_id !== "string") {
        assert.fail("Managed MCP fixture did not provide its owner site.");
      }
      return value.site_id;
    });
    const otherSiteId = await readFile(join(stateDir, "fixture.json"), "utf8").then((contents) => {
      const value: unknown = JSON.parse(contents);
      if (!isRecord(value) || typeof value.other_site_id !== "string") {
        assert.fail("Managed MCP fixture did not provide its other site.");
      }
      return value.other_site_id;
    });
    const manager = new EnergyAgentTools({ baseUrl: gatewayUrl, token: managerToken });
    const identity = await manager.identity();
    assert.equal(identity.can_manage_workspace, true);
    assert.equal(identity.workspace?.mode, "managed");
    assert.ok(identity.sites.some((site) => site.id === ownerSiteId));
    assert.ok(identity.sites.some((site) => site.id === otherSiteId));
    const workspace = manager.workspace();
    const inspectionRequest = {
      url: upstreamUrl,
      auth_scheme: "bearer",
      auth_header: "Authorization",
      credential: providerCredential,
    } satisfies MCPConnectionInspectionRequest;

    const ownerLogin = await postForm("/api/auth", { token: managerToken });
    assert.equal(ownerLogin.status, 303);
    const cookie = responseCookie(ownerLogin, "energy_web_session");
    assert.ok(cookie);
    assert.ok(!(ownerLogin.headers.get("set-cookie") ?? "").includes(managerToken));

    const initialProviderRequests = await readProviderObservations(join(stateDir, "provider-observations.jsonl"));
    assert.deepEqual(initialProviderRequests, []);
    const foreignOrigin = await postJson(
      "/api/workspace/mcp/inspect",
      inspectionRequest,
      cookie,
      "https://foreign.invalid",
    );
    assert.equal(foreignOrigin.status, 403);
    assert.ok(!(await foreignOrigin.text()).includes(providerCredential));
    const oversized = await postRawJson(
      "/api/workspace/mcp/inspect",
      JSON.stringify({ ...inspectionRequest, credential: "x".repeat(129 * 1024) }),
      cookie,
    );
    assert.equal(oversized.status, 400);
    assert.ok(!(await oversized.text()).includes(providerCredential));
    assert.deepEqual(
      await readProviderObservations(join(stateDir, "provider-observations.jsonl")),
      initialProviderRequests,
    );

    const wrongTarget = await postJson(
      "/api/workspace/mcp/inspect",
      { ...inspectionRequest, url: `${upstreamUrl}/unapproved` },
      cookie,
    );
    assert.equal(wrongTarget.status, 422);
    assert.ok(!(await wrongTarget.text()).includes(providerCredential));

    const foreignLogin = await postForm("/api/auth", { token: foreignManagerToken });
    assert.equal(foreignLogin.status, 303);
    const foreignCookie = responseCookie(foreignLogin, "energy_web_session");
    assert.ok(foreignCookie);
    const foreignWorkspace = await postJson(
      "/api/workspace/mcp/inspect",
      inspectionRequest,
      foreignCookie,
    );
    assert.equal(foreignWorkspace.status, 422);
    assert.ok(!(await foreignWorkspace.text()).includes(providerCredential));
    assert.deepEqual(
      await readProviderObservations(join(stateDir, "provider-observations.jsonl")),
      initialProviderRequests,
    );

    const inspected = await postJson("/api/workspace/mcp/inspect", inspectionRequest, cookie);
    assert.equal(inspected.status, 200, await inspected.clone().text());
    const inspectionValue: unknown = await inspected.json();
    assert.ok(isInspection(inspectionValue));
    assert.deepEqual(
      new Set(inspectionValue.inspection.tools.map((tool) => tool.name)),
      new Set(["read_energy", "unselected_control"]),
    );
    assert.ok(!JSON.stringify(inspectionValue).includes(providerCredential));
    const schemaDigest = inspectionValue.inspection.schema_digest;

    const stageRequest = {
      ...inspectionRequest,
      display_name: "Reviewed synthetic energy reader",
      schema_digest: schemaDigest,
      reviews: [{
        name: "read_energy",
        reviewed: true,
        actions: ["read-only"],
        kind: "metered",
        unit: "kWh",
      }],
    } satisfies MCPConnectionStageRequest;
    const stagedResponse = await postJson("/api/workspace/mcp/stage", stageRequest, cookie);
    assert.equal(stagedResponse.status, 201, await stagedResponse.clone().text());
    const stagedValue: unknown = await stagedResponse.json();
    assert.ok(isStageResponse(stagedValue));
    assert.equal(stagedValue.account.state, "pending_mapping");
    assert.equal(stagedValue.account.enabled, false);
    assert.equal(stagedValue.account.site_id, null);
    assert.equal(stagedValue.selected_tool_count, 1);
    assert.ok(!JSON.stringify(stagedValue).includes(providerCredential));
    const connectionId = stagedValue.account.id;
    const toolkitId = stagedValue.account.toolkit;
    const toolName = `${connectionId}.read_energy`;
    const pending = (await workspace.connections()).connections.find((account) => account.id === connectionId);
    assert.equal(pending?.state, "pending_mapping");
    assert.equal(pending?.enabled, false);

    const map = await postForm("/api/workspace/connections/action", {
      action: "map",
      connection_id: connectionId,
      site_id: ownerSiteId,
    }, cookie);
    assert.equal(map.status, 200, await map.clone().text());
    assert.deepEqual(await map.json(), { ok: true, action: "map" });
    const active = (await workspace.connections()).connections.find((account) => account.id === connectionId);
    assert.equal(active?.state, "active");
    assert.equal(active?.enabled, true);
    assert.equal(active?.site_id, ownerSiteId);

    const verify = await postForm("/api/workspace/connections/action", {
      action: "verify",
      connection_id: connectionId,
    }, cookie);
    assert.equal(verify.status, 200, await verify.clone().text());
    const health: unknown = await verify.json();
    assert.ok(isRecord(health));
    assert.equal(health.status, "healthy");
    assert.ok(!JSON.stringify(health).includes(providerCredential));

    const siteSession = await manager.createSession({ site_id: ownerSiteId });
    sessions.push(siteSession);
    const catalogue = await siteSession.toolkits();
    assert.ok(catalogue.toolkits.some((toolkit) => toolkit.id === toolkitId));
    const execution = await siteSession.execute({ tool: toolName, arguments: {} });
    assert.equal(execution.ok, true);
    assert.equal(execution.result.kind, "metered");
    assert.equal(execution.result.unit, "kWh");
    assert.ok(!JSON.stringify(execution).includes(providerCredential));
    assert.ok(JSON.stringify(execution.result.data).includes("[REDACTED]"));

    const providerObservations = await readProviderObservations(join(stateDir, "provider-observations.jsonl"));
    assert.ok(providerObservations.length > 0);
    assert.ok(providerObservations.every((item) => item.provider_auth));
    assert.ok(providerObservations.every((item) => !item.management_auth));

    const otherSiteSession = await manager.createSession({ site_id: otherSiteId });
    sessions.push(otherSiteSession);
    const beforeOutOfScope = providerObservations.length;
    const outOfScope = await otherSiteSession.execute({ tool: toolName, arguments: {} });
    assert.equal(outOfScope.ok, false);
    if (outOfScope.ok === false) {
      assert.equal(outOfScope.error.code, "tool_forbidden");
      assert.ok(!JSON.stringify(outOfScope).includes(providerCredential));
    }
    assert.equal(
      (await readProviderObservations(join(stateDir, "provider-observations.jsonl"))).length,
      beforeOutOfScope,
    );

    await stop(gateway);
    gateway = startGateway();
    const recoveredGateway = gateway;
    await waitUntilReady(`${gatewayUrl}/me`, recoveredGateway, gatewayErrors);
    const recovered = (await manager.workspace().connections()).connections.find((account) => account.id === connectionId);
    assert.equal(recovered?.state, "active");
    assert.equal(recovered?.site_id, ownerSiteId);
    const recoveredSession = await manager.createSession({ site_id: ownerSiteId });
    sessions.push(recoveredSession);
    const recoveredExecution = await recoveredSession.execute({ tool: toolName, arguments: {} });
    assert.equal(recoveredExecution.ok, true);
    assert.ok(!JSON.stringify(recoveredExecution).includes(providerCredential));
    assert.ok(JSON.stringify(recoveredExecution.result.data).includes("[REDACTED]"));

    const disconnect = await postForm("/api/workspace/connections/action", {
      action: "disconnect",
      connection_id: connectionId,
    }, cookie);
    assert.equal(disconnect.status, 200, await disconnect.clone().text());
    assert.deepEqual(await disconnect.json(), { ok: true, action: "disconnect" });
    const revoked = (await manager.workspace().connections()).connections.find((account) => account.id === connectionId);
    assert.equal(revoked?.state, "revoked");
    assert.equal(revoked?.enabled, false);
    const afterDisconnect = await recoveredSession.execute({ tool: toolName, arguments: {} });
    assert.equal(afterDisconnect.ok, false);
    assert.ok(!JSON.stringify(afterDisconnect).includes(providerCredential));

    const finalObservations = await readProviderObservations(join(stateDir, "provider-observations.jsonl"));
    assert.ok(finalObservations.length > providerObservations.length);
    assert.ok(finalObservations.every((item) => item.provider_auth));
    assert.ok(finalObservations.every((item) => !item.management_auth));
    assert.ok(!(await readFile(join(stateDir, "provider-observations.jsonl"), "utf8")).includes(providerCredential));
  } finally {
    await Promise.all(sessions.map((session) => session.close().catch(() => undefined)));
    await Promise.all([stop(web), gateway === null ? Promise.resolve() : stop(gateway)]);
    await rm(stateDir, { recursive: true, force: true });
  }
});
