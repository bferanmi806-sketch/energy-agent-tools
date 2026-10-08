import { cookies } from "next/headers";
import {
  OAUTH_CALLBACK_PATH,
  OAUTH_FLOW_COOKIE_NAME,
  openOAuthFlow,
  matchesOAuthSessionBinding,
  matchesOAuthState,
  readWebRuntimeConfig,
  SESSION_COOKIE_NAME,
  serializeClearedOAuthFlowCookie,
} from "@/lib/security";
import { openWorkspaceForOAuthCallback, workspaceFailure } from "@/lib/managed";

export const runtime = "nodejs";

type CallbackResult = "connected" | "provider_connected" | "cancelled" | "invalid" | "failed";

function redirectResult(result: CallbackResult, secure: boolean, clearFlow = false): Response {
  const location = result === "provider_connected"
    ? "/?view=connections&oauth=provider_connected"
    : result === "connected"
    ? "/?view=connections&oauth=connected"
    : result === "cancelled"
      ? "/?view=apps&oauth=cancelled"
      : result === "failed"
        ? "/?view=apps&oauth=failed"
        : "/?view=apps&oauth=invalid";
  const headers = new Headers({
    "Cache-Control": "no-store",
    "Location": location,
    "Referrer-Policy": "no-referrer",
  });
  if (clearFlow) headers.append("Set-Cookie", serializeClearedOAuthFlowCookie(secure));
  return new Response(null, { status: 303, headers });
}

function hasSafeOpaqueValue(value: string, maximum: number): boolean {
  return value.length > 0 && value.length <= maximum && !/[\u0000-\u0020\u007f]/.test(value);
}

export async function GET(request: Request): Promise<Response> {
  let config;
  try {
    config = readWebRuntimeConfig(process.env);
  } catch {
    return workspaceFailure(503);
  }

  let callbackUrl: URL;
  try {
    callbackUrl = new URL(request.url);
  } catch {
    return workspaceFailure(400);
  }
  if (callbackUrl.pathname !== OAUTH_CALLBACK_PATH || callbackUrl.href.length > 8192) {
    return workspaceFailure(400);
  }

  const cookieJar = await cookies();
  const flowCookieValue = cookieJar.get(OAUTH_FLOW_COOKIE_NAME)?.value;
  const flow = openOAuthFlow(flowCookieValue, config.sessionKey.toString("hex"));
  if (!flow) return redirectResult("invalid", config.webOrigin.secure, true);

  const stateValues = callbackUrl.searchParams.getAll("state");
  if (
    stateValues.length !== 1 || !hasSafeOpaqueValue(stateValues[0] ?? "", 512) ||
    !matchesOAuthState(flow.state, stateValues[0] ?? "")
  ) return redirectResult("invalid", config.webOrigin.secure);

  const sessionCookie = cookieJar.get(SESSION_COOKIE_NAME)?.value;
  if (!matchesOAuthSessionBinding(flow.sessionBinding, sessionCookie)) {
    return redirectResult("invalid", config.webOrigin.secure);
  }

  const providerErrors = callbackUrl.searchParams.getAll("error");
  if (providerErrors.length > 1) return redirectResult("invalid", config.webOrigin.secure);
  if (providerErrors.length === 1) return redirectResult("cancelled", config.webOrigin.secure, true);

  const codeValues = callbackUrl.searchParams.getAll("code");
  if (codeValues.length !== 1 || !hasSafeOpaqueValue(codeValues[0] ?? "", 2048)) {
    return redirectResult("invalid", config.webOrigin.secure);
  }

  const context = await openWorkspaceForOAuthCallback(sessionCookie, config);
  if (context.kind === "error") {
    return redirectResult(context.status === 401 || context.status === 403 ? "invalid" : "failed", config.webOrigin.secure, true);
  }

  const managerWorkspace = context.identity.workspace;
  if (
    context.identity.user_id !== flow.managerId ||
    !managerWorkspace || managerWorkspace.mode !== "managed" || managerWorkspace.id !== flow.workspaceId
  ) return redirectResult("invalid", config.webOrigin.secure, true);

  try {
    const { configurations } = await context.workspace.authConfigurations();
    const configuration = configurations.find(item => item.id === flow.configurationId);
    if (!configuration) return redirectResult("invalid", config.webOrigin.secure, true);
    const result = await context.workspace.completeAuthorization({
      configuration_id: flow.configurationId,
      state: flow.state,
      code: codeValues[0] ?? "",
    });
    if (
      result.account.id !== flow.connectionId ||
      result.account.toolkit !== configuration.toolkit ||
      result.account.site_id !== null ||
      result.account.enabled !== false ||
      result.account.state !== "pending_mapping" ||
      result.account.verified !== true ||
      result.health.connection_id !== flow.connectionId ||
      result.health.provider !== ({ "home-assistant": "home_assistant", "tesla-energy": "tesla", "enphase-energy": "enphase" }[configuration.toolkit]) ||
      result.health.status !== "healthy" ||
      result.health.probe !== "provider-read"
    ) return redirectResult("failed", config.webOrigin.secure, true);
    return redirectResult(configuration.toolkit === "home-assistant" ? "connected" : "provider_connected", config.webOrigin.secure, true);
  } catch {
    return redirectResult("failed", config.webOrigin.secure, true);
  }
}
