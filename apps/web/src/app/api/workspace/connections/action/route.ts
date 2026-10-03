import { EnergyHttpError } from "@energy-agent-tools/sdk";
import {
  exactWorkspaceForm,
  openWorkspaceMutation,
  success,
  textField,
  workspaceFailure,
} from "@/lib/managed";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  const context = await openWorkspaceMutation(request);
  if (context.kind === "error") return workspaceFailure(context.status);

  const form = await exactWorkspaceForm(
    request,
    ["action", "connection_id", "site_id"],
    ["action", "connection_id"],
  );
  if (!form) return workspaceFailure(400);

  const action = textField(form, "action", 16);
  const connectionId = textField(form, "connection_id", 256);
  const siteId = form.get("site_id");
  if (!connectionId || (siteId !== null && (!siteId || siteId.length > 256))) {
    return workspaceFailure(400);
  }

  try {
    if (action === "map") {
      if (!siteId) return workspaceFailure(400);
      await context.workspace.mapConnection(connectionId, { site_id: siteId });
      return success({ ok: true, action });
    }
    if (siteId !== null) return workspaceFailure(400);

    if (action === "verify") {
      const result = await context.workspace.verifyConnection(connectionId);
      return success({
        ok: true,
        action,
        status: result.health.status,
        checked_at: result.health.checked_at,
      });
    }
    if (action === "disconnect") {
      await context.workspace.disconnectConnection(connectionId);
      return success({ ok: true, action });
    }
    return workspaceFailure(400);
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
