import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { once } from "node:events";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { delimiter, join } from "node:path";
import { tmpdir } from "node:os";
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { EnergyAgentTools } from "@energy-agent-tools/sdk";
import type { WorkspaceAuthorizationResponse } from "@energy-agent-tools/sdk";
import { approvedCloudAuthorizationUrl } from "../src/lib/managed-oauth.js";
import { resolveWebOrigin } from "../src/lib/security.js";

const webOrigin = resolveWebOrigin("https://energy.example");
const callback = new URL("/api/workspace/oauth/callback", `${webOrigin.origin}/`).toString();
const state = "cloud-oauth-state-123";
const toolkitByProvider = {
  tesla: "tesla-energy",
  enphase: "enphase-energy",
} as const;
type CloudToolkit = (typeof toolkitByProvider)[keyof typeof toolkitByProvider];
type CloudProvider = keyof typeof toolkitByProvider;

const projectRoot = fileURLToPath(new URL("../../../", import.meta.url));
const webRoot = join(projectRoot, "apps/web");
const teslaClientSecret = "web-fixture-private-tesla-client-secret";
const enphaseClientSecret = "web-fixture-private-enphase-client-secret";
const enphaseApiKey = "web-fixture-private-enphase-api-key";
const providerAccessTokens = [
  "cloud-web-fixture-tesla-access-token",
  "cloud-web-fixture-enphase-access-token",
];
const providerRefreshTokens = [
  "cloud-web-fixture-tesla-refresh-token",
  "cloud-web-fixture-enphase-refresh-token",
];

interface ProviderObservations {
  token_exchanges: Record<CloudProvider, number>;
  provider_reads: Record<CloudProvider, number>;
  resources: Array<{ provider: CloudProvider; resource_id: string }>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertNoSecrets(value: string, ...secrets: string[]): void {
  for (const secret of secrets) assert.ok(!value.includes(secret), `Unexpected secret: ${secret}`);
}

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
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error("Cloud OAuth acceptance service exited during startup.");
    }
    try {
      await fetch(url, { signal: AbortSignal.timeout(1_000) });
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw new Error("Cloud OAuth acceptance service startup timed out.");
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

async function readProviderObservations(path: string): Promise<ProviderObservations> {
  const value: unknown = JSON.parse(await readFile(path, "utf8"));
  if (!isRecord(value) || !isRecord(value.token_exchanges) || !isRecord(value.provider_reads)) {
    assert.fail("Cloud provider observations have an invalid shape.");
  }
  const teslaExchanges = value.token_exchanges.tesla;
  const enphaseExchanges = value.token_exchanges.enphase;
  const teslaReads = value.provider_reads.tesla;
  const enphaseReads = value.provider_reads.enphase;
  if (
    typeof teslaExchanges !== "number" ||
    typeof enphaseExchanges !== "number" ||
    typeof teslaReads !== "number" ||
    typeof enphaseReads !== "number"
  ) assert.fail("Cloud provider observation counters are invalid.");
  if (!Array.isArray(value.resources)) assert.fail("Cloud provider resource observations are invalid.");
  const resources: ProviderObservations["resources"] = value.resources.map((item) => {
    if (
      !isRecord(item) ||
      (item.provider !== "tesla" && item.provider !== "enphase") ||
      typeof item.resource_id !== "string"
    ) assert.fail("Cloud provider resource observation is invalid.");
    return { provider: item.provider, resource_id: item.resource_id };
  });
  return {
    token_exchanges: {
      tesla: teslaExchanges,
      enphase: enphaseExchanges,
    },
    provider_reads: {
      tesla: teslaReads,
      enphase: enphaseReads,
    },
    resources,
  };
}

async function startGatewayProxy(upstreamUrl: string): Promise<{
  baseUrl: string;
  badTargetRequests: () => number;
  close: () => Promise<void>;
}> {
  let badTargetRequests = 0;
  const server = createServer((incoming, outgoing) => {
    void (async () => {
      const chunks: Buffer[] = [];
      for await (const chunk of incoming) chunks.push(Buffer.from(chunk));
      const requestBody = Buffer.concat(chunks).toString("utf8");
      const requestUrl = incoming.url ?? "/";
      const pathname = new URL(requestUrl, "http://gateway.local").pathname;
      const headers = new Headers();
      for (const [name, value] of Object.entries(incoming.headers)) {
        if (name === "host" || name === "connection" || name === "content-length" || value === undefined) continue;
        headers.set(name, Array.isArray(value) ? value.join(",") : value);
      }

      if (incoming.method === "POST" && pathname === "/workspace/provider-authorizations") {
        let body: unknown;
        try {
          body = JSON.parse(requestBody);
        } catch {
          body = null;
        }
        if (isRecord(body) && body.configuration_id === "attacker-target") {
          badTargetRequests += 1;
          outgoing.writeHead(201, { "content-type": "application/json" });
          outgoing.end(JSON.stringify({
            authorization: {
              connection_id: "untrusted-gateway-connection",
              authorization_url: "https://attacker.example/authorize?response_type=code",
              state: "untrusted-gateway-state",
              expires_at: new Date(Date.now() + 60_000).toISOString(),
            },
          }));
          return;
        }
      }

      const init: RequestInit = { method: incoming.method ?? "GET", headers };
      if (requestBody.length > 0) init.body = requestBody;
      const upstreamResponse = await fetch(`${upstreamUrl}${requestUrl}`, init);
      const contentType = upstreamResponse.headers.get("content-type") ?? "application/json";

      if (incoming.method === "GET" && pathname === "/workspace/auth-configurations") {
        const payload: unknown = await upstreamResponse.json();
        if (!isRecord(payload) || !Array.isArray(payload.configurations)) {
          outgoing.writeHead(502, { "content-type": "application/json" });
          outgoing.end("{}");
          return;
        }
        const responseBody = {
          ...payload,
          configurations: [
            ...payload.configurations,
            {
              id: "attacker-target",
              name: "Adversarial gateway fixture",
              toolkit: "tesla-energy",
              protocol: "oauth2_confidential",
              pending_cleanup: 0,
            },
          ],
        };
        outgoing.writeHead(upstreamResponse.status, { "content-type": contentType });
        outgoing.end(JSON.stringify(responseBody));
        return;
      }

      outgoing.writeHead(upstreamResponse.status, { "content-type": contentType });
      outgoing.end(Buffer.from(await upstreamResponse.arrayBuffer()));
    })().catch(() => {
      outgoing.writeHead(502, { "content-type": "application/json" });
      outgoing.end("{}");
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    badTargetRequests: () => badTargetRequests,
    async close() {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}

function authorizationUrl(provider: keyof typeof toolkitByProvider): URL {
  const toolkit = toolkitByProvider[provider];
  const url = new URL(
    toolkit === "tesla-energy"
      ? "https://auth.tesla.com/oauth2/v3/authorize"
      : "https://api.enphaseenergy.com/oauth/authorize",
  );
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", `energy-agent-${provider}-client`);
  url.searchParams.set("redirect_uri", callback);
  url.searchParams.set("state", state);
  if (toolkit === "tesla-energy") {
    url.searchParams.set("scope", "openid offline_access energy_device_data");
  }
  return url;
}

function response(toolkit: CloudToolkit, url = authorizationUrl(
  toolkit === "tesla-energy" ? "tesla" : "enphase",
).toString(), authorizationState = state): WorkspaceAuthorizationResponse["authorization"] {
  return {
    connection_id: "managed-energy-connection",
    authorization_url: url,
    state: authorizationState,
    expires_at: "2030-10-08T12:00:00Z",
  };
}

test("only approved Tesla and Enphase authorization URLs pass with the configured callback and state", () => {
  for (const provider of ["tesla", "enphase"] as const) {
    const toolkit = toolkitByProvider[provider];
    const candidate = authorizationUrl(provider);
    const approved = approvedCloudAuthorizationUrl(response(toolkit, candidate.toString()), toolkit, webOrigin);

    assert.ok(approved);
    assert.equal(approved.origin, candidate.origin);
    assert.equal(approved.pathname, candidate.pathname);
    assert.equal(approved.searchParams.get("redirect_uri"), callback);
    assert.equal(approved.searchParams.get("state"), state);
    assert.equal(approved.searchParams.get("response_type"), "code");
    if (provider === "tesla") {
      assert.equal(approved.searchParams.get("scope"), "openid offline_access energy_device_data");
    } else {
      assert.equal(approved.searchParams.has("scope"), false);
    }
  }
});

test("rejects arbitrary origins, paths, callback targets, and mismatched state or scopes", () => {
  const tesla = authorizationUrl("tesla");
  const enphase = authorizationUrl("enphase");
  const cases: Array<[CloudToolkit, string, string?]> = [
    ["tesla-energy", "https://attacker.example/oauth2/v3/authorize"],
    ["tesla-energy", "https://auth.tesla.com.attacker.example/oauth2/v3/authorize"],
    ["tesla-energy", "http://auth.tesla.com/oauth2/v3/authorize"],
    ["tesla-energy", "https://auth.tesla.com/other/path"],
    ["enphase-energy", tesla.toString()],
    ["tesla-energy", enphase.toString()],
    ["tesla-energy", (() => { const url = new URL(tesla); url.searchParams.set("redirect_uri", "https://attacker.example/callback"); return url.toString(); })()],
    ["tesla-energy", tesla.toString(), "different-state"],
    ["tesla-energy", (() => { const url = new URL(tesla); url.searchParams.set("scope", "openid offline_access"); return url.toString(); })()],
  ];

  for (const [toolkit, url, responseState] of cases) {
    assert.equal(approvedCloudAuthorizationUrl(response(toolkit, url, responseState), toolkit, webOrigin), null);
  }
});

test("rejects duplicate or extra query parameters, URL credentials, and fragments", () => {
  const tesla = authorizationUrl("tesla");
  const candidates = [
    (() => { const url = new URL(tesla); url.searchParams.append("state", state); return url.toString(); })(),
    (() => { const url = new URL(tesla); url.searchParams.append("client_id", "another-client"); return url.toString(); })(),
    (() => { const url = new URL(tesla); url.searchParams.set("prompt", "consent"); return url.toString(); })(),
    (() => { const url = new URL(tesla); url.searchParams.set("client_secret", "private-client-secret"); return url.toString(); })(),
    (() => { const url = new URL(tesla); url.username = "user"; url.password = "password"; return url.toString(); })(),
    (() => { const url = new URL(tesla); url.hash = "credential-fragment"; return url.toString(); })(),
  ];

  for (const url of candidates) {
    assert.equal(approvedCloudAuthorizationUrl(response("tesla-energy", url), "tesla-energy", webOrigin), null);
  }
});

test("production web routes complete both cloud OAuth providers and keep callback scope bound", { timeout: 180_000 }, async () => {
  const gatewayPort = await freePort();
  const webPort = await freePort();
  const gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
  const webUrl = `http://127.0.0.1:${webPort}`;
  const stateDir = await mkdtemp(join(tmpdir(), "energy-cloud-oauth-web-"));
  const python = process.env.ENERGY_WEB_TEST_PYTHON ?? join(projectRoot, ".venv/bin/python");
  const gateway = spawn(
    python,
    [
      join(projectRoot, "scripts/test_cloud_oauth_web_host.py"),
      "--port", String(gatewayPort),
      "--state-dir", stateDir,
      "--web-origin", "https://energy.example",
    ],
    {
      cwd: projectRoot,
      env: {
        ...process.env,
        CLOUD_OAUTH_TEST_TESLA_CLIENT_SECRET: teslaClientSecret,
        CLOUD_OAUTH_TEST_ENPHASE_CLIENT_SECRET: enphaseClientSecret,
        CLOUD_OAUTH_TEST_ENPHASE_API_KEY: enphaseApiKey,
        PYTHONPATH: [join(projectRoot, "src"), process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      },
      stdio: "ignore",
    },
  );
  let proxy: Awaited<ReturnType<typeof startGatewayProxy>> | null = null;
  let web: ChildProcess | null = null;
  let session: Awaited<ReturnType<EnergyAgentTools["createSession"]>> | null = null;

  async function postForm(
    baseUrl: string,
    path: string,
    fields: Record<string, string>,
    cookie?: string,
  ): Promise<Response> {
    const headers: Record<string, string> = {
      origin: "https://energy.example",
      "content-type": "application/x-www-form-urlencoded",
    };
    if (cookie !== undefined) headers.cookie = cookie;
    return fetch(`${baseUrl}${path}`, {
      method: "POST",
      headers,
      body: new URLSearchParams(fields),
      redirect: "manual",
    });
  }

  async function callback(
    stateValue: string,
    cookie: string,
    options: { code?: string; error?: string } = {},
  ): Promise<Response> {
    const query = new URLSearchParams({ state: stateValue });
    if (options.code !== undefined) query.set("code", options.code);
    if (options.error !== undefined) query.set("error", options.error);
    return fetch(`${webUrl}/api/workspace/oauth/callback?${query.toString()}`, {
      headers: { cookie },
      redirect: "manual",
    });
  }

  try {
    await waitUntilReady(`${gatewayUrl}/me`, gateway);
    const gatewayProxy = await startGatewayProxy(gatewayUrl);
    proxy = gatewayProxy;
    const webEnvironment: NodeJS.ProcessEnv = {
      ...process.env,
      NODE_ENV: "production",
      ENERGY_GATEWAY_URL: gatewayProxy.baseUrl,
      ENERGY_WEB_ORIGIN: "https://energy.example",
      ENERGY_PUBLIC_GATEWAY_URL: gatewayUrl,
      ENERGY_WEB_SESSION_KEY: randomBytes(32).toString("hex"),
    };
    delete webEnvironment.CLOUD_OAUTH_TEST_TESLA_CLIENT_SECRET;
    delete webEnvironment.CLOUD_OAUTH_TEST_ENPHASE_CLIENT_SECRET;
    delete webEnvironment.CLOUD_OAUTH_TEST_ENPHASE_API_KEY;
    const webProcess = spawn(
      process.execPath,
      [join(webRoot, "node_modules/next/dist/bin/next"), "start", "--hostname", "127.0.0.1", "--port", String(webPort)],
      { cwd: webRoot, env: webEnvironment, stdio: "ignore" },
    );
    web = webProcess;
    await waitUntilReady(webUrl, webProcess);

    const managerToken = await readFile(join(stateDir, "manager.token"), "ascii");
    const foreignManagerToken = await readFile(join(stateDir, "foreign-manager.token"), "ascii");
    const foreignSiteId = await readFile(join(stateDir, "foreign-site.id"), "ascii");
    const manager = new EnergyAgentTools({ baseUrl: gatewayUrl, token: managerToken });
    const workspace = manager.workspace();
    const identity = await manager.identity();
    assert.equal(identity.can_manage_workspace, true);
    assert.equal(identity.workspace?.mode, "managed");
    assert.deepEqual(identity.sites, []);
    const configurations = await workspace.authConfigurations();
    assert.deepEqual(
      configurations.configurations.map((item) => [item.id, item.toolkit, item.protocol]),
      [
        ["tesla-home", "tesla-energy", "oauth2_confidential"],
        ["enphase-home", "enphase-energy", "oauth2_confidential"],
      ],
    );
    assertNoSecrets(
      JSON.stringify(configurations),
      teslaClientSecret,
      enphaseClientSecret,
      enphaseApiKey,
    );

    const login = await postForm(webUrl, "/api/auth", { token: managerToken });
    assert.equal(login.status, 303);
    const loginCookie = responseCookie(login, "energy_web_session");
    if (loginCookie === null) assert.fail("The manager login did not set a session cookie.");
    const managerCookie = loginCookie;
    const loginCookieHeader = login.headers.get("set-cookie") ?? "";
    assert.match(loginCookieHeader, /HttpOnly/);
    assert.match(loginCookieHeader, /SameSite=Lax/);
    assert.match(loginCookieHeader, /Secure/);
    assertNoSecrets(loginCookieHeader, managerToken, teslaClientSecret, enphaseClientSecret, enphaseApiKey);

    const badTarget = await postForm(
      webUrl,
      "/api/workspace/provider-authorizations",
      { configuration_id: "attacker-target", resource_id: "1234567" },
      managerCookie,
    );
    assert.equal(badTarget.status, 502, await badTarget.clone().text());
    assert.equal(gatewayProxy.badTargetRequests(), 1);
    assert.equal(responseCookie(badTarget, "energy_web_oauth_flow"), null);
    const badTargetText = await badTarget.text();
    assertNoSecrets(badTargetText, "attacker.example", teslaClientSecret, enphaseClientSecret, enphaseApiKey);

    async function beginAuthorization(provider: CloudProvider, resourceId: string) {
      const configurationId = provider === "tesla" ? "tesla-home" : "enphase-home";
      const started = await postForm(
        webUrl,
        "/api/workspace/provider-authorizations",
        { configuration_id: configurationId, resource_id: resourceId },
        managerCookie,
      );
      assert.equal(started.status, 200, await started.clone().text());
      const startedBody: unknown = await started.json();
      if (!isRecord(startedBody) || startedBody.ok !== true || typeof startedBody.authorization_url !== "string") {
        assert.fail("The provider authorization route returned an invalid response.");
      }
      const target = new URL(startedBody.authorization_url);
      const expected = provider === "tesla"
        ? { origin: "https://auth.tesla.com", pathname: "/oauth2/v3/authorize" }
        : { origin: "https://api.enphaseenergy.com", pathname: "/oauth/authorize" };
      assert.equal(target.origin, expected.origin);
      assert.equal(target.pathname, expected.pathname);
      assert.equal(target.searchParams.get("redirect_uri"), "https://energy.example/api/workspace/oauth/callback");
      assert.equal(target.searchParams.get("response_type"), "code");
      assert.ok(target.searchParams.get("state"));
      assert.equal(target.searchParams.getAll("state").length, 1);
      if (provider === "tesla") {
        assert.equal(target.searchParams.get("scope"), "openid offline_access energy_device_data");
      } else {
        assert.equal(target.searchParams.has("scope"), false);
      }
      const flowCookie = responseCookie(started, "energy_web_oauth_flow");
      assert.ok(flowCookie);
      const startCookieHeader = started.headers.get("set-cookie") ?? "";
      assert.match(startCookieHeader, /Path=\/api\/workspace\/oauth\/callback/);
      assert.match(startCookieHeader, /HttpOnly/);
      assert.match(startCookieHeader, /SameSite=Lax/);
      assert.match(startCookieHeader, /Secure/);
      assert.ok(!flowCookie.includes(target.searchParams.get("state") ?? ""));
      assertNoSecrets(
        JSON.stringify(startedBody),
        teslaClientSecret,
        enphaseClientSecret,
        enphaseApiKey,
        ...providerAccessTokens,
        ...providerRefreshTokens,
      );
      return {
        state: target.searchParams.get("state") ?? "",
        cookie: `${managerCookie}; ${flowCookie}`,
        flowCookie,
      };
    }

    const cancellation = await beginAuthorization("tesla", "1234567891");
    const cancelled = await callback(cancellation.state, cancellation.cookie, { error: "access_denied" });
    assert.equal(cancelled.status, 303);
    assert.match(cancelled.headers.get("location") ?? "", /oauth=cancelled/);
    assert.match(cancelled.headers.get("set-cookie") ?? "", /energy_web_oauth_flow=;/);
    let observations = await readProviderObservations(join(stateDir, "observations.json"));
    assert.deepEqual(observations.token_exchanges, { tesla: 0, enphase: 0 });
    assert.deepEqual(observations.provider_reads, { tesla: 0, enphase: 0 });

    const teslaStart = await beginAuthorization("tesla", "1234567890");
    const foreignLogin = await postForm(webUrl, "/api/auth", { token: foreignManagerToken });
    assert.equal(foreignLogin.status, 303);
    const foreignSessionCookie = responseCookie(foreignLogin, "energy_web_session");
    if (foreignSessionCookie === null) assert.fail("The foreign manager login did not set a session cookie.");
    const foreignCookie = foreignSessionCookie;
    const crossSession = await callback(
      teslaStart.state,
      `${foreignCookie}; ${teslaStart.flowCookie}`,
      { code: "synthetic-tesla-authorization-code" },
    );
    assert.equal(crossSession.status, 303);
    assert.match(crossSession.headers.get("location") ?? "", /oauth=invalid/);
    const wrongState = await callback(
      `${teslaStart.state}-wrong`,
      teslaStart.cookie,
      { code: "synthetic-tesla-authorization-code" },
    );
    assert.equal(wrongState.status, 303);
    assert.match(wrongState.headers.get("location") ?? "", /oauth=invalid/);
    observations = await readProviderObservations(join(stateDir, "observations.json"));
    assert.deepEqual(observations.token_exchanges, { tesla: 0, enphase: 0 });
    assert.deepEqual(observations.provider_reads, { tesla: 0, enphase: 0 });

    const teslaCompleted = await callback(
      teslaStart.state,
      teslaStart.cookie,
      { code: "synthetic-tesla-authorization-code" },
    );
    assert.equal(teslaCompleted.status, 303);
    assert.equal(teslaCompleted.headers.get("location"), "/?view=connections&oauth=provider_connected");
    assert.match(teslaCompleted.headers.get("set-cookie") ?? "", /energy_web_oauth_flow=;/);
    assert.ok(!(teslaCompleted.headers.get("location") ?? "").includes(teslaStart.state));
    assert.ok(!(teslaCompleted.headers.get("location") ?? "").includes("synthetic-tesla-authorization-code"));

    const replay = await callback(
      teslaStart.state,
      teslaStart.cookie,
      { code: "synthetic-tesla-authorization-code" },
    );
    assert.equal(replay.status, 303);
    assert.match(replay.headers.get("location") ?? "", /oauth=(?:failed|invalid)/);
    observations = await readProviderObservations(join(stateDir, "observations.json"));
    assert.deepEqual(observations.token_exchanges, { tesla: 1, enphase: 0 });
    assert.deepEqual(observations.provider_reads, { tesla: 1, enphase: 0 });

    const enphaseStart = await beginAuthorization("enphase", "698910067");
    const enphaseCompleted = await callback(
      enphaseStart.state,
      enphaseStart.cookie,
      { code: "synthetic-enphase-authorization-code" },
    );
    assert.equal(enphaseCompleted.status, 303);
    assert.equal(enphaseCompleted.headers.get("location"), "/?view=connections&oauth=provider_connected");
    const pending = (await workspace.connections()).connections;
    assert.equal(pending.length, 2);
    for (const account of pending) {
      assert.equal(account.site_id, null);
      assert.equal(account.enabled, false);
      assert.equal(account.state, "pending_mapping");
    }
    assert.deepEqual((await workspace.sites()).sites, []);

    const teslaAccount = pending.find((account) => account.toolkit === "tesla-energy");
    const enphaseAccount = pending.find((account) => account.toolkit === "enphase-energy");
    assert.ok(teslaAccount);
    assert.ok(enphaseAccount);
    const foreignMapping = await postForm(
      webUrl,
      "/api/workspace/connections/action",
      { action: "map", connection_id: teslaAccount.id, site_id: foreignSiteId },
      managerCookie,
    );
    assert.equal(foreignMapping.status, 403);
    observations = await readProviderObservations(join(stateDir, "observations.json"));
    assert.deepEqual(observations.provider_reads, { tesla: 1, enphase: 1 });

    const siteResponse = await postForm(
      webUrl,
      "/api/workspace/sites",
      { name: "Owner cloud energy home", timezone: "Europe/London" },
      managerCookie,
    );
    assert.equal(siteResponse.status, 201, await siteResponse.clone().text());
    const sitePayload: unknown = await siteResponse.json();
    if (!isRecord(sitePayload) || !isRecord(sitePayload.site) || typeof sitePayload.site.id !== "string") {
      assert.fail("The site route did not return the created site ID.");
    }
    const siteId = sitePayload.site.id;

    for (const account of [teslaAccount, enphaseAccount]) {
      const mapped = await postForm(
        webUrl,
        "/api/workspace/connections/action",
        { action: "map", connection_id: account.id, site_id: siteId },
        managerCookie,
      );
      assert.equal(mapped.status, 200, await mapped.clone().text());
    }
    const active = (await workspace.connections()).connections;
    assert.equal(active.length, 2);
    assert.ok(active.every((account) => account.state === "active" && account.site_id === siteId));

    const keyResponse = await postForm(
      webUrl,
      "/api/workspace/keys",
      { name: "Cloud energy agent", site_id: siteId },
      managerCookie,
    );
    assert.equal(keyResponse.status, 201, await keyResponse.clone().text());
    const keyPayload: unknown = await keyResponse.json();
    if (!isRecord(keyPayload) || typeof keyPayload.token !== "string") {
      assert.fail("The key route did not return its one-time agent token.");
    }
    const agentToken = keyPayload.token;
    assert.notEqual(agentToken, managerToken);
    const agent = new EnergyAgentTools({ baseUrl: gatewayUrl, token: agentToken });
    const agentSession = await agent.createSession({ site_id: siteId });
    session = agentSession;
    for (const [account, provider] of [[teslaAccount, "tesla"], [enphaseAccount, "enphase"]] as const) {
      const execution = await agentSession.execute({
        tool: provider === "tesla" ? "tesla_energy.get_site_info" : "enphase_energy.get_summary",
        arguments: {},
        account_id: account.id,
      });
      if (!execution.ok) assert.fail(`The scoped ${provider} execution failed: ${execution.error.message}`);
      assert.equal(execution.result.site_id, siteId);
    }
    await agentSession.close();
    session = null;

    observations = await readProviderObservations(join(stateDir, "observations.json"));
    assert.deepEqual(observations.token_exchanges, { tesla: 1, enphase: 1 });
    assert.deepEqual(observations.provider_reads, { tesla: 3, enphase: 3 });
    assert.ok(observations.resources.some((item) => item.provider === "tesla" && item.resource_id === "1234567890"));
    assert.ok(observations.resources.some((item) => item.provider === "enphase" && item.resource_id === "698910067"));
    assert.ok(observations.resources.every((item) => /^\d{1,32}$/.test(item.resource_id)));
    const observationsText = JSON.stringify(observations);
    assertNoSecrets(
      observationsText,
      teslaClientSecret,
      enphaseClientSecret,
      enphaseApiKey,
      ...providerAccessTokens,
      ...providerRefreshTokens,
      managerToken,
      foreignManagerToken,
      "synthetic-tesla-authorization-code",
      "synthetic-enphase-authorization-code",
    );

    const dashboard = await fetch(`${webUrl}/?view=connections`, { headers: { cookie: managerCookie } });
    assert.equal(dashboard.status, 200);
    const dashboardHtml = await dashboard.text();
    assertNoSecrets(
      dashboardHtml,
      teslaClientSecret,
      enphaseClientSecret,
      enphaseApiKey,
      ...providerAccessTokens,
      ...providerRefreshTokens,
      managerToken,
      foreignManagerToken,
      "synthetic-tesla-authorization-code",
      "synthetic-enphase-authorization-code",
    );
  } finally {
    if (session) await session.close().catch(() => undefined);
    if (web) await stop(web);
    if (proxy) await proxy.close();
    await stop(gateway);
    await rm(stateDir, { recursive: true, force: true });
  }
});
