import { EnergyHttpError, parseMcpInspectionRequest } from "@energy-agent-tools/sdk";
import { openWorkspaceMutation, success, workspaceFailure } from "@/lib/managed";
import { readBoundedJson } from "@/lib/security";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  const context = await openWorkspaceMutation(request);
  if (context.kind === "error") return workspaceFailure(context.status);
  let input;
  try {
    input = parseMcpInspectionRequest(await readBoundedJson(request));
  } catch {
    return workspaceFailure(400);
  }
  try {
    const result = await context.workspace.inspectMcp(input);
    return success(result, 200);
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
