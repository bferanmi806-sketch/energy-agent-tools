import { createCipheriv, createDecipheriv, createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { isIP } from "node:net";

export const SESSION_COOKIE_NAME = "energy_web_session";
export const OAUTH_FLOW_COOKIE_NAME = "energy_web_oauth_flow";
export const OAUTH_CALLBACK_PATH = "/api/workspace/oauth/callback";
export const SESSION_TTL_MS = 8 * 60 * 60 * 1000;
export const OAUTH_FLOW_MAX_TTL_MS = 60 * 60 * 1000;
export const MAX_LOGIN_TOKEN_CHARS = 4096;
export const MAX_SESSION_COOKIE_BYTES = 4096;
export const MAX_OAUTH_FLOW_COOKIE_BYTES = 2048;
export const MAX_SITE_ID_CHARS = 256;
export const MAX_FORM_BODY_BYTES = 16 * 1024;

const GCM_NONCE_BYTES = 12;
const GCM_TAG_BYTES = 16;
const MAX_FUTURE_ISSUED_AT_MS = 60_000;
const TOKEN_ERROR = "Enter a valid gateway token.";
const CONFIG_ERROR = "The web security configuration is invalid.";

export interface WebSession {
  version: 1;
  token: string;
  issuedAt: number;
  expiresAt: number;
  siteId: string | null;
}

export interface OAuthFlowCookie {
  version: 1;
  state: string;
  configurationId: string;
  connectionId: string;
  managerId: string;
  workspaceId: string;
  sessionBinding: string;
  createdAt: number;
  expiresAt: number;
}

export interface WebOrigin {
  origin: string;
  secure: boolean;
}

export interface WebRuntimeConfig {
  gatewayUrl: string;
  webOrigin: WebOrigin;
  sessionKey: Buffer;
  publicGatewayUrl: string | null;
}

export interface RuntimeEnvironment {
  ENERGY_GATEWAY_URL?: string;
  ENERGY_WEB_ORIGIN?: string;
  ENERGY_WEB_SESSION_KEY?: string;
  ENERGY_PUBLIC_GATEWAY_URL?: string;
  NODE_ENV?: string;
}

function hasUnsafeWhitespace(value: string): boolean {
  return value.trim() !== value || /[\u0000-\u0020\u007f]/.test(value);
}

function hasCredentialsAuthority(value: string): boolean {
  const authority = /^[a-z][a-z\d+.-]*:\/\/([^/?#]*)/i.exec(value)?.[1];
  return authority === undefined || authority.includes("@");
}

function parseSafeHttpUrl(value: string | undefined): URL | null {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    hasUnsafeWhitespace(value) ||
    value.includes("\\") ||
    value.includes("?") ||
    value.includes("#") ||
    hasCredentialsAuthority(value)
  ) {
    return null;
  }

  try {
    const url = new URL(value);
    if (
      (url.protocol !== "http:" && url.protocol !== "https:") ||
      url.username !== "" ||
      url.password !== "" ||
      url.search !== "" ||
      url.hash !== ""
    ) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (normalized === "localhost" || normalized.endsWith(".localhost") || normalized === "::1") {
    return true;
  }
  if (isIP(normalized) === 4) return normalized.split(".")[0] === "127";
  return false;
}

export function resolveGatewayUrl(value: string | undefined, production = false): string {
  const url = parseSafeHttpUrl(value);
  if (!url || (production && url.protocol !== "https:" && !isLoopbackHostname(url.hostname))) {
    throw new TypeError(CONFIG_ERROR);
  }
  if (!url.pathname.endsWith("/")) url.pathname += "/";
  return url.toString();
}

export function resolveWebOrigin(value: string | undefined, production = false): WebOrigin {
  const url = parseSafeHttpUrl(value);
  if (
    !url ||
    (url.pathname !== "/" && url.pathname !== "") ||
    (production && url.protocol !== "https:" && !isLoopbackHostname(url.hostname))
  ) {
    throw new TypeError(CONFIG_ERROR);
  }
  return { origin: url.origin, secure: url.protocol === "https:" };
}

export function resolvePublicGatewayUrl(value: string | undefined): string | null {
  const url = parseSafeHttpUrl(value);
  return url?.toString() ?? null;
}

export function resolveSessionKey(value: string | undefined): Buffer {
  if (typeof value !== "string" || !/^[\da-fA-F]{64}$/.test(value)) {
    throw new TypeError(CONFIG_ERROR);
  }
  return Buffer.from(value, "hex");
}

export function readWebRuntimeConfig(environment: RuntimeEnvironment): WebRuntimeConfig {
  const production = environment.NODE_ENV === "production";
  const webOrigin = resolveWebOrigin(environment.ENERGY_WEB_ORIGIN, production);
  return {
    gatewayUrl: resolveGatewayUrl(environment.ENERGY_GATEWAY_URL, production),
    webOrigin,
    sessionKey: resolveSessionKey(environment.ENERGY_WEB_SESSION_KEY),
    publicGatewayUrl: resolvePublicGatewayUrl(environment.ENERGY_PUBLIC_GATEWAY_URL),
  };
}

export function isAllowedMutationOrigin(originHeader: string | null, expected: WebOrigin): boolean {
  if (originHeader === null || originHeader.length === 0 || hasUnsafeWhitespace(originHeader)) return false;
  const url = parseSafeHttpUrl(originHeader);
  return url !== null && (url.pathname === "/" || url.pathname === "") && url.origin === expected.origin;
}

export function parseLoginToken(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_LOGIN_TOKEN_CHARS ||
    hasUnsafeWhitespace(value)
  ) {
    throw new TypeError(TOKEN_ERROR);
  }
  return value;
}

export function parseUrlEncodedFormBody(body: Uint8Array): URLSearchParams | null {
  if (body.byteLength > MAX_FORM_BODY_BYTES) return null;
  try {
    return new URLSearchParams(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    return null;
  }
}

function sessionKey(keyHex: string): Buffer {
  return resolveSessionKey(keyHex);
}

function isOAuthFlowPayload(value: unknown): value is OAuthFlowCookie {
  if (!isRecord(value)) return false;
  const record = value;
  return (
    record.version === 1 &&
    typeof record.state === "string" && record.state.length > 0 && record.state.length <= 512 && !hasUnsafeWhitespace(record.state) &&
    typeof record.configurationId === "string" && /^[a-z0-9][a-z0-9-]{0,63}$/.test(record.configurationId) &&
    typeof record.connectionId === "string" && record.connectionId.length > 0 && record.connectionId.length <= 256 && !/[\u0000-\u001f\u007f]/.test(record.connectionId) &&
    typeof record.managerId === "string" && record.managerId.length > 0 && record.managerId.length <= 256 && !/[\u0000-\u001f\u007f]/.test(record.managerId) &&
    typeof record.workspaceId === "string" && record.workspaceId.length > 0 && record.workspaceId.length <= 256 && !/[\u0000-\u001f\u007f]/.test(record.workspaceId) &&
    typeof record.sessionBinding === "string" && /^[A-Za-z0-9_-]{43}$/.test(record.sessionBinding) &&
    typeof record.createdAt === "number" && Number.isSafeInteger(record.createdAt) &&
    typeof record.expiresAt === "number" && Number.isSafeInteger(record.expiresAt) &&
    record.expiresAt > record.createdAt && record.expiresAt - record.createdAt <= OAUTH_FLOW_MAX_TTL_MS
  );
}

export function bindOAuthFlowToSession(sessionCookie: string): string {
  return createHash("sha256").update(sessionCookie, "utf8").digest("base64url");
}

export function matchesOAuthSessionBinding(binding: string, sessionCookie: string | undefined): boolean {
  if (typeof sessionCookie !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(binding)) return false;
  const expected = Buffer.from(binding, "base64url");
  const actual = Buffer.from(bindOAuthFlowToSession(sessionCookie), "base64url");
  return expected.byteLength === actual.byteLength && timingSafeEqual(expected, actual);
}

export function matchesOAuthState(expected: string, provided: string): boolean {
  const expectedBytes = Buffer.from(expected, "utf8");
  const providedBytes = Buffer.from(provided, "utf8");
  return expectedBytes.byteLength === providedBytes.byteLength && timingSafeEqual(expectedBytes, providedBytes);
}

export function sealOAuthFlow(flow: OAuthFlowCookie, keyHex: string): string {
  if (!isOAuthFlowPayload(flow)) throw new TypeError("The encrypted authorization flow is invalid.");
  const nonce = randomBytes(GCM_NONCE_BYTES);
  const cipher = createCipheriv("aes-256-gcm", sessionKey(keyHex), nonce);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(flow), "utf8"), cipher.final()]);
  const value = Buffer.concat([nonce, cipher.getAuthTag(), ciphertext]).toString("base64url");
  if (Buffer.byteLength(`${OAUTH_FLOW_COOKIE_NAME}=${value}`, "utf8") > MAX_OAUTH_FLOW_COOKIE_BYTES) {
    throw new RangeError("The encrypted authorization flow cookie is too large.");
  }
  return value;
}

export function openOAuthFlow(value: string | undefined, keyHex: string, now = Date.now()): OAuthFlowCookie | null {
  if (
    typeof value !== "string" || value.length === 0 ||
    Buffer.byteLength(`${OAUTH_FLOW_COOKIE_NAME}=${value}`, "utf8") > MAX_OAUTH_FLOW_COOKIE_BYTES ||
    !/^[A-Za-z0-9_-]+$/.test(value)
  ) return null;

  const packed = Buffer.from(value, "base64url");
  if (packed.toString("base64url") !== value || packed.byteLength <= GCM_NONCE_BYTES + GCM_TAG_BYTES) return null;

  try {
    const decipher = createDecipheriv("aes-256-gcm", sessionKey(keyHex), packed.subarray(0, GCM_NONCE_BYTES));
    decipher.setAuthTag(packed.subarray(GCM_NONCE_BYTES, GCM_NONCE_BYTES + GCM_TAG_BYTES));
    const plaintext = Buffer.concat([
      decipher.update(packed.subarray(GCM_NONCE_BYTES + GCM_TAG_BYTES)),
      decipher.final(),
    ]).toString("utf8");
    const decoded: unknown = JSON.parse(plaintext);
    return isOAuthFlowPayload(decoded) && decoded.createdAt <= now + MAX_FUTURE_ISSUED_AT_MS && decoded.expiresAt > now
      ? decoded
      : null;
  } catch {
    return null;
  }
}

export function serializeOAuthFlowCookie(value: string, secure: boolean, maxAgeSeconds: number): string {
  const serialized = `${OAUTH_FLOW_COOKIE_NAME}=${value}; Path=${OAUTH_CALLBACK_PATH}; HttpOnly; SameSite=Lax; Max-Age=${maxAgeSeconds}${secure ? "; Secure" : ""}`;
  if (
    !/^[A-Za-z0-9_-]+$/.test(value) ||
    !Number.isSafeInteger(maxAgeSeconds) || maxAgeSeconds < 1 || maxAgeSeconds > OAUTH_FLOW_MAX_TTL_MS / 1000 ||
    Buffer.byteLength(serialized, "utf8") > MAX_OAUTH_FLOW_COOKIE_BYTES
  ) {
    throw new RangeError("The encrypted authorization flow cookie is invalid.");
  }
  return serialized;
}

export function serializeClearedOAuthFlowCookie(secure: boolean): string {
  return `${OAUTH_FLOW_COOKIE_NAME}=; Path=${OAUTH_CALLBACK_PATH}; HttpOnly; SameSite=Lax; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT${secure ? "; Secure" : ""}`;
}

function isSessionPayload(value: unknown): value is WebSession {
  if (!isRecord(value)) return false;
  const record = value;
  return (
    record.version === 1 &&
    typeof record.token === "string" &&
    record.token.length > 0 &&
    record.token.length <= MAX_LOGIN_TOKEN_CHARS &&
    !hasUnsafeWhitespace(record.token) &&
    typeof record.issuedAt === "number" &&
    Number.isSafeInteger(record.issuedAt) &&
    typeof record.expiresAt === "number" &&
    Number.isSafeInteger(record.expiresAt) &&
    record.expiresAt > record.issuedAt &&
    record.expiresAt - record.issuedAt <= SESSION_TTL_MS &&
    (record.siteId === null ||
      (typeof record.siteId === "string" && record.siteId.length <= MAX_SITE_ID_CHARS))
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isUsableSession(session: WebSession, now: number): boolean {
  return session.issuedAt <= now + MAX_FUTURE_ISSUED_AT_MS && session.expiresAt > now;
}

export function sealSession(session: WebSession, keyHex: string): string {
  if (!isSessionPayload(session) || !isUsableSession(session, session.issuedAt)) {
    throw new TypeError("The encrypted session payload is invalid.");
  }

  const nonce = randomBytes(GCM_NONCE_BYTES);
  const cipher = createCipheriv("aes-256-gcm", sessionKey(keyHex), nonce);
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify(session), "utf8"),
    cipher.final(),
  ]);
  return Buffer.concat([nonce, cipher.getAuthTag(), ciphertext]).toString("base64url");
}

export function openSession(value: string | undefined, keyHex: string, now = Date.now()): WebSession | null {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    Buffer.byteLength(`${SESSION_COOKIE_NAME}=${value}`, "utf8") > MAX_SESSION_COOKIE_BYTES ||
    !/^[A-Za-z0-9_-]+$/.test(value)
  ) {
    return null;
  }

  const packed = Buffer.from(value, "base64url");
  if (packed.toString("base64url") !== value || packed.byteLength <= GCM_NONCE_BYTES + GCM_TAG_BYTES) {
    return null;
  }

  try {
    const key = sessionKey(keyHex);
    const nonce = packed.subarray(0, GCM_NONCE_BYTES);
    const tagStart = GCM_NONCE_BYTES;
    const ciphertextStart = GCM_NONCE_BYTES + GCM_TAG_BYTES;
    const decipher = createDecipheriv("aes-256-gcm", key, nonce);
    decipher.setAuthTag(packed.subarray(tagStart, ciphertextStart));
    const plaintext = Buffer.concat([
      decipher.update(packed.subarray(ciphertextStart)),
      decipher.final(),
    ]).toString("utf8");
    const decoded: unknown = JSON.parse(plaintext);
    return isSessionPayload(decoded) && isUsableSession(decoded, now) ? decoded : null;
  } catch {
    return null;
  }
}

export function serializeSessionCookie(value: string, secure: boolean): string {
  const serialized = `${SESSION_COOKIE_NAME}=${value}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${SESSION_TTL_MS / 1000}${secure ? "; Secure" : ""}`;
  if (
    !/^[A-Za-z0-9_-]+$/.test(value) ||
    Buffer.byteLength(serialized, "utf8") > MAX_SESSION_COOKIE_BYTES
  ) {
    throw new RangeError("The encrypted session cookie is too large.");
  }
  return serialized;
}

export function serializeClearedSessionCookie(secure: boolean): string {
  return `${SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT${secure ? "; Secure" : ""}`;
}

export async function readBoundedForm(request: Request): Promise<URLSearchParams | null> {
  const contentType = request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (contentType !== "application/x-www-form-urlencoded") return null;

  const contentLength = request.headers.get("content-length");
  if (contentLength !== null && (!/^\d+$/.test(contentLength) || Number(contentLength) > MAX_FORM_BODY_BYTES)) {
    return null;
  }

  if (request.body === null) return new URLSearchParams();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      if (part.value.byteLength > MAX_FORM_BODY_BYTES - total) {
        await reader.cancel().catch(() => undefined);
        return null;
      }
      chunks.push(part.value.slice());
      total += part.value.byteLength;
    }
  } catch {
    return null;
  } finally {
    reader.releaseLock();
  }

  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return parseUrlEncodedFormBody(body);
}
