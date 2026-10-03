import { EnergyHttpError, type WorkspaceHomeAssistantAuthorizationRequest } from "@energy-agent-tools/sdk";
import {
  bindOAuthFlowToSession,
  OAUTH_FLOW_MAX_TTL_MS,
  sealOAuthFlow,
  serializeOAuthFlowCookie,
  serializeSessionCookie,
} from "@/lib/security";
import { approvedHomeAssistantAuthorizationUrl } from "@/lib/managed-oauth";
import {
  exactWorkspaceForm,
  openWorkspaceMutation,
  textField,
  workspaceFailure,
} from "@/lib/managed";

export const runtime = "nodejs";

const TELEMETRY_ROLES = ["consumption_interval", "current_power", "generation", "export", "storage_state"] as const;
const TELEMETRY_UNITS = ["kWh", "W", "kW", "MW", "%"] as const;
const QUANTITY_SHAPES = ["interval", "instantaneous"] as const;

function isTelemetryRole(value: string | null): value is typeof TELEMETRY_ROLES[number] {
  switch (value) {
    case "consumption_interval":
    case "current_power":
    case "generation":
    case "export":
    case "storage_state":
      return true;
    default:
      return false;
  }
}

function isTelemetryUnit(value: string | null): value is typeof TELEMETRY_UNITS[number] {
  switch (value) {
    case "kWh":
    case "W":
    case "kW":
    case "MW":
    case "%":
      return true;
    default:
      return false;
  }
}

function isQuantityShape(value: string | null): value is typeof QUANTITY_SHAPES[number] {
  switch (value) {
    case "interval":
    case "instantaneous":
      return true;
    default:
      return false;
  }
}

export async function POST(request: Request): Promise<Response> {
  const context = await openWorkspaceMutation(request);
  if (context.kind === "error") return workspaceFailure(context.status);

  const form = await exactWorkspaceForm(
    request,
    ["configuration_id", "entity_id", "reviewed_mapping", "telemetry_role", "unit", "quantity_shape"],
    ["configuration_id", "entity_id"],
  );
  if (!form) return workspaceFailure(400);

  const configurationId = textField(form, "configuration_id", 64);
  const entityId = textField(form, "entity_id", 256);
  if (
    !configurationId || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(configurationId) ||
    !entityId || !/^[A-Za-z0-9_.:-]+$/.test(entityId)
  ) return workspaceFailure(400);

  const reviewedMapping = form.get("reviewed_mapping");
  const telemetryRole = form.get("telemetry_role");
  const unit = form.get("unit");
  const quantityShape = form.get("quantity_shape");
  let input: WorkspaceHomeAssistantAuthorizationRequest;
  if (reviewedMapping === null) {
    if (telemetryRole !== null || unit !== null || quantityShape !== null) return workspaceFailure(400);
    input = { configuration_id: configurationId, entity_id: entityId };
  } else {
    if (
      reviewedMapping !== "reviewed" || !isTelemetryRole(telemetryRole) ||
      !isTelemetryUnit(unit) || !isQuantityShape(quantityShape)
    ) return workspaceFailure(400);
    input = {
      configuration_id: configurationId,
      entity_id: entityId,
      mapping: {
        telemetry_role: telemetryRole,
        unit,
        quantity_shape: quantityShape,
        measurement_kind: "metered",
      },
    };
  }

  const managerWorkspace = context.identity.workspace;
  if (!managerWorkspace || managerWorkspace.mode !== "managed") return workspaceFailure(403);

  try {
    const result = await context.workspace.beginAuthorization(input);
    const authorization = result.authorization;
    const destination = approvedHomeAssistantAuthorizationUrl(
      authorization,
      context.config.webOrigin,
      process.env.NODE_ENV === "production",
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
