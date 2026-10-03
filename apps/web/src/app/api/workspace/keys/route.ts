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
    ["name", "site_id"],
    ["name"],
    ["site_id"],
  );
  if (!form) return workspaceFailure(400);

  const name = textField(form, "name", 256)?.trim();
  const siteIds = form.getAll("site_id");
  if (
    !name || siteIds.length === 0 || siteIds.length > 32 ||
    siteIds.some((siteId) => !siteId || siteId.length > 256 || /[\u0000-\u001f\u007f]/.test(siteId)) ||
    new Set(siteIds).size !== siteIds.length
  ) {
    return workspaceFailure(400);
  }

  try {
    const result = await context.workspace.createAgentKey({ name, site_ids: siteIds });
    return success({ ok: true, token: result.token, key: result.key }, 201);
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
