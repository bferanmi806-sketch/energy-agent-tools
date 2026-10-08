import { EnergyHttpError, type WorkspaceProviderAuthorizationRequest } from "@energy-agent-tools/sdk";
import {
  bindOAuthFlowToSession,
  OAUTH_FLOW_MAX_TTL_MS,
  sealOAuthFlow,
  serializeOAuthFlowCookie,
  serializeSessionCookie,
} from "@/lib/security";
import { approvedCloudAuthorizationUrl } from "@/lib/managed-oauth";
import {
  exactWorkspaceForm,
  openWorkspaceMutation,
  textField,
  workspaceFailure,
} from "@/lib/managed";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  const context = await openWorkspaceMutation(request);
  if (context.kind === "error") return workspaceFailure(context.status);

  const form = await exactWorkspaceForm(request, ["configuration_id", "resource_id"], ["configuration_id", "resource_id"]);
  if (!form) return workspaceFailure(400);
  const configurationId = textField(form, "configuration_id", 64);
  const resourceId = textField(form, "resource_id", 32);
  if (!configurationId || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(configurationId) || !resourceId || !/^[0-9]{1,32}$/.test(resourceId)) return workspaceFailure(400);
  const input: WorkspaceProviderAuthorizationRequest = { configuration_id: configurationId, resource_id: resourceId };

  const managerWorkspace = context.identity.workspace;
  if (!managerWorkspace || managerWorkspace.mode !== "managed") return workspaceFailure(403);

  try {
    const { configurations } = await context.workspace.authConfigurations();
    const configuration = configurations.find(item => item.id === configurationId);
    if (!configuration || configuration.protocol !== "oauth2_confidential" || (configuration.toolkit !== "tesla-energy" && configuration.toolkit !== "enphase-energy")) return workspaceFailure(422);
    const result = await context.workspace.beginProviderAuthorization(input);
    const authorization = result.authorization;
    const destination = approvedCloudAuthorizationUrl(
      authorization,
      configuration.toolkit,
      context.config.webOrigin,
    );
    const now = Date.now();
    const expiresAt = Date.parse(authorization.expires_at);
    const remainingMs = expiresAt - now;
    if (
      !destination || !Number.isSafeInteger(expiresAt) || remainingMs <= 0 ||
      remainingMs > OAUTH_FLOW_MAX_TTL_MS || authorization.connection_id.length === 0 ||
      authorization.connection_id.length > 256
    ) return workspaceFailure(502);

    const flowCookie = sealOAuthFlow({
      version: 1,
      state: authorization.state,
      configurationId,
      connectionId: authorization.connection_id,
      managerId: context.identity.user_id,
      workspaceId: managerWorkspace.id,
      sessionBinding: bindOAuthFlowToSession(context.sessionCookie),
      createdAt: now,
      expiresAt,
    }, context.config.sessionKey.toString("hex"));
    const maxAgeSeconds = Math.ceil(remainingMs / 1000);
    const headers = new Headers({
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
      "Referrer-Policy": "no-referrer",
    });
    headers.append("Set-Cookie", serializeOAuthFlowCookie(flowCookie, context.config.webOrigin.secure, maxAgeSeconds));
    headers.append("Set-Cookie", serializeSessionCookie(context.sessionCookie, context.config.webOrigin.secure));
    return new Response(JSON.stringify({ ok: true, authorization_url: destination.toString() }), { status: 200, headers });
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
