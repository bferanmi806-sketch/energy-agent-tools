import assert from "node:assert/strict";
import test from "node:test";
import { MAX_MCP_BODY_BYTES, readBoundedJson } from "../src/lib/security.js";

interface StreamObservation {
  pulls: number;
  cancelled: boolean;
}

function streamedRequest(
  chunks: readonly Uint8Array[],
  headers: Record<string, string> = { "content-type": "application/json" },
  observation: StreamObservation = { pulls: 0, cancelled: false },
): Request {
  let index = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      observation.pulls += 1;
      const chunk = chunks[index];
      index += 1;
      if (chunk === undefined) controller.close();
      else controller.enqueue(chunk);
    },
    cancel() {
      observation.cancelled = true;
    },
  }, { highWaterMark: 0 });

  const init: RequestInit & { duplex: "half" } = {
    method: "POST",
    headers,
    body,
    duplex: "half",
  };
  return new Request("https://energy.example/api/workspace/mcp/inspect", init);
}

function encode(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

test("readBoundedJson reads streamed bodies without trusting missing or understated lengths", async () => {
  const payload = encode('{"ok":true}');

  const absentLengthRequest = streamedRequest([payload.subarray(0, 4), payload.subarray(4)]);
  assert.equal(absentLengthRequest.headers.has("content-length"), false);
  assert.deepEqual(await readBoundedJson(absentLengthRequest), { ok: true });

  const understatedLengthRequest = streamedRequest([payload], {
    "content-type": "application/json",
    "content-length": "1",
  });
  assert.equal(understatedLengthRequest.headers.get("content-length"), "1");
  assert.deepEqual(await readBoundedJson(understatedLengthRequest), { ok: true });
});

test("readBoundedJson cancels streams that exceed the actual byte limit", async () => {
  for (const contentLength of [undefined, "1"]) {
    const observation: StreamObservation = { pulls: 0, cancelled: false };
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (contentLength !== undefined) headers["content-length"] = contentLength;
    const request = streamedRequest([
      new Uint8Array(MAX_MCP_BODY_BYTES),
      new Uint8Array([0x20]),
    ], headers, observation);

    assert.equal(await readBoundedJson(request), null);
    assert.equal(observation.cancelled, true);
  }
});

test("readBoundedJson rejects a declared oversized body before pulling its stream", async () => {
  const observation: StreamObservation = { pulls: 0, cancelled: false };
  const request = streamedRequest([encode("{}")], {
    "content-type": "application/json",
    "content-length": String(MAX_MCP_BODY_BYTES + 1),
  }, observation);

  assert.equal(await readBoundedJson(request), null);
  assert.equal(observation.pulls, 0);
});

test("readBoundedJson accepts valid JSON whose UTF-8 body is exactly at the byte limit", async () => {
  const body = encode(`{}${" ".repeat(MAX_MCP_BODY_BYTES - 2)}`);
  assert.equal(body.byteLength, MAX_MCP_BODY_BYTES);
  const request = streamedRequest([body.subarray(0, 65_536), body.subarray(65_536)]);

  assert.deepEqual(await readBoundedJson(request), {});
});

test("readBoundedJson refuses the wrong media type, encoded bodies, malformed UTF-8, and invalid JSON", async () => {
  assert.equal(await readBoundedJson(streamedRequest([encode("{}")], {
    "content-type": "text/plain",
  })), null);
  assert.equal(await readBoundedJson(streamedRequest([encode("{}")], {
    "content-type": "application/json",
    "content-encoding": "gzip",
  })), null);
  assert.equal(await readBoundedJson(streamedRequest([new Uint8Array([0x7b, 0x22, 0xff, 0x22, 0x7d])])), null);
  assert.equal(await readBoundedJson(streamedRequest([encode('{"unterminated":')])), null);
});

test("readBoundedJson does not reflect provider credentials from stream errors", async () => {
  const providerCredential = "provider-secret-that-must-not-escape";
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      controller.error(new Error(providerCredential));
    },
  }, { highWaterMark: 0 });
  const init: RequestInit & { duplex: "half" } = {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
    duplex: "half",
  };
  const request = new Request("https://energy.example/api/workspace/mcp/inspect", init);

  assert.equal(await readBoundedJson(request), null);
});
