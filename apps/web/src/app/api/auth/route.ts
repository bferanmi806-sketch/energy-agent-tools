import { EnergyAgentTools } from "@energy-agent-tools/sdk";
import {
  SESSION_TTL_MS,
  isAllowedMutationOrigin,
  parseLoginToken,
  readBoundedForm,
  readWebRuntimeConfig,
  resolveWebOrigin,
  sealSession,
  serializeClearedOAuthFlowCookie,
  serializeSessionCookie,
} from "@/lib/security";

export const runtime = "nodejs";

type AuthErrorCode = "configuration" | "invalid_token" | "cookie_too_large";

function redirectHome(code?: AuthErrorCode, cookies?: string[]): Response {
  const location = code === undefined ? "/" : `/?auth_error=${code}`;
  const headers = new Headers({
    "Cache-Control": "no-store",
    Location: location,
  });
  for (const cookie of cookies ?? []) headers.append("Set-Cookie", cookie);
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
    return redirectHome(undefined, [cookie, serializeClearedOAuthFlowCookie(config.webOrigin.secure)]);
  } catch (error) {
    return redirectHome(error instanceof RangeError ? "cookie_too_large" : "configuration");
  }
}
