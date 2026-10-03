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
    ["site_id", "name", "kind", "account_id"],
    ["site_id", "name", "kind"],
    ["account_id"],
  );
  if (!form) return workspaceFailure(400);

  const siteId = textField(form, "site_id", 256);
  const name = textField(form, "name", 256)?.trim();
  const kind = textField(form, "kind", 80)?.trim();
  const accountIds = form.getAll("account_id");
  if (
    !siteId || !name || !kind || accountIds.length > 32 ||
    accountIds.some((id) => !id || id.length > 256 || /[\u0000-\u001f\u007f]/.test(id))
  ) {
    return workspaceFailure(400);
  }

  try {
    const result = await context.workspace.createAsset({
      site_id: siteId,
      name,
      kind,
      account_ids: accountIds,
    });
    return success({ ok: true, asset: result.asset }, 201);
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
