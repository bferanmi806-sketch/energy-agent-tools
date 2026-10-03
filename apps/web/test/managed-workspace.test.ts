import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { once } from "node:events";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { EnergyAgentTools } from "@energy-agent-tools/sdk";
import { bindOAuthFlowToSession, OAUTH_FLOW_MAX_TTL_MS, sealOAuthFlow } from "../src/lib/security.js";

const projectRoot = fileURLToPath(new URL("../../../", import.meta.url));
const webRoot = join(projectRoot, "apps/web");

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

async function waitUntilReady(url: string, child: ChildProcess): Promise<void> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error("Managed acceptance service exited.");
    try {
      await fetch(url, { signal: AbortSignal.timeout(1_000) });
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw new Error("Managed acceptance service startup timed out.");
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

async function readOAuthObservations(path: string): Promise<{ authCode: number; stateReads: number }> {
  const value: unknown = JSON.parse(await readFile(path, "utf8"));
  if (!isRecord(value) || typeof value.auth_code !== "number" || typeof value.state_reads !== "number") {
    assert.fail("The managed OAuth fixture did not return valid provider observations.");
  }
  return { authCode: value.auth_code, stateReads: value.state_reads };
}

function managedGatewaySource(): string {
  return String.raw`from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import os
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from energy_agent_tools.app import build_agent
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.hosting import create_host


def create_app(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    control = ControlStore(base / "control")
    first = control.bootstrap_workspace("Owner", "Managed home")
    second = control.bootstrap_workspace("Other owner", "Second workspace")
    second_site = control.create_site(second.user.id, second.workspace.id, name="Other site", timezone="UTC")
    for name, token in (("manager.token", first.key.token), ("other-manager.token", second.key.token)):
        path = base / name
        path.write_text(token, encoding="ascii")
        path.chmod(0o600)
    (base / "other-site.id").write_text(second_site.id, encoding="ascii")
    (base / "other-site.id").chmod(0o600)

    agent_root = base / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    vault_key = agent_root / "vault.key"
    vault_key.write_bytes(Fernet.generate_key())
    vault_key.chmod(0o600)
    agent = build_agent(agent_root, {"vault": {"master_key_file": "vault.key"}})
    provider_key = os.environ["ENERGY_WEB_TEST_OCTOPUS_KEY"]
    expected = "Basic " + base64.b64encode(f"{provider_key}:".encode()).decode()
    probes = [0]

    def provider(request: httpx.Request) -> httpx.Response:
        probes[0] += 1
        (base / "provider-probes").write_text(str(probes[0]), encoding="ascii")
        if request.url.host != "api.octopus.energy":
            return httpx.Response(503)
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return httpx.Response(401)
        return httpx.Response(200, json={"results": [{
            "consumption": 1.25,
            "interval_start": "2026-09-30T00:00:00Z",
            "interval_end": "2026-09-30T00:30:00Z",
        }], "next": None})

    asyncio.run(agent.http.aclose())
    agent.http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    agent._owns_http = False
    return create_host(
        agent,
        {},
        close_agent_on_shutdown=True,
        control_store=control,
        managed_workspaces=True,
        max_requests_per_minute=300,
    )


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    uvicorn.run(create_app(args.state_dir), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
`;
}

test("production web onboards a zero-site manager through the real managed gateway", { timeout: 180_000 }, async () => {
  const gatewayPort = await freePort();
  const webPort = await freePort();
  const gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
  const webUrl = `http://127.0.0.1:${webPort}`;
  const stateDir = await mkdtemp(join(tmpdir(), "energy-managed-web-"));
  const providerKey = randomBytes(32).toString("hex");
  const gatewayScript = join(stateDir, "managed_gateway.py");
  await writeFile(gatewayScript, managedGatewaySource(), { mode: 0o600 });
  await chmod(gatewayScript, 0o600);

  const python = process.env.ENERGY_WEB_TEST_PYTHON ?? join(projectRoot, ".venv/bin/python");
  const gateway = spawn(
    python,
    [gatewayScript, "--port", String(gatewayPort), "--state-dir", stateDir],
    {
      cwd: projectRoot,
      env: {
        ...process.env,
        ENERGY_WEB_TEST_OCTOPUS_KEY: providerKey,
        PYTHONPATH: [join(projectRoot, "src"), process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      },
      stdio: ["ignore", "ignore", "pipe"],
    },
  );
  const gatewayError: string[] = [];
  gateway.stderr?.on("data", (chunk: Buffer) => {
    gatewayError.push(chunk.toString("utf8").replaceAll(providerKey, "[redacted]"));
  });
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
        ENERGY_WEB_SESSION_KEY: randomBytes(32).toString("hex"),
      },
      stdio: "ignore",
    },
  );

  async function post(
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

  try {
    await Promise.all([
      waitUntilReady(`${gatewayUrl}/me`, gateway).catch((error: unknown) => {
        throw new Error(`${error instanceof Error ? error.message : "Gateway startup failed."} ${gatewayError.join("")}`);
      }),
      waitUntilReady(webUrl, web),
    ]);
    const managerToken = await readFile(join(stateDir, "manager.token"), "ascii");
    const otherManagerToken = await readFile(join(stateDir, "other-manager.token"), "ascii");
    const otherSiteId = await readFile(join(stateDir, "other-site.id"), "ascii");
    const manager = new EnergyAgentTools({ baseUrl: gatewayUrl, token: managerToken });
    const identity = await manager.identity();
    assert.equal(identity.can_manage_workspace, true);
    assert.equal(identity.workspace?.mode, "managed");
    assert.deepEqual(identity.sites, []);
    const workspace = manager.workspace();
    const catalogue = await workspace.toolkits();
    assert.ok(catalogue.toolkits.some((toolkit) => toolkit.id === "octopus-energy-account"));
    assert.deepEqual((await workspace.sites()).sites, []);

    const landing = await fetch(webUrl);
    assert.equal(landing.status, 200);
    assert.match(await landing.text(), /Gateway access key/);
    assert.equal((await post("/api/auth", { token: managerToken }, undefined, null)).status, 403);
    assert.equal((await post("/api/auth", { token: managerToken }, undefined, "https://foreign.invalid")).status, 403);
    const login = await post("/api/auth", { token: managerToken });
    assert.equal(login.status, 303);
    const setCookie = login.headers.get("set-cookie");
    assert.ok(setCookie);
    assert.match(setCookie, /HttpOnly/);
    assert.match(setCookie, /SameSite=Lax/);
    assert.ok(!setCookie.includes(managerToken));
    const cookie = setCookie.split(";", 1)[0];
    assert.ok(cookie);

    const dashboard = await fetch(webUrl, { headers: { cookie } });
    assert.equal(dashboard.status, 200);
    const dashboardHtml = await dashboard.text();
    assert.match(dashboardHtml, /Connect a system/);
    assert.match(dashboardHtml, /Managed home/);
    assert.match(dashboardHtml, /Octopus/);
    assert.match(dashboardHtml, /octopus-energy-account/);
    assert.doesNotMatch(dashboardHtml, /No site selected/);
    assert.ok(!dashboardHtml.includes(managerToken));

    const beforeRejected = await workspace.connections();
    assert.deepEqual(beforeRejected.connections, []);
    const providerFields = {
      provider: "octopus",
      credential: providerKey,
      mpan: "1234567890123",
      serial_number: "TEST123",
    };
    assert.equal((await post("/api/workspace/connections", providerFields, cookie, null)).status, 403);
    assert.equal((await post("/api/workspace/connections", providerFields, cookie, "https://foreign.invalid")).status, 403);
    assert.equal((await post("/api/workspace/connections", providerFields)).status, 401);
    assert.equal((await post("/api/workspace/connections", { ...providerFields, workspace_id: "spoofed" }, cookie)).status, 400);
    const missingProvider = await post("/api/workspace/connections", { credential: providerKey, mpan: "1234567890123", serial_number: "TEST123" }, cookie);
    assert.equal(missingProvider.status, 400);
    assert.equal((await post("/api/workspace/connections", { ...providerFields, credential: "x".repeat(17_000) }, cookie)).status, 400);
    const providerRefusal = await post("/api/workspace/connections", { ...providerFields, credential: "wrong-fixture-key" }, cookie);
    assert.equal(providerRefusal.status, 422);
    const safeProviderError = await providerRefusal.text();
    assert.ok(!safeProviderError.includes("wrong-fixture-key"));
    assert.ok(!safeProviderError.includes(managerToken));
    assert.deepEqual((await workspace.connections()).connections, []);

    const stage = await post("/api/workspace/connections", providerFields, cookie);
    assert.equal(stage.status, 201);
    const stageJson: unknown = await stage.json();
    assert.ok(typeof stageJson === "object" && stageJson !== null && "connection_id" in stageJson);
    if (typeof stageJson !== "object" || stageJson === null || !("connection_id" in stageJson) || typeof stageJson.connection_id !== "string") {
      assert.fail("The staged connection response did not include its safe connection ID.");
    }
    const connectionId = stageJson.connection_id;
    assert.ok(!JSON.stringify(stageJson).includes(providerKey));
    const stagedConnections = (await workspace.connections()).connections;
    assert.equal(stagedConnections.length, 1);
    assert.equal(stagedConnections[0]?.id, connectionId);
    assert.equal(stagedConnections[0]?.state, "pending_mapping");
    assert.equal(stagedConnections[0]?.enabled, false);
    assert.equal(stagedConnections[0]?.site_id, null);
    assert.equal(await readFile(join(stateDir, "provider-probes"), "ascii"), "2");

    const otherTokenLogin = await post("/api/auth", { token: otherManagerToken });
    assert.equal(otherTokenLogin.status, 303);
    const otherCookie = otherTokenLogin.headers.get("set-cookie")?.split(";", 1)[0];
    assert.ok(otherCookie);
    const otherWorkspaceAttempt = await post(
      "/api/workspace/connections/action",
      { action: "map", connection_id: connectionId, site_id: otherSiteId },
      otherCookie,
    );
    assert.equal(otherWorkspaceAttempt.status, 403);
    assert.ok(!(await otherWorkspaceAttempt.text()).includes(providerKey));

    const wrongWorkspaceSite = await post(
      "/api/workspace/connections/action",
      { action: "map", connection_id: connectionId, site_id: otherSiteId },
      cookie,
    );
    assert.equal(wrongWorkspaceSite.status, 403);
    const malformedMap = await post(
      "/api/workspace/connections/action",
      { action: "map", connection_id: connectionId },
      cookie,
    );
    assert.equal(malformedMap.status, 400);
    assert.equal(await readFile(join(stateDir, "provider-probes"), "ascii"), "2");
    assert.equal((await workspace.connections()).connections[0]?.state, "pending_mapping");

    const createdSite = await post("/api/workspace/sites", { name: "Home", timezone: "Europe/London" }, cookie);
    assert.equal(createdSite.status, 201);
    const createdSiteJson: unknown = await createdSite.json();
    assert.ok(typeof createdSiteJson === "object" && createdSiteJson !== null && "site" in createdSiteJson);
    if (typeof createdSiteJson !== "object" || createdSiteJson === null || !("site" in createdSiteJson) || typeof createdSiteJson.site !== "object" || createdSiteJson.site === null || !("id" in createdSiteJson.site) || typeof createdSiteJson.site.id !== "string") {
      assert.fail("The site response did not include its generated ID.");
    }
    const siteId = createdSiteJson.site.id;
    const mapping = await post("/api/workspace/connections/action", { action: "map", connection_id: connectionId, site_id: siteId }, cookie);
    assert.equal(mapping.status, 200);
    assert.deepEqual(await mapping.json(), { ok: true, action: "map" });
    const activeConnection = (await workspace.connections()).connections[0];
    assert.equal(activeConnection?.state, "active");
    assert.equal(activeConnection?.enabled, true);
    assert.equal(activeConnection?.site_id, siteId);
    assert.equal(await readFile(join(stateDir, "provider-probes"), "ascii"), "3");

    const verify = await post("/api/workspace/connections/action", { action: "verify", connection_id: connectionId }, cookie);
    assert.equal(verify.status, 200);
    const verified: unknown = await verify.json();
    assert.ok(typeof verified === "object" && verified !== null && "status" in verified && verified.status === "healthy");
    assert.ok(!JSON.stringify(verified).includes(providerKey));

    const asset = await post(
      "/api/workspace/assets",
      { site_id: siteId, name: "Main meter", kind: "meter", account_id: connectionId },
      cookie,
    );
    assert.equal(asset.status, 201);
    const assets = await workspace.assets();
    assert.equal(assets.assets.length, 1);
    assert.equal(assets.assets[0]?.site_id, siteId);
    assert.deepEqual(assets.assets[0]?.account_ids, [connectionId]);

    const issuedResponse = await post("/api/workspace/keys", { name: "Home assistant", site_id: siteId }, cookie);
    assert.equal(issuedResponse.status, 201);
    const issuedJson: unknown = await issuedResponse.json();
    assert.ok(typeof issuedJson === "object" && issuedJson !== null && "token" in issuedJson && typeof issuedJson.token === "string");
    if (typeof issuedJson !== "object" || issuedJson === null || !("token" in issuedJson) || typeof issuedJson.token !== "string") {
      assert.fail("The agent key response did not contain a one-time token.");
    }
    const agentToken = issuedJson.token;
    assert.equal(agentToken.length, 47);
    assert.notEqual(agentToken, managerToken);
    assert.ok(!JSON.stringify(issuedJson).includes(providerKey));
    const keyRecords = (await workspace.keys()).keys;
    const agentRecord = keyRecords.find((key) => key.name === "Home assistant");
    assert.ok(agentRecord);
    assert.equal(agentRecord.access.kind, "agent");
    assert.deepEqual(agentRecord.access.kind === "agent" ? agentRecord.access.site_ids : [], [siteId]);
    assert.ok(!JSON.stringify(keyRecords).includes(agentToken));

    const afterIssueHtml = await fetch(`${webUrl}/?view=agent`, { headers: { cookie } }).then((response) => response.text());
    assert.match(afterIssueHtml, /Home assistant/);
    assert.ok(!afterIssueHtml.includes(agentToken));
    assert.ok(!afterIssueHtml.includes(managerToken));
    assert.ok(!afterIssueHtml.includes(providerKey));

    const agentKeyLogin = await post("/api/auth", { token: agentToken });
    assert.equal(agentKeyLogin.status, 303);
    const agentCookie = agentKeyLogin.headers.get("set-cookie")?.split(";", 1)[0];
    assert.ok(agentCookie);
    const agentCannotCreateSite = await post("/api/workspace/sites", { name: "Denied site", timezone: "UTC" }, agentCookie);
    assert.equal(agentCannotCreateSite.status, 403);
    assert.ok(!(await agentCannotCreateSite.text()).includes(agentToken));

    const disconnect = await post("/api/workspace/connections/action", { action: "disconnect", connection_id: connectionId }, cookie);
    assert.equal(disconnect.status, 200);
    assert.deepEqual(await disconnect.json(), { ok: true, action: "disconnect" });
    assert.equal((await workspace.connections()).connections[0]?.state, "revoked");
    const finalPage = await fetch(`${webUrl}/?view=connections`, { headers: { cookie } }).then((response) => response.text());
    assert.match(finalPage, /Disconnected/);
    assert.ok(!finalPage.includes(managerToken));
    assert.ok(!finalPage.includes(providerKey));
  } finally {
    await Promise.all([stop(web), stop(gateway)]);
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("production web binds Home Assistant callback to the manager session and leaves a pending mapping", { timeout: 180_000 }, async () => {
  const gatewayPort = await freePort();
  const webPort = await freePort();
  const gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
  const webUrl = `http://127.0.0.1:${webPort}`;
  const stateDir = await mkdtemp(join(tmpdir(), "energy-managed-oauth-web-"));
  const managerTokenPath = join(stateDir, "manager.token");
  const otherManagerTokenPath = join(stateDir, "other-manager.token");
  const gatewayFixture = join(projectRoot, "packages/typescript/test/managed_host.py");
  const python = process.env.ENERGY_WEB_TEST_PYTHON ?? join(projectRoot, ".venv/bin/python");
  const webSessionKey = randomBytes(32).toString("hex");
  const gateway = spawn(
    python,
    [
      gatewayFixture,
      "--port", String(gatewayPort),
      "--state-dir", stateDir,
      "--token-file", managerTokenPath,
      "--other-token-file", otherManagerTokenPath,
      "--web-origin", webUrl,
    ],
    {
      cwd: projectRoot,
      env: {
        ...process.env,
        PYTHONPATH: [join(projectRoot, "src"), process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      },
      stdio: "ignore",
    },
  );
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

  async function post(
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

  try {
    await Promise.all([waitUntilReady(`${gatewayUrl}/me`, gateway), waitUntilReady(webUrl, web)]);
    const managerToken = await readFile(managerTokenPath, "ascii");
    const otherManagerToken = await readFile(otherManagerTokenPath, "ascii");
    const managerSdk = new EnergyAgentTools({ baseUrl: gatewayUrl, token: managerToken });
    const configurations = await managerSdk.workspace().authConfigurations();
    assert.deepEqual(configurations.configurations.map((item) => item.id), ["home-assistant"]);

    const managerLogin = await post("/api/auth", { token: managerToken });
    assert.equal(managerLogin.status, 303);
    const managerCookie = responseCookie(managerLogin, "energy_web_session");
    assert.ok(managerCookie);

    const requestFields = {
      configuration_id: "home-assistant",
      entity_id: "sensor.power",
      reviewed_mapping: "reviewed",
      telemetry_role: "current_power",
      unit: "kW",
      quantity_shape: "instantaneous",
    };
    assert.equal((await post("/api/workspace/authorizations", requestFields, managerCookie, null)).status, 403);
    assert.equal((await post("/api/workspace/authorizations", requestFields, managerCookie, "https://foreign.invalid")).status, 403);
    assert.equal((await post("/api/workspace/authorizations", { ...requestFields, base_url: "http://127.0.0.1:18123" }, managerCookie)).status, 400);

    const start = await post("/api/workspace/authorizations", requestFields, managerCookie);
    assert.equal(start.status, 200);
    assert.equal(start.headers.get("location"), null);
    const authorizationResponse = await start.json();
    assert.equal(authorizationResponse.ok, true);
    const destinationValue = authorizationResponse.authorization_url;
    assert.ok(destinationValue);
    const destination = new URL(destinationValue);
    assert.equal(destination.protocol, "http:");
    assert.equal(destination.hostname, "127.0.0.1");
    assert.equal(destination.pathname, "/auth/authorize");
    const state = destination.searchParams.get("state");
    assert.ok(state);
    assert.equal(destination.searchParams.get("redirect_uri"), `${webUrl}/api/workspace/oauth/callback`);
    assert.equal(destination.searchParams.get("client_id"), `${webUrl}/`);
    const flowCookie = responseCookie(start, "energy_web_oauth_flow");
    assert.ok(flowCookie);
    const startCookies = start.headers.get("set-cookie") ?? "";
    assert.match(startCookies, /Path=\/api\/workspace\/oauth\/callback/);
    assert.match(startCookies, /HttpOnly/);
    assert.match(startCookies, /SameSite=Lax/);
    assert.ok(!flowCookie.includes(state));
    const managerFlowCookie = `${managerCookie}; ${flowCookie}`;

    const observationsPath = join(stateDir, "observations.json");
    assert.deepEqual(await readOAuthObservations(observationsPath), { authCode: 0, stateReads: 0 });

    const managerIdentity = await managerSdk.identity();
    const managerWorkspaceId = managerIdentity.workspace?.id;
    assert.ok(managerWorkspaceId);
    const expiredAt = Date.now();
    const expiredFlowValue = sealOAuthFlow({
      version: 1,
      state,
      configurationId: "home-assistant",
      connectionId: "expired-fixture-connection",
      managerId: managerIdentity.user_id,
      workspaceId: managerWorkspaceId,
      sessionBinding: bindOAuthFlowToSession(managerCookie.split("=", 2)[1] ?? ""),
      createdAt: expiredAt - OAUTH_FLOW_MAX_TTL_MS,
      expiresAt: expiredAt - 1,
    }, webSessionKey);
    const expiredFlowCookie = `energy_web_oauth_flow=${expiredFlowValue}`;
    const expired = await callback(
      new URLSearchParams({ state, code: "fixture-authorization-code-private" }),
      `${managerCookie}; ${expiredFlowCookie}`,
    );
    assert.equal(expired.status, 303, await expired.clone().text());
    assert.match(expired.headers.get("location") ?? "", /oauth=invalid/);
    assert.deepEqual(await readOAuthObservations(observationsPath), { authCode: 0, stateReads: 0 });

    async function callback(query: URLSearchParams, cookie: string): Promise<Response> {
      return fetch(`${webUrl}/api/workspace/oauth/callback?${query.toString()}`, {
        headers: { cookie },
        redirect: "manual",
      });
    }

    const missingState = await callback(new URLSearchParams({ code: "fixture-authorization-code-private" }), managerFlowCookie);
    assert.equal(missingState.status, 303);
    assert.match(missingState.headers.get("location") ?? "", /oauth=invalid/);
    assert.ok(!(missingState.headers.get("location") ?? "").includes("fixture-authorization-code-private"));
    assert.deepEqual(await readOAuthObservations(observationsPath), { authCode: 0, stateReads: 0 });

    const otherLogin = await post("/api/auth", { token: otherManagerToken });
    assert.equal(otherLogin.status, 303);
    const otherCookie = responseCookie(otherLogin, "energy_web_session");
    assert.ok(otherCookie);
    const wrongManager = await callback(
      new URLSearchParams({ state, code: "fixture-authorization-code-private" }),
      `${otherCookie}; ${flowCookie}`,
    );
    assert.equal(wrongManager.status, 303);
    assert.match(wrongManager.headers.get("location") ?? "", /oauth=invalid/);
    assert.deepEqual(await readOAuthObservations(observationsPath), { authCode: 0, stateReads: 0 });

    const wrongStateValue = state === "a" ? "b" : `${state.slice(0, -1)}${state.endsWith("a") ? "b" : "a"}`;
    const wrongState = await callback(
      new URLSearchParams({ state: wrongStateValue, code: "fixture-authorization-code-private" }),
      managerFlowCookie,
    );
    assert.equal(wrongState.status, 303);
    assert.match(wrongState.headers.get("location") ?? "", /oauth=invalid/);
    assert.deepEqual(await readOAuthObservations(observationsPath), { authCode: 0, stateReads: 0 });

    const completed = await callback(
      new URLSearchParams({ state, code: "fixture-authorization-code-private" }),
      managerFlowCookie,
    );
    assert.equal(completed.status, 303);
    assert.equal(completed.headers.get("location"), "/?view=connections&oauth=connected");
    assert.ok(!(completed.headers.get("location") ?? "").includes(state));
    assert.ok(!(completed.headers.get("location") ?? "").includes("fixture-authorization-code-private"));
    assert.match(completed.headers.get("set-cookie") ?? "", /energy_web_oauth_flow=;/);
    assert.match(completed.headers.get("set-cookie") ?? "", /Max-Age=0/);

    const connections = (await managerSdk.workspace().connections()).connections;
    assert.equal(connections.length, 1);
    assert.equal(connections[0]?.toolkit, "home-assistant");
    assert.equal(connections[0]?.site_id, null);
    assert.equal(connections[0]?.enabled, false);
    assert.equal(connections[0]?.state, "pending_mapping");
    const observations = await readOAuthObservations(observationsPath);
    assert.deepEqual(observations, { authCode: 1, stateReads: 1 });

    const connectedPage = await fetch(webUrl + completed.headers.get("location"), { headers: { cookie: managerCookie } });
    assert.equal(connectedPage.status, 200);
    const connectedHtml = await connectedPage.text();
    assert.match(connectedHtml, /Ready to map/);
    assert.match(connectedHtml, /Needs a site/);
    assert.match(connectedHtml, /Home Assistant verified the selected sensor/);
    for (const secret of [managerToken, otherManagerToken, state, "fixture-authorization-code-private", "fixture-access-token-private", "fixture-refresh-token-private"]) {
      assert.ok(!connectedHtml.includes(secret));
      assert.ok(!(completed.headers.get("location") ?? "").includes(secret));
    }
  } finally {
    await Promise.all([stop(web), stop(gateway)]);
    await rm(stateDir, { recursive: true, force: true });
  }
});
