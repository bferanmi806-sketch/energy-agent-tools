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

  const form = await exactWorkspaceForm(request, ["configuration_id"], ["configuration_id"]);
  if (!form) return workspaceFailure(400);

  const configurationId = textField(form, "configuration_id", 64);
  if (!configurationId || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(configurationId)) return workspaceFailure(400);

  try {
    const result = await context.workspace.retryAuthorizationCleanup({ configuration_id: configurationId });
    return success({ ok: true, cleanup: result.cleanup });
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
