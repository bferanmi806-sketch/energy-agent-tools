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

  const form = await exactWorkspaceForm(request, ["name", "timezone"], ["name", "timezone"]);
  if (!form) return workspaceFailure(400);

  const name = textField(form, "name", 256)?.trim();
  const timezone = textField(form, "timezone", 80)?.trim();
  if (!name || !timezone) return workspaceFailure(400);

  try {
    const result = await context.workspace.createSite({ name, timezone });
    return success({ ok: true, site: result.site }, 201);
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
