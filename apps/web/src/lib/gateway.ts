import "server-only";

import { EnergyAgentTools, EnergyHttpError } from "@energy-agent-tools/sdk";
import { cookies } from "next/headers";
import { unstable_noStore } from "next/cache";
import { readWebRuntimeConfig, openSession, SESSION_COOKIE_NAME } from "./security";
import type { DashboardState } from "./types";

const SIGNED_OUT_MESSAGE = "Your gateway session has expired. Sign in again.";
const UNAVAILABLE_MESSAGE = "The gateway is unavailable. Check the web and gateway configuration.";

function signedOut(message: string | null): DashboardState {
  return { kind: "signed-out", message };
}

function unavailable(): DashboardState {
  return { kind: "unavailable", message: UNAVAILABLE_MESSAGE };
}

function gatewayClient(baseUrl: string, token: string): EnergyAgentTools {
  return new EnergyAgentTools({
    baseUrl,
    token,
    fetch: (input, init) => fetch(input, { ...init, cache: "no-store" }),
  });
}

export async function loadDashboard(): Promise<DashboardState> {
  unstable_noStore();
  const cookieJar = await cookies();
  const cookieValue = cookieJar.get(SESSION_COOKIE_NAME)?.value;
  if (!cookieValue) return signedOut(null);

  let config;
  try {
    config = readWebRuntimeConfig(process.env);
  } catch {
    return unavailable();
  }

  const session = openSession(cookieValue, config.sessionKey.toString("hex"));
  if (!session) return signedOut(SIGNED_OUT_MESSAGE);

  try {
    const gateway = gatewayClient(config.gatewayUrl, session.token);
    const identity = await gateway.identity();

    if (identity.can_manage_workspace === true && identity.workspace?.mode === "managed") {
      const workspace = gateway.workspace();
      const results = await Promise.allSettled([
        workspace.details(),
        workspace.sites(),
        workspace.assets(),
        workspace.toolkits(),
        workspace.connectionSetups(),
        workspace.connections(),
        workspace.keys(),
        workspace.authConfigurations(),
        workspace.members(),
        workspace.skills(),
      ]);

      return {
        kind: "ready",
        data: {
          kind: "managed",
          identity,
          workspace: settledValue(results[0]).workspace,
          sites: settledValue(results[1]).sites,
          assets: settledValue(results[2]).assets,
          toolkits: settledValue(results[3]).toolkits,
          connectionSetups: settledValue(results[4]).setups,
          connections: settledValue(results[5]).connections,
          keys: settledValue(results[6]).keys,
          authConfigurations: settledValue(results[7]).configurations,
          members: settledValue(results[8]).members,
          skills: settledValue(results[9]).skills,
          publicGatewayUrl: config.publicGatewayUrl,
        },
      };
    }

    const availableSites = identity.workspace?.mode === "managed"
      ? identity.sites
      : identity.sites.filter((site) => site.user_id === identity.user_id);
    const selectedSite = availableSites.find((site) => site.id === session.siteId) ?? availableSites[0];
    const siteId = selectedSite?.id ?? null;
    const scopedSession = await gateway.createSession({ site_id: siteId });

    try {
      const results = await Promise.allSettled([
        scopedSession.toolkits(),
        scopedSession.connections(),
        scopedSession.skills(),
        scopedSession.artifacts(),
        scopedSession.connectionSetups(),
      ]);
      const toolkits = settledValue(results[0]);
      const connections = settledValue(results[1]);
      const skills = settledValue(results[2]);
      const artifacts = settledValue(results[3]);
      return {
        kind: "ready",
        data: {
          kind: "operator",
          identity,
          siteId,
          connectionSetups: settledValue(results[4]).setups,
          toolkits: toolkits.toolkits,
          connections: connections.connections,
          skills: skills.skills,
          artifacts: artifacts.artifacts,
          publicGatewayUrl: config.publicGatewayUrl,
        },
      };
    } finally {
      await scopedSession.close().catch(() => undefined);
    }
  } catch (error) {
    if (error instanceof EnergyHttpError && (error.status === 401 || error.status === 403)) {
      return signedOut(SIGNED_OUT_MESSAGE);
    }
    return unavailable();
  }
}

function settledValue<T>(result: PromiseSettledResult<T> | undefined): T {
  if (!result || result.status === "rejected") throw new Error("A dashboard gateway request failed.");
  return result.value;
}
