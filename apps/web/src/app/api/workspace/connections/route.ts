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
    ["provider", "credential", "mpan", "serial_number"],
    ["provider", "credential", "mpan", "serial_number"],
  );
  if (!form) return workspaceFailure(400);

  const provider = textField(form, "provider", 16);
  const credential = textField(form, "credential", 4096);
  const mpan = textField(form, "mpan", 13);
  const serialNumber = textField(form, "serial_number", 120);
  if (
    provider !== "octopus" || !credential || mpan === null || serialNumber === null || !/^\d{13}$/.test(mpan) ||
    !/^[A-Za-z0-9._~-]+$/.test(serialNumber)
  ) {
    return workspaceFailure(400);
  }

  try {
    const result = await context.workspace.connectAccount({
      provider,
      credential,
      mpan,
      serial_number: serialNumber,
    });
    return success(
      { ok: true, connection_id: result.account.id, state: result.account.state },
      201,
    );
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
