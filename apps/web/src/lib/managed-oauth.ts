import type { WorkspaceAuthorizationResponse } from "@energy-agent-tools/sdk";
import { OAUTH_CALLBACK_PATH, type WebOrigin } from "./security";

const MAX_AUTHORIZATION_URL_CHARS = 4096;

function isLoopback(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return normalized === "localhost" || normalized.endsWith(".localhost") || normalized === "127.0.0.1" || normalized === "::1";
}

export function approvedHomeAssistantAuthorizationUrl(
  authorization: WorkspaceAuthorizationResponse["authorization"],
  webOrigin: WebOrigin,
  production: boolean,
): URL | null {
  if (
    authorization.state.length === 0 || authorization.state.length > 512 ||
    /[\u0000-\u0020\u007f]/.test(authorization.state) ||
    authorization.authorization_url.length === 0 ||
    authorization.authorization_url.length > MAX_AUTHORIZATION_URL_CHARS
  ) return null;

  let destination: URL;
  try {
    destination = new URL(authorization.authorization_url);
  } catch {
    return null;
  }

  const callback = new URL(OAUTH_CALLBACK_PATH, `${webOrigin.origin}/`);
  const transportAllowed = destination.protocol === "https:" ||
    (destination.protocol === "http:" && (!production || isLoopback(destination.hostname)));
  if (
    !transportAllowed ||
    destination.username !== "" ||
    destination.password !== "" ||
    destination.hash !== "" ||
    destination.pathname !== "/auth/authorize"
  ) return null;

  const names = [...destination.searchParams.keys()];
  if (
    names.length !== 3 ||
    names.some((name) => !["client_id", "redirect_uri", "state"].includes(name)) ||
    destination.searchParams.getAll("state").length !== 1 ||
    destination.searchParams.getAll("client_id").length !== 1 ||
    destination.searchParams.getAll("redirect_uri").length !== 1 ||
    destination.searchParams.get("state") !== authorization.state ||
    destination.searchParams.get("redirect_uri") !== callback.toString()
  ) return null;

  const clientId = destination.searchParams.get("client_id");
  if (!clientId) return null;
  try {
    const client = new URL(clientId);
    if (
      client.origin !== callback.origin ||
      client.username !== "" || client.password !== "" || client.search !== "" || client.hash !== ""
    ) return null;
  } catch {
    return null;
  }

  return destination;
}

export function approvedCloudAuthorizationUrl(
  authorization: WorkspaceAuthorizationResponse["authorization"],
  toolkit: "tesla-energy" | "enphase-energy",
  webOrigin: WebOrigin,
): URL | null {
  if (!authorization.state || authorization.state.length > 512 || /[\u0000-\u0020\u007f]/.test(authorization.state) || authorization.authorization_url.length > MAX_AUTHORIZATION_URL_CHARS) return null;
  let destination: URL;
  try { destination = new URL(authorization.authorization_url); } catch { return null; }
  const expected = toolkit === "tesla-energy"
    ? "https://auth.tesla.com/oauth2/v3/authorize"
    : "https://api.enphaseenergy.com/oauth/authorize";
  const endpoint = new URL(expected);
  if (destination.origin !== endpoint.origin || destination.pathname !== endpoint.pathname || destination.username || destination.password || destination.hash) return null;
  const allowed = ["response_type", "client_id", "redirect_uri", "state", ...(toolkit === "tesla-energy" ? ["scope"] : [])];
  const names = [...destination.searchParams.keys()];
  if (names.length !== allowed.length || names.some(name => !allowed.includes(name)) || allowed.some(name => destination.searchParams.getAll(name).length !== 1)) return null;
  if (destination.searchParams.get("response_type") !== "code" || !destination.searchParams.get("client_id") || destination.searchParams.get("state") !== authorization.state || destination.searchParams.get("redirect_uri") !== new URL(OAUTH_CALLBACK_PATH, `${webOrigin.origin}/`).toString()) return null;
  if (toolkit === "tesla-energy" && destination.searchParams.get("scope") !== "openid offline_access energy_device_data") return null;
  return destination;
}
