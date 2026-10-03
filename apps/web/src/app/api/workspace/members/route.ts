import { EnergyHttpError } from "@energy-agent-tools/sdk";
import {
  exactWorkspaceForm,
  openWorkspaceMutation,
  success,
  textField,
  workspaceFailure,
} from "@/lib/managed";

export const runtime = "nodejs";

function identifiers(form: URLSearchParams, field: string): string[] | null {
  const values = form.getAll(field);
  if (
    values.length > 256 || new Set(values).size !== values.length ||
    values.some((value) => !value.trim() || value.length > 256 || /[\u0000-\u001f\u007f]/.test(value))
  ) return null;
  return values;
}

export async function POST(request: Request): Promise<Response> {
  const context = await openWorkspaceMutation(request);
  if (context.kind === "error") return workspaceFailure(context.status);
  const form = await exactWorkspaceForm(
    request,
    ["operation", "user_id", "name", "site_id", "connection_id"],
    ["operation", "user_id"],
    ["site_id", "connection_id"],
  );
  if (!form) return workspaceFailure(400);
  const userId = textField(form, "user_id", 256);
  const operation = textField(form, "operation", 16);
  if (!userId?.trim()) return workspaceFailure(400);

  try {
    switch (operation) {
      case "add":
      case "remove": {
        if ([...form.keys()].some((key) => key !== "operation" && key !== "user_id")) {
          return workspaceFailure(400);
        }
        const result = operation === "add"
          ? await context.workspace.addMember({ user_id: userId })
          : await context.workspace.removeMember(userId);
        return success({ ok: true, ...result }, operation === "add" ? 201 : 200);
      }
      case "update": {
        if (form.has("name")) return workspaceFailure(400);
        const siteIds = identifiers(form, "site_id");
        const connectionIds = identifiers(form, "connection_id");
        if (siteIds === null || connectionIds === null) return workspaceFailure(400);
        const result = await context.workspace.setMemberGrants(userId, {
          site_ids: siteIds,
          connection_ids: connectionIds,
        });
        return success({ ok: true, ...result });
      }
      case "key": {
        if (form.has("connection_id")) return workspaceFailure(400);
        const name = textField(form, "name", 256)?.trim();
        const siteIds = identifiers(form, "site_id");
        if (!name || !siteIds?.length) return workspaceFailure(400);
        const result = await context.workspace.createMemberAgentKey(userId, {
          name,
          site_ids: siteIds,
        });
        return success({ ok: true, token: result.token, key: result.key }, 201);
      }
      default:
        return workspaceFailure(400);
    }
  } catch (error) {
    const status = error instanceof EnergyHttpError
      ? error.status === 401 || error.status === 403 ? error.status : 422
      : 502;
    return workspaceFailure(status);
  }
}
