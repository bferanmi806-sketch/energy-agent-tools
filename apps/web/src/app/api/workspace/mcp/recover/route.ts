import { EnergyHttpError } from "@energy-agent-tools/sdk";
import { openWorkspaceMutation, success, workspaceFailure } from "@/lib/managed";
import { readBoundedJson } from "@/lib/security";

export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  const context = await openWorkspaceMutation(request);
  if (context.kind === "error") return workspaceFailure(context.status);
  const input = await readBoundedJson(request);
  if (typeof input !== "object" || input === null || Array.isArray(input) || Object.keys(input).length !== 0) {
    return workspaceFailure(400);
  }
  try {
    return success(await context.workspace.recoverMcp());
  } catch (error) {
    return workspaceFailure(error instanceof EnergyHttpError && (error.status === 401 || error.status === 403) ? error.status : 502);
  }
}
