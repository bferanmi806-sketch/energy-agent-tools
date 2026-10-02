import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import {
  EnergyHttpError,
  EnergyProtocolError,
  EnergyTransportError,
  HttpTransport,
} from "../dist/transport.js";

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.ok(address && typeof address === "object");
  return `http://127.0.0.1:${address.port}/api/v1`;
}

async function withServer(handler, run) {
  const server = createServer(handler);
  const baseUrl = await listen(server);
  try {
    return await run(baseUrl, server);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

async function readRequestBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString("utf8");
}

function sendJson(response, status, value) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(value));
}

test("sends bearer auth and JSON POST bodies beneath the configured base path", async () => {
  await withServer(async (request, response) => {
    const body = await readRequestBody(request);
    assert.equal(request.url, "/api/v1/sessions");
    assert.equal(request.headers.authorization, "Bearer loopback-token");
    assert.equal(request.headers["content-type"], "application/json");
    assert.equal(body, '{"label":"meter","enabled":true}');
    sendJson(response, 200, { created: true });
  }, async (baseUrl) => {
    const transport = new HttpTransport({ baseUrl, token: () => "loopback-token" });
    const result = await transport.request({
      path: "sessions",
      method: "POST",
      body: { label: "meter", enabled: true },
      parse: (value) => value,
    });
    assert.deepEqual(result, { created: true });
  });
});

test("resolves a fresh token for each request and parses known HTTP error fields", async () => {
  let calls = 0;
  await withServer(async (_request, response) => {
    calls += 1;
    if (calls === 1) {
      sendJson(response, 401, {
        error: { code: "unauthorized", message: "Token loopback-1-secret was rejected." },
        diagnostic: "must not be exposed",
      });
    } else {
      sendJson(response, 429, { error: { code: "rate_limited", message: "Try again later." } });
    }
  }, async (baseUrl) => {
    let tokenNumber = 0;
    const transport = new HttpTransport({
      baseUrl,
      token: () => `loopback-${++tokenNumber}${tokenNumber === 1 ? "-secret" : ""}`,
    });

    await assert.rejects(
      transport.request({ path: "first", method: "GET", parse: (value) => value }),
      (error) => {
        assert.ok(error instanceof EnergyHttpError);
        assert.equal(error.status, 401);
        assert.equal(error.code, "unauthorized");
        assert.equal(error.retryable, false);
        assert.match(error.message, /\[redacted\]/);
        assert.doesNotMatch(String(error), /loopback-1-secret/);
        assert.doesNotMatch(JSON.stringify(error), /loopback-1-secret/);
        assert.doesNotMatch(error.message, /diagnostic/);
        return true;
      },
    );

    await assert.rejects(
      transport.request({ path: "second", method: "GET", parse: (value) => value }),
      (error) => {
        assert.ok(error instanceof EnergyHttpError);
        assert.equal(error.status, 429);
        assert.equal(error.retryable, true);
        return true;
      },
    );
    assert.equal(calls, 2);
  });
});

test("rejects malformed JSON, invalid UTF-8, oversized bodies, and invalid parsed values", async () => {
  await withServer(async (request, response) => {
    if (request.url?.endsWith("/malformed")) {
      response.writeHead(200, { "content-type": "application/json" });
      response.end("{");
      return;
    }
    if (request.url?.endsWith("/invalid-utf8")) {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(Buffer.from([0xff, 0xfe]));
      return;
    }
    if (request.url?.endsWith("/large")) {
      response.writeHead(200, { "content-type": "application/json" });
      response.write('{"value":"');
      response.end("abcdefghijk" + '"}');
      return;
    }
    sendJson(response, 200, { value: 1 });
  }, async (baseUrl) => {
    const transport = new HttpTransport({ baseUrl, token: "test-token", maxResponseBytes: 12 });
    await assert.rejects(
      transport.request({ path: "malformed", method: "GET", parse: (value) => value }),
      EnergyProtocolError,
    );
    await assert.rejects(
      transport.request({ path: "invalid-utf8", method: "GET", parse: (value) => value }),
      EnergyProtocolError,
    );
    await assert.rejects(
      transport.request({ path: "large", method: "GET", parse: (value) => value }),
      (error) => error instanceof EnergyProtocolError && /size limit/.test(error.message),
    );

    await assert.rejects(
      transport.request({
        path: "parse-failure",
        method: "GET",
        parse: () => { throw new Error("parser included test-token"); },
      }),
      (error) => {
        assert.ok(error instanceof EnergyProtocolError);
        assert.doesNotMatch(String(error), /test-token/);
        return true;
      },
    );
  });
});

test("rejects non-JSON-safe request bodies before sending them", async () => {
  let requests = 0;
  await withServer((_request, response) => {
    requests += 1;
    sendJson(response, 200, { ok: true });
  }, async (baseUrl) => {
    const transport = new HttpTransport({ baseUrl, token: "test-token" });
    const cyclic = {};
    cyclic.self = cyclic;
    const invalidBodies = [
      undefined,
      Number.NaN,
      Number.POSITIVE_INFINITY,
      () => "not JSON",
      cyclic,
      { hidden: undefined },
      [undefined],
      new Array(1),
    ];

    for (const body of invalidBodies) {
      await assert.rejects(
        transport.request({ path: "write", method: "POST", body, parse: (value) => value }),
        TypeError,
      );
    }
    await assert.rejects(
      transport.request({ path: "/outside-prefix", method: "GET", parse: (value) => value }),
      TypeError,
    );
    assert.equal(requests, 0);
  });
});

test("times out requests and honors caller cancellation with safe diagnostics", async () => {
  await withServer((request, response) => {
    request.on("close", () => undefined);
    setTimeout(() => {
      if (!response.destroyed) sendJson(response, 200, { ok: true });
    }, 250);
  }, async (baseUrl) => {
    const transport = new HttpTransport({ baseUrl, token: "private-token", timeoutMs: 30 });
    await assert.rejects(
      transport.request({ path: "slow", method: "GET", parse: (value) => value }),
      (error) => {
        assert.ok(error instanceof EnergyTransportError);
        assert.equal(error.code, "timeout");
        assert.doesNotMatch(String(error), /private-token/);
        return true;
      },
    );

    let startedResolve;
    const started = new Promise((resolve) => { startedResolve = resolve; });
    const cancellationServer = createServer((request, response) => {
      startedResolve();
      setTimeout(() => {
        if (!response.destroyed) sendJson(response, 200, { ok: true });
      }, 250);
    });
    const cancellationBase = await listen(cancellationServer);
    try {
      const controller = new AbortController();
      const pending = new HttpTransport({
        baseUrl: cancellationBase,
        token: "private-token",
        timeoutMs: 2_000,
      }).request({ path: "slow", method: "GET", parse: (value) => value, signal: controller.signal });
      await started;
      controller.abort();
      await assert.rejects(pending, (error) => {
        assert.ok(error instanceof EnergyTransportError);
        assert.equal(error.code, "cancelled");
        assert.doesNotMatch(String(error), /private-token/);
        return true;
      });
    } finally {
      cancellationServer.closeAllConnections();
      await new Promise((resolve) => cancellationServer.close(resolve));
    }
  });
});

test("does not start a request when token resolution finishes after cancellation", async () => {
  let resolveToken;
  const tokenPromise = new Promise((resolve) => { resolveToken = resolve; });
  let fetchCalls = 0;

  await withServer((_request, response) => {
    sendJson(response, 200, { unexpected: true });
  }, async (baseUrl) => {
    const transport = new HttpTransport({
      baseUrl,
      token: () => tokenPromise,
      fetch: (...args) => {
        fetchCalls += 1;
        return globalThis.fetch(...args);
      },
      timeoutMs: 2_000,
    });
    const controller = new AbortController();
    const pending = transport.request({
      path: "after-cancel",
      method: "POST",
      body: { effect: true },
      parse: (value) => value,
      signal: controller.signal,
    });

    controller.abort();
    await assert.rejects(pending, (error) => {
      assert.ok(error instanceof EnergyTransportError);
      assert.equal(error.code, "cancelled");
      return true;
    });

    resolveToken("late-token");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(fetchCalls, 0);
  });
});

test("refuses redirects and never retries a POST", async () => {
  let requests = 0;
  let redirectedRequests = 0;
  await withServer((request, response) => {
    requests += 1;
    if (request.url?.endsWith("/redirect")) {
      response.writeHead(302, { location: "/api/v1/target" });
      response.end();
      return;
    }
    if (request.url?.endsWith("/target")) redirectedRequests += 1;
    sendJson(response, 503, { error: { code: "unavailable", message: "Try later." } });
  }, async (baseUrl) => {
    const transport = new HttpTransport({ baseUrl, token: "private-token" });
    await assert.rejects(
      transport.request({ path: "redirect", method: "GET", parse: (value) => value }),
      (error) => error instanceof EnergyTransportError && error.code === "network_error",
    );
    await assert.rejects(
      transport.request({
        path: "write",
        method: "POST",
        body: { value: 1 },
        parse: (value) => value,
      }),
      (error) => error instanceof EnergyHttpError && error.status === 503,
    );
    assert.equal(requests, 2);
    assert.equal(redirectedRequests, 0);
  });
});
