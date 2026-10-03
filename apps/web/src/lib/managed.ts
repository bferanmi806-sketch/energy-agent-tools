import "server-only";

import { EnergyAgentTools, EnergyHttpError } from "@energy-agent-tools/sdk";
import type { IdentityResponse } from "@energy-agent-tools/sdk";
import { cookies } from "next/headers";
import {
  type WebRuntimeConfig,
  isAllowedMutationOrigin,
  openSession,
  readBoundedForm,
  readWebRuntimeConfig,
  SESSION_COOKIE_NAME,
} from "./security";

type WorkspaceClient = ReturnType<EnergyAgentTools["workspace"]>;

export type WorkspaceContext =
  | {
      kind: "ready";
      workspace: WorkspaceClient;
      identity: IdentityResponse;
      sessionCookie: string;
      config: WebRuntimeConfig;
    }
  | { kind: "error"; status: number };

export function workspaceFailure(status: number): Response {
  return Response.json(
    { ok: false, error: "workspace_request_failed" },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

export async function openWorkspaceMutation(request: Request): Promise<WorkspaceContext> {
  let config;
  try {
    config = readWebRuntimeConfig(process.env);
  } catch {
    return { kind: "error", status: 503 };
  }

  if (!isAllowedMutationOrigin(request.headers.get("origin"), config.webOrigin)) {
    return { kind: "error", status: 403 };
  }

  const cookieValue = (await cookies()).get(SESSION_COOKIE_NAME)?.value;
  return workspaceForSession(cookieValue, config);
}

export async function openWorkspaceForOAuthCallback(
  sessionCookie: string | undefined,
  config: WebRuntimeConfig,
): Promise<WorkspaceContext> {
  return workspaceForSession(sessionCookie, config);
}

async function workspaceForSession(
  cookieValue: string | undefined,
  config: WebRuntimeConfig,
): Promise<WorkspaceContext> {
  const session = openSession(cookieValue, config.sessionKey.toString("hex"));
  if (!session) return { kind: "error", status: 401 };
  if (!cookieValue) return { kind: "error", status: 401 };

  const gateway = new EnergyAgentTools({
    baseUrl: config.gatewayUrl,
    token: session.token,
    fetch: (input, init) => fetch(input, { ...init, cache: "no-store" }),
  });

  try {
    const identity = await gateway.identity();
    const managerWorkspace = identity.workspace;
    if (identity.can_manage_workspace !== true || managerWorkspace?.mode !== "managed") {
      return { kind: "error", status: 403 };
    }
    return {
      kind: "ready",
      workspace: gateway.workspace(),
      identity,
      sessionCookie: cookieValue,
      config,
    };
  } catch (error) {
    return { kind: "error", status: error instanceof EnergyHttpError && error.status === 401 ? 401 : 502 };
  }
}

export async function exactWorkspaceForm(
  request: Request,
  allowed: readonly string[],
  required: readonly string[],
  repeatable: readonly string[] = [],
): Promise<URLSearchParams | null> {
  const form = await readBoundedForm(request);
  if (!form) return null;

  const allowedNames = new Set(allowed);
  const repeatableNames = new Set(repeatable);
  if ([...form.keys()].some((name) => !allowedNames.has(name))) return null;

  for (const name of allowed) {
    const values = form.getAll(name);
    if ((required.includes(name) && values.length !== 1) || values.length > (repeatableNames.has(name) ? 256 : 1)) {
      return null;
    }
  }
  return form;
}

export function textField(form: URLSearchParams, name: string, maximum: number): string | null {
  const value = form.get(name);
  if (value === null || value.length === 0 || value.length > maximum || /[\u0000-\u001f\u007f]/.test(value)) {
    return null;
  }
  return value;
}

export function success(body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store" } });
}
