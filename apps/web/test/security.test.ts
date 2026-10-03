import assert from "node:assert/strict";
import { createCipheriv, randomBytes } from "node:crypto";
import test from "node:test";
import {
  MAX_FORM_BODY_BYTES,
  MAX_LOGIN_TOKEN_CHARS,
  MAX_SESSION_COOKIE_BYTES,
  SESSION_TTL_MS,
  isAllowedMutationOrigin,
  openSession,
  parseLoginToken,
  parseUrlEncodedFormBody,
  resolveGatewayUrl,
  resolvePublicGatewayUrl,
  resolveSessionKey,
  resolveWebOrigin,
  sealSession,
  serializeSessionCookie,
} from "../src/lib/security.js";
import type { WebSession } from "../src/lib/security.js";

const key = "11".repeat(32);
const otherKey = "22".repeat(32);
const issuedAt = 1_000_000;
const session: WebSession = {
  version: 1,
  token: "raw-gateway-token-6ac8baf7",
  issuedAt,
  expiresAt: issuedAt + SESSION_TTL_MS,
  siteId: "household-west",
};

function encryptPayload(payload: unknown, keyHex: string): string {
  const nonce = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", Buffer.from(keyHex, "hex"), nonce);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(payload), "utf8"), cipher.final()]);
  return Buffer.concat([nonce, cipher.getAuthTag(), ciphertext]).toString("base64url");
}

test("AES-256-GCM session cookie round-trips without exposing the gateway token", () => {
  const encrypted = sealSession(session, key);
  assert.equal(encrypted.includes(session.token), false);
  assert.deepEqual(openSession(encrypted, key, issuedAt + 1), session);
});

test("tampered, wrong-key, malformed, expired, and unsupported-version cookies are refused", () => {
  const encrypted = sealSession(session, key);
  const middle = Math.floor(encrypted.length / 2);
  const changed = encrypted[middle] === "A" ? "B" : "A";
  const tampered = `${encrypted.slice(0, middle)}${changed}${encrypted.slice(middle + 1)}`;

  assert.equal(openSession(tampered, key, issuedAt + 1), null);
  assert.equal(openSession(encrypted, otherKey, issuedAt + 1), null);
  assert.equal(openSession(encrypted, key, session.expiresAt), null);
  assert.equal(openSession(undefined, key, issuedAt), null);
  assert.equal(openSession("not-a-session", key, issuedAt), null);

  const unsupported = encryptPayload({ ...session, version: 2 }, key);
  assert.equal(openSession(unsupported, key, issuedAt + 1), null);
});

test("mutation Origin must match the configured web origin", () => {
  const origin = resolveWebOrigin("https://energy.example");
  assert.equal(isAllowedMutationOrigin("https://energy.example", origin), true);
  assert.equal(isAllowedMutationOrigin("https://energy.example/", origin), true);
  assert.equal(isAllowedMutationOrigin(null, origin), false);
  assert.equal(isAllowedMutationOrigin("null", origin), false);
  assert.equal(isAllowedMutationOrigin("https://attacker.example", origin), false);
  assert.equal(isAllowedMutationOrigin("https://energy.example.evil.test", origin), false);
});

test("gateway, public URL, origin, and encryption-key restrictions are enforced", () => {
  assert.equal(resolveGatewayUrl("https://gateway.example/api", true), "https://gateway.example/api/");
  assert.equal(resolveGatewayUrl("http://127.0.0.1:8765", true), "http://127.0.0.1:8765/");
  assert.equal(resolveGatewayUrl("http://localhost:8765", true), "http://localhost:8765/");
  assert.throws(() => resolveGatewayUrl("http://gateway.example", true));
  assert.throws(() => resolveGatewayUrl("https://user:pass@gateway.example", false));
  assert.throws(() => resolveGatewayUrl("https://gateway.example?token=secret", false));
  assert.throws(() => resolveGatewayUrl("https://gateway.example/#fragment", false));

  assert.throws(() => resolveWebOrigin("http://energy.example", true));
  assert.equal(resolveWebOrigin("http://localhost:3000", true).secure, false);
  assert.equal(resolvePublicGatewayUrl("https://mcp.example/operator/prefix"), "https://mcp.example/operator/prefix");
  assert.equal(resolvePublicGatewayUrl("https://mcp.example?token=secret"), null);
  assert.throws(() => resolveSessionKey("a1"));
  assert.equal(resolveSessionKey(key).byteLength, 32);
});

test("login form tokens and form bodies stay within their limits", () => {
  assert.equal(parseLoginToken("credential_1"), "credential_1");
  assert.throws(() => parseLoginToken("x".repeat(MAX_LOGIN_TOKEN_CHARS + 1)));
  assert.throws(() => parseLoginToken("credential with spaces"));

  const form = parseUrlEncodedFormBody(new TextEncoder().encode("token=credential_1"));
  assert.equal(form?.get("token"), "credential_1");
  assert.equal(parseUrlEncodedFormBody(new Uint8Array(MAX_FORM_BODY_BYTES + 1)), null);
  assert.equal(parseUrlEncodedFormBody(new Uint8Array([0xff])), null);
});

test("serialized cookies are bounded and use strict browser attributes", () => {
  const encrypted = sealSession(session, key);
  const cookie = serializeSessionCookie(encrypted, true);
  assert.match(cookie, /^energy_web_session=/);
  assert.match(cookie, /; Path=\//);
  assert.match(cookie, /; HttpOnly/);
  assert.match(cookie, /; SameSite=Strict/);
  assert.match(cookie, /; Max-Age=28800/);
  assert.match(cookie, /; Secure$/);
  assert.ok(Buffer.byteLength(cookie, "utf8") <= MAX_SESSION_COOKIE_BYTES);

  assert.throws(() => serializeSessionCookie("A".repeat(MAX_SESSION_COOKIE_BYTES), true), RangeError);
  const tooLargeTokenSession = { ...session, token: "x".repeat(MAX_LOGIN_TOKEN_CHARS) };
  const tooLargeEncrypted = sealSession(tooLargeTokenSession, key);
  assert.throws(() => serializeSessionCookie(tooLargeEncrypted, false), RangeError);
});
