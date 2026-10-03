import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:net";
import { once } from "node:events";
import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { EnergyAgentTools } from "@energy-agent-tools/sdk";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const app = join(root, "apps/web");
async function port(): Promise<number> {
  const server = createServer(); server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address(); assert.ok(address && typeof address !== "string");
  const value = address.port;
  await new Promise<void>(resolve => server.close(() => resolve()));
  return value;
}
async function stop(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const done = once(child, "exit"); child.kill("SIGTERM"); await done;
}
async function ready(url: string, child: ChildProcess): Promise<void> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error("Acceptance service exited.");
    try { await fetch(url, { signal: AbortSignal.timeout(1_000) }); return; } catch { }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("Acceptance service startup timed out.");
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

test("production web routes authenticate the real gateway and keep keys out of rendered props", { timeout: 120_000 }, async () => {
  const gatewayPort = await port(); const webPort = await port();
  const gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
  const webUrl = `http://127.0.0.1:${webPort}`;
  const state = await mkdtemp(join(tmpdir(), "energy-web-"));
  const token = randomBytes(32).toString("hex");
  const providerKey = randomBytes(32).toString("hex");
  const gateway = spawn(process.env.ENERGY_WEB_TEST_PYTHON ?? join(root, ".venv/bin/python"), [join(root, "scripts/web_reference_host.py"), "--port", String(gatewayPort), "--state-dir", state, "--octopus-fixture"], { cwd:root, env:{...process.env, ENERGY_WEB_TEST_TOKEN:token, ENERGY_WEB_TEST_OCTOPUS_KEY:providerKey}, stdio:"ignore" });
  const web = spawn(process.execPath, [join(app, "node_modules/next/dist/bin/next"), "start", "--hostname", "127.0.0.1", "--port", String(webPort)], { cwd:app, env:{...process.env, NODE_ENV:"production", ENERGY_GATEWAY_URL:gatewayUrl, ENERGY_WEB_ORIGIN:webUrl, ENERGY_PUBLIC_GATEWAY_URL:gatewayUrl, ENERGY_WEB_SESSION_KEY:randomBytes(32).toString("hex")}, stdio:"ignore" });
  try {
    await Promise.all([ready(`${gatewayUrl}/me`, gateway), ready(webUrl, web)]);
    const landing = await fetch(webUrl); assert.equal(landing.status, 200);
    assert.match(await landing.text(), /Gateway access key/);
    async function post(path:string, fields:Record<string,string>, cookie?:string, origin:string | null=webUrl) {
      const headers:Record<string,string>={"content-type":"application/x-www-form-urlencoded"};
      if(origin!==null) headers.origin=origin;
      if(cookie!==undefined) headers.cookie=cookie;
      return fetch(webUrl+path,{method:"POST",headers,body:new URLSearchParams(fields),redirect:"manual"});
    }
    assert.equal((await post("/api/auth",{token},undefined,null)).status,403);
    assert.equal((await post("/api/auth",{token},undefined,"https://foreign.invalid")).status,403);
    const invalid=await post("/api/auth",{token:"wrong-fixture-key"});
    assert.equal(invalid.status,303); assert.match(invalid.headers.get("location") ?? "",/invalid_token/);
    const login=await post("/api/auth",{token}); assert.equal(login.status,303);
    const sealed=login.headers.get("set-cookie"); assert.ok(sealed);
    assert.match(sealed,/HttpOnly/); assert.match(sealed,/SameSite=Lax/); assert.ok(!sealed.includes(token));
    const cookie=sealed.split(";",1)[0]; assert.ok(cookie);
    const sdk = new EnergyAgentTools({baseUrl:gatewayUrl,token});
    const identity=await sdk.identity();
    assert.equal(identity.sites.length,2);
    const dashboard=await fetch(webUrl,{headers:{cookie}}); assert.equal(dashboard.status,200);
    const html=await dashboard.text(); assert.match(html,/Connect apps/); assert.match(html,/Synthetic home/);
    const session=await sdk.createSession({site_id:"synthetic-home"});
    const catalogue=await session.toolkits();
    const setups=await session.connectionSetups();
    assert.equal(setups.setups[0]?.enabled,true);
    const fields={provider:"octopus",credential:providerKey,mpan:"1234567890123",serial_number:"TEST123"};
    assert.equal((await post("/api/connections",fields,cookie,null)).status,403);
    assert.equal((await post("/api/connections",fields,cookie,"https://foreign.invalid")).status,403);
    assert.equal((await post("/api/connections",fields)).status,401);
    assert.equal((await post("/api/connections",{...fields,base_url:"http://127.0.0.1"},cookie)).status,400);
    const duplicate=await fetch(webUrl+"/api/connections",{method:"POST",headers:{origin:webUrl,cookie,"content-type":"application/x-www-form-urlencoded"},body:new URLSearchParams(fields).toString()+"&credential=duplicate"});
    assert.equal(duplicate.status,400);
    const oversized=await post("/api/connections",{...fields,credential:"x".repeat(17_000)},cookie);
    assert.equal(oversized.status,400);
    const refused=await post("/api/connections",{...fields,credential:"wrong-fictional-provider-key"},cookie);
    assert.equal(refused.status,422);
    assert.deepEqual((await session.connections()).connections,[]);
    const connected=await post("/api/connections",fields,cookie);
    assert.equal(connected.status,201); assert.deepEqual(await connected.json(),{ok:true});
    assert.equal((await post("/api/connections",fields,cookie)).status,201);
    const accounts=await session.connections();
    assert.equal(accounts.connections.length,1);
    const execution=await session.capability({capability:"get_energy_consumption"});
    assert.equal(execution.ok,true);
    const connectedHtml=await fetch(webUrl+"/?view=connections",{headers:{cookie}}).then(r=>r.text());
    assert.match(connectedHtml,/Octopus/); assert.match(connectedHtml,/Verified record/);
    assert.ok(!connectedHtml.includes(providerKey)); assert.ok(!connectedHtml.includes(token));
    await session.close();
    assert.ok(catalogue.toolkits.length>10);
    assert.ok(catalogue.toolkits.some(toolkit=>html.includes(toolkit.name)));
    assert.ok(!html.includes(token)); assert.ok(!html.includes(sealed));
    const changed=await post("/api/site",{site_id:"synthetic-workshop"},cookie); assert.equal(changed.status,303);
    const replacement=changed.headers.get("set-cookie")?.split(";",1)[0]; assert.ok(replacement);
    const changedHtml=await fetch(webUrl,{headers:{cookie:replacement}}).then(r=>r.text());
    const workshopSession=await sdk.createSession({site_id:"synthetic-workshop"});
    assert.deepEqual((await workshopSession.connections()).connections,[]);
    await workshopSession.close();
    assert.match(changedHtml,/Synthetic workshop/); assert.ok(!changedHtml.includes(token));
    const foreignSite=await post("/api/site",{site_id:"foreign-site"},replacement);
    assert.equal(foreignSite.status,303);
    assert.match(foreignSite.headers.get("location") ?? "",/invalid_site/);
    assert.equal(foreignSite.headers.get("set-cookie"),null);
    const accountId=accounts.connections[0]?.id; assert.ok(accountId);
    const action={connection_id:accountId,action:"verify"};
    assert.equal((await post("/api/connections/action",action,cookie,"https://foreign.invalid")).status,403);
    assert.equal((await post("/api/connections/action",action,replacement)).status,422);
    assert.equal((await post("/api/connections/action",{...action,credential:providerKey},cookie)).status,400);
    const verified=await post("/api/connections/action",action,cookie); assert.equal(verified.status,200);
    const health=await verified.json(); assert.equal(health.status,"healthy"); assert.equal(health.action,"verify");
    const ongoing=await sdk.createSession({site_id:"synthetic-home"});
    const removed=await post("/api/connections/action",{...action,action:"disconnect"},cookie);
    assert.equal(removed.status,200); assert.deepEqual(await removed.json(),{ok:true,action:"disconnect"});
    const after=await ongoing.connections(); assert.equal(after.connections[0]?.state,"revoked");
    const deniedExecution=await ongoing.capability({capability:"get_energy_consumption"}); assert.equal(deniedExecution.ok,false);
    assert.equal((await post("/api/connections/action",action,cookie)).status,422);
    await ongoing.close();
    const tampered=await fetch(webUrl,{headers:{cookie:replacement+"tampered"}}).then(r=>r.text());
    assert.match(tampered,/Gateway access key/);
    assert.equal((await post("/api/logout",{},replacement,"https://foreign.invalid")).status,403);
    const logout=await post("/api/logout",{},replacement); assert.equal(logout.status,303);
    assert.match(logout.headers.get("set-cookie") ?? "",/Max-Age=0/);
  } finally {
    await Promise.all([stop(web),stop(gateway)]); await rm(state,{recursive:true,force:true});
  }
});

test("production web keeps a home-only agent key read-only while allowing connection health checks", { timeout: 120_000 }, async () => {
  const gatewayPort = await port(); const webPort = await port();
  const gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
  const webUrl = `http://127.0.0.1:${webPort}`;
  const state = await mkdtemp(join(tmpdir(), "energy-web-agent-"));
  const token = randomBytes(32).toString("hex");
  const providerKey = randomBytes(32).toString("hex");
  const children: ChildProcess[] = [];
  let session: Awaited<ReturnType<EnergyAgentTools["createSession"]>> | null = null;
  const gateway = spawn(
    process.env.ENERGY_WEB_TEST_PYTHON ?? join(root, ".venv/bin/python"),
    [join(root, "scripts/web_reference_host.py"), "--port", String(gatewayPort), "--state-dir", state, "--agent-key"],
    { cwd:root, env:{...process.env, ENERGY_WEB_TEST_TOKEN:token, ENERGY_WEB_TEST_OCTOPUS_KEY:providerKey}, stdio:"ignore" },
  );
  children.push(gateway);
  const web = spawn(
    process.execPath,
    [join(app, "node_modules/next/dist/bin/next"), "start", "--hostname", "127.0.0.1", "--port", String(webPort)],
    {
      cwd:app,
      env:{...process.env, NODE_ENV:"production", ENERGY_GATEWAY_URL:gatewayUrl, ENERGY_WEB_ORIGIN:webUrl, ENERGY_PUBLIC_GATEWAY_URL:gatewayUrl, ENERGY_WEB_SESSION_KEY:randomBytes(32).toString("hex")},
      stdio:"ignore",
    },
  );
  children.push(web);

  async function post(path: string, fields: Record<string, string>, cookie?: string) {
    const headers: Record<string, string> = {
      origin: webUrl,
      "content-type": "application/x-www-form-urlencoded",
    };
    if (cookie !== undefined) headers.cookie = cookie;
    return fetch(webUrl + path, { method:"POST", headers, body:new URLSearchParams(fields), redirect:"manual" });
  }

  try {
    await Promise.all([ready(`${gatewayUrl}/me`, gateway), ready(webUrl, web)]);
    const login = await post("/api/auth", { token });
    assert.equal(login.status, 303);
    const sealed = login.headers.get("set-cookie"); assert.ok(sealed);
    const cookie = sealed.split(";", 1)[0]; assert.ok(cookie);

    const sdk = new EnergyAgentTools({ baseUrl:gatewayUrl, token });
    const identity = await sdk.identity();
    assert.deepEqual(identity.sites.map(site => site.id), ["synthetic-home"]);
    assert.equal(identity.can_manage_connections, false);
    session = await sdk.createSession({ site_id:"synthetic-home" });
    const setups = await session.connectionSetups();
    assert.equal(setups.setups[0]?.enabled, false);
    assert.equal(setups.setups[0]?.unavailable_reason, "management_key_required");
    const accounts = await session.connections();
    assert.equal(accounts.connections.length, 1);
    const account = accounts.connections[0]; assert.ok(account);
    assert.equal(account.state, "active");
    assert.equal(account.enabled, true);

    const page = await fetch(`${webUrl}/?view=connections`, { headers:{ cookie } });
    assert.equal(page.status, 200);
    const html = await page.text();
    assert.match(html, /Check connection/);
    assert.doesNotMatch(html, /aria-label="Disconnect connection"/);
    assert.doesNotMatch(html, /aria-label="Confirm disconnect"/);
    assert.ok(!html.includes("Synthetic workshop"));
    assert.ok(!html.includes(token));
    assert.ok(!html.includes(providerKey));

    const authorization = { authorization:`Bearer ${token}`, "content-type":"application/json" };
    const deniedConnect = await fetch(`${gatewayUrl}/sessions/${session.id}/connections`, {
      method:"POST",
      headers:authorization,
      body:JSON.stringify({ provider:"octopus", credential:providerKey, mpan:"1234567890123", serial_number:"TEST123" }),
    });
    assert.equal(deniedConnect.status, 403);
    const connectMessage = await deniedConnect.text();
    assert.match(connectMessage, /workspace management key to connect accounts/);
    assert.ok(!connectMessage.includes(providerKey));

    const deniedDisconnect = await fetch(`${gatewayUrl}/sessions/${session.id}/connections/${encodeURIComponent(account.id)}/disconnect`, {
      method:"POST",
      headers:authorization,
      body:"{}",
    });
    assert.equal(deniedDisconnect.status, 403);
    const disconnectMessage = await deniedDisconnect.text();
    assert.match(disconnectMessage, /workspace management key to disconnect accounts/);
    assert.ok(!disconnectMessage.includes(providerKey));

    const webDeniedConnect = await post(
      "/api/connections",
      { provider:"octopus", credential:providerKey, mpan:"1234567890123", serial_number:"TEST123" },
      cookie,
    );
    assert.equal(webDeniedConnect.status, 422);
    const webConnectMessage = await webDeniedConnect.text();
    assert.match(webConnectMessage, /connection_failed/);
    assert.ok(!webConnectMessage.includes(providerKey));

    const webDeniedDisconnect = await post(
      "/api/connections/action",
      { connection_id:account.id, action:"disconnect" },
      cookie,
    );
    assert.equal(webDeniedDisconnect.status, 422);
    const webDisconnectMessage = await webDeniedDisconnect.text();
    assert.match(webDisconnectMessage, /connection_action_failed/);
    assert.ok(!webDisconnectMessage.includes(providerKey));

    const unchanged = await session.connections();
    assert.equal(unchanged.connections.length, 1);
    assert.equal(unchanged.connections[0]?.state, "active");

    const verified = await post(
      "/api/connections/action",
      { connection_id:account.id, action:"verify" },
      cookie,
    );
    assert.equal(verified.status, 200);
    const health: unknown = await verified.json();
    if (!isRecord(health)) throw new Error("Health response is invalid.");
    assert.equal(health.action, "verify");
    assert.equal(health.status, "healthy");
    const afterVerification = await session.connections();
    assert.equal(afterVerification.connections[0]?.state, "active");
  } finally {
    if (session) await session.close().catch(() => undefined);
    await Promise.all(children.map(stop));
    await rm(state, { recursive:true, force:true });
  }
});
