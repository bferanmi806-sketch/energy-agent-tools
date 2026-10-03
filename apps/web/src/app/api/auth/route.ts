import { EnergyAgentTools } from "@energy-agent-tools/sdk";
import {
  MAX_FORM_BODY_BYTES,
  SESSION_TTL_MS,
  isAllowedMutationOrigin,
  parseLoginToken,
  parseUrlEncodedFormBody,
  readWebRuntimeConfig,
  resolveWebOrigin,
  sealSession,
  serializeSessionCookie,
} from "@/lib/security";

export const runtime = "nodejs";

type AuthErrorCode = "configuration" | "invalid_token" | "cookie_too_large";

function redirectHome(code?: AuthErrorCode, cookie?: string): Response {
  const location = code === undefined ? "/" : `/?auth_error=${code}`;
  const headers = new Headers({
    "Cache-Control": "no-store",
    Location: location,
  });
  if (cookie !== undefined) headers.append("Set-Cookie", cookie);
  return new Response(null, { status: 303, headers });
}

function forbidden(): Response {
  return new Response("Forbidden", {
    status: 403,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "text/plain; charset=utf-8",
    },
  });
}

async function readBoundedForm(request: Request): Promise<URLSearchParams | null> {
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

export async function POST(request: Request): Promise<Response> {
  const originHeader = request.headers.get("origin");
  if (originHeader === null) return forbidden();

  let expectedOrigin;
  try {
    expectedOrigin = resolveWebOrigin(process.env.ENERGY_WEB_ORIGIN);
  } catch {
    return redirectHome("configuration");
  }
  if (!isAllowedMutationOrigin(originHeader, expectedOrigin)) return forbidden();

  let config;
  try {
    config = readWebRuntimeConfig(process.env);
  } catch {
    return redirectHome("configuration");
  }

  const form = await readBoundedForm(request);
  if (form === null || form.getAll("token").length !== 1) return redirectHome("invalid_token");

  let token: string;
  try {
    token = parseLoginToken(form.get("token"));
  } catch {
    return redirectHome("invalid_token");
  }

  try {
    const client = new EnergyAgentTools({
      baseUrl: config.gatewayUrl,
      token,
      fetch: (input, init) => fetch(input, { ...init, cache: "no-store" }),
    });
    await client.identity();
  } catch {
    return redirectHome("invalid_token");
  }

  const issuedAt = Date.now();
  try {
    const sealed = sealSession({
      version: 1,
      token,
      issuedAt,
      expiresAt: issuedAt + SESSION_TTL_MS,
      siteId: null,
    }, config.sessionKey.toString("hex"));
    const cookie = serializeSessionCookie(sealed, config.webOrigin.secure);
    return redirectHome(undefined, cookie);
  } catch (error) {
    return redirectHome(error instanceof RangeError ? "cookie_too_large" : "configuration");
  }
}
