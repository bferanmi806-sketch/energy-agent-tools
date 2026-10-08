"use client";

import { useId, useMemo, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, ShieldCheck, Sun, Zap } from "lucide-react";
import type { WorkspaceOAuthConfigurationsResponse } from "@energy-agent-tools/sdk";
import styles from "./ConnectionForm.module.css";

type Configuration = WorkspaceOAuthConfigurationsResponse["configurations"][number];
export type EnergyProvider = "tesla-energy" | "enphase-energy";
type AuthorizationErrorReason = "application" | "access" | "gateway" | "popup";
type AuthorizationState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "opened" }
  | { kind: "error"; reason: AuthorizationErrorReason };
type CleanupResult = { attempted: number; succeeded: number; pending: number };
type CleanupFeedback =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "complete"; result: CleanupResult }
  | { kind: "error" };

const PROVIDERS = {
  "tesla-energy": {
    label: "Tesla Energy",
    resourceLabel: "Tesla energy site ID",
    resourceHint: "Use the site ID for the energy site you own or manage.",
    placeholder: "1234567890",
    host: "auth.tesla.com",
    path: "/oauth2/v3/authorize",
    accountUrl: "https://www.tesla.com/teslaaccount",
    accountLabel: "Tesla Account",
  },
  "enphase-energy": {
    label: "Enphase Energy",
    resourceLabel: "Enphase system ID",
    resourceHint: "Use the system ID for the Enphase installation you own or manage.",
    placeholder: "1234567",
    host: "api.enphaseenergy.com",
    path: "/oauth/authorize",
    accountUrl: "https://enlighten.enphaseenergy.com/",
    accountLabel: "Enphase Enlighten",
  },
} satisfies Record<EnergyProvider, {
  label: string;
  resourceLabel: string;
  resourceHint: string;
  placeholder: string;
  host: string;
  path: string;
  accountUrl: string;
  accountLabel: string;
}>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCleanupResult(value: unknown): value is CleanupResult {
  if (!isRecord(value)) return false;
  const { attempted, succeeded, pending } = value;
  return typeof attempted === "number" && Number.isSafeInteger(attempted) && attempted >= 0 &&
    typeof succeeded === "number" && Number.isSafeInteger(succeeded) && succeeded >= 0 && succeeded <= attempted &&
    typeof pending === "number" && Number.isSafeInteger(pending) && pending >= 0;
}

function isEnergyConfiguration(configuration: Configuration, provider: EnergyProvider): boolean {
  return configuration.toolkit === provider && configuration.protocol === "oauth2_confidential";
}

function authorizationError(reason: AuthorizationErrorReason, providerName: string): string {
  switch (reason) {
    case "popup":
      return "Your browser blocked the authorization tab. Allow pop-ups for this gateway, then try again.";
    case "application":
      return `The ${providerName} authorization could not start. Check the selected application and resource ID, then try again.`;
    case "access":
      return "Your gateway account cannot connect this provider system. Sign in with a workspace management account.";
    case "gateway":
      return "Authorization could not be started. Check the gateway connection and try again.";
  }
}

function approvedAuthorizationUrl(value: string, provider: EnergyProvider): URL | null {
  try {
    const destination = new URL(value);
    const expected = PROVIDERS[provider];
    if (
      destination.protocol !== "https:" ||
      destination.hostname !== expected.host ||
      destination.port !== "" ||
      destination.pathname !== expected.path ||
      destination.username !== "" ||
      destination.password !== "" ||
      destination.hash !== ""
    ) return null;
    return destination;
  } catch {
    return null;
  }
}

export function CloudOAuthConnect({
  provider,
  configurations,
}: {
  provider: EnergyProvider;
  configurations: Configuration[];
}) {
  const router = useRouter();
  const formId = useId();
  const headingId = `${formId}-heading`;
  const details = PROVIDERS[provider];
  const Mark = provider === "tesla-energy" ? Zap : Sun;
  const providerConfigurations = useMemo(
    () => configurations.filter((configuration) => isEnergyConfiguration(configuration, provider)),
    [configurations, provider],
  );
  const [selectedId, setSelectedId] = useState(providerConfigurations[0]?.id ?? "");
  const [authorization, setAuthorization] = useState<AuthorizationState>({ kind: "idle" });
  const selected = providerConfigurations.find((configuration) => configuration.id === selectedId)
    ?? providerConfigurations[0]
    ?? null;

  async function authorize(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (authorization.kind === "pending" || authorization.kind === "opened" || !selected) return;

    const body = new URLSearchParams();
    for (const [name, value] of new FormData(event.currentTarget)) {
      if (typeof value !== "string") {
        setAuthorization({ kind: "error", reason: "gateway" });
        return;
      }
      body.append(name, value);
    }
    const resourceId = body.get("resource_id")?.trim() ?? "";
    if (!/^[0-9]{1,32}$/.test(resourceId)) {
      setAuthorization({ kind: "error", reason: "application" });
      return;
    }
    body.set("resource_id", resourceId);

    const providerTab = window.open("/connect/energy-provider", "_blank");
    if (!providerTab) {
      setAuthorization({ kind: "error", reason: "popup" });
      return;
    }
    providerTab.opener = null;
    setAuthorization({ kind: "pending" });

    try {
      const response = await fetch("/api/workspace/provider-authorizations", {
        method: "POST",
        body,
        credentials: "same-origin",
        cache: "no-store",
        signal: AbortSignal.timeout(30_000),
      });
      if (response.status === 401) {
        providerTab.close();
        window.location.assign("/");
        return;
      }
      if (!response.ok) {
        providerTab.close();
        setAuthorization({
          kind: "error",
          reason: response.status === 400 || response.status === 422 ? "application" : response.status === 403 ? "access" : "gateway",
        });
        return;
      }

      const payload: unknown = await response.json();
      if (!isRecord(payload) || payload.ok !== true || typeof payload.authorization_url !== "string") {
        throw new Error("Authorization response is invalid.");
      }
      const destination = approvedAuthorizationUrl(payload.authorization_url, provider);
      if (!destination || providerTab.closed) throw new Error("Authorization destination is invalid.");

      providerTab.location.replace(destination.toString());
      setAuthorization({ kind: "opened" });
    } catch {
      providerTab.close();
      setAuthorization({ kind: "error", reason: "gateway" });
    }
  }

  return (
    <section className="home-assistant-connect cloud-oauth-connect" aria-labelledby={headingId}>
      <div className="home-assistant-heading-row">
        <span className="home-assistant-mark" aria-hidden="true"><Mark size={17} strokeWidth={1.8} /></span>
        <div>
          <h2 id={headingId}>{details.label}</h2>
          <p>Choose an operator-approved application and connect your own {details.label} account.</p>
        </div>
      </div>

      {providerConfigurations.length === 0 ? (
        <div className="home-assistant-empty" role="status">
          <ShieldCheck size={15} aria-hidden="true" />
          <p>No approved {details.label} applications are available in this workspace. Ask your gateway operator to configure one before connecting.</p>
        </div>
      ) : (
        <>
          <div className={styles.accountNote}>
            <p><strong>Sign in with {details.label}.</strong> Energy Agent Tools will send you to the provider to review and approve access. It does not ask for your provider password.</p>
            <p>Enter the provider resource ID here. After {details.label} verifies access, return to Connections to map it to a site.</p>
          </div>

          <div className="home-assistant-instance-list" role="group" aria-label={`Approved ${details.label} applications`}>
            {providerConfigurations.map((configuration) => {
              const isSelected = selected?.id === configuration.id;
              const pendingCleanup = configuration.pending_cleanup ?? 0;
              return (
                <div className="home-assistant-instance-row" key={configuration.id}>
                  <button
                    className={`home-assistant-instance${isSelected ? " home-assistant-instance-selected" : ""}`}
                    type="button"
                    disabled={authorization.kind === "pending" || authorization.kind === "opened"}
                    aria-pressed={isSelected}
                    onClick={() => {
                      setSelectedId(configuration.id);
                      setAuthorization({ kind: "idle" });
                    }}
                  >
                    <span>{configuration.name}</span>
                    <span>{isSelected ? "Selected" : "Choose"}</span>
                  </button>
                  {pendingCleanup > 0 ? (
                    <AuthorizationCleanupAction configurationId={configuration.id} pendingCount={pendingCleanup} />
                  ) : null}
                </div>
              );
            })}
          </div>

          {selected ? (
            <form
              className={`home-assistant-form ${styles.providerForm}`}
              action="/api/workspace/provider-authorizations"
              method="post"
              onSubmit={authorize}
              aria-busy={authorization.kind === "pending"}
            >
              <fieldset disabled={authorization.kind === "pending" || authorization.kind === "opened"} className={styles.formContents}>
                <input type="hidden" name="configuration_id" value={selected.id} />
                <div className={`field-stack ${styles.providerField}`}>
                  <label htmlFor={`${formId}-resource`}>{details.resourceLabel}</label>
                  <input
                    id={`${formId}-resource`}
                    name="resource_id"
                    autoComplete="off"
                    maxLength={32}
                    pattern="[0-9]{1,32}"
                    placeholder={details.placeholder}
                    required
                    aria-describedby={`${formId}-resource-hint`}
                  />
                  <span className={styles.fieldHint} id={`${formId}-resource-hint`}>{details.resourceHint}</span>
                </div>

                <button className="button button-primary home-assistant-submit" type="submit">
                  {authorization.kind === "pending" ? `Opening ${details.label}…` : `Authorize with ${details.label}`} <ArrowRight size={15} aria-hidden="true" />
                </button>
              </fieldset>
              {authorization.kind === "pending" ? <p className={styles.pendingMessage} role="status">Preparing the approved {details.label} authorization in a new tab.</p> : null}
              {authorization.kind === "opened" ? (
                <div className={styles.accountNote} role="status">
                  <p>Continue in the {details.label} tab. After approving access, return here and refresh Connections to map the verified system to a site.</p>
                  <button className="button button-secondary" type="button" onClick={() => router.push("/?view=connections")}>Open Connections</button>
                  <button className="button button-secondary" type="button" onClick={() => router.refresh()}>Refresh dashboard</button>
                  <button className="button button-secondary" type="button" onClick={() => setAuthorization({ kind: "idle" })}>Start again if cancelled</button>
                </div>
              ) : null}
              {authorization.kind === "error" ? <p className="notice notice-error" role="alert">{authorizationError(authorization.reason, details.label)}</p> : null}
              <p className={styles.revokeNote}>
                Disconnect removes gateway access. To remove the provider grant too, revoke Energy Agent Tools in your {details.label} account at{" "}
                <a href={details.accountUrl} target="_blank" rel="noopener noreferrer">{details.accountLabel}</a>.
              </p>
            </form>
          ) : null}
        </>
      )}
    </section>
  );
}

function AuthorizationCleanupAction({ configurationId, pendingCount }: { configurationId: string; pendingCount: number }) {
  const router = useRouter();
  const [feedback, setFeedback] = useState<CleanupFeedback>({ kind: "idle" });
  const busy = feedback.kind === "pending";

  async function retryCleanup() {
    if (busy) return;
    setFeedback({ kind: "pending" });
    try {
      const response = await fetch("/api/workspace/authorizations/cleanup", {
        method: "POST",
        body: new URLSearchParams({ configuration_id: configurationId }),
        credentials: "same-origin",
        cache: "no-store",
      });
      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (!response.ok) throw new Error("Authorization cleanup failed.");
      const payload: unknown = await response.json();
      if (!isRecord(payload) || payload.ok !== true || !isRecord(payload.cleanup) || !isCleanupResult(payload.cleanup)) {
        throw new Error("Authorization cleanup response is invalid.");
      }
      setFeedback({ kind: "complete", result: payload.cleanup });
      router.refresh();
    } catch {
      setFeedback({ kind: "error" });
    }
  }

  return (
    <div className="home-assistant-cleanup" aria-busy={busy}>
      <p className="home-assistant-cleanup-count">
        {pendingCount} authorization {pendingCount === 1 ? "grant is" : "grants are"} waiting for cleanup.
      </p>
      <button className="button button-secondary home-assistant-cleanup-button" type="button" disabled={busy} onClick={() => void retryCleanup()}>
        {busy ? "Retrying cleanup…" : "Retry authorization cleanup"}
      </button>
      {feedback.kind === "complete" ? (
        <p className="mutation-feedback" role="status">
          Cleanup tried {feedback.result.attempted} {feedback.result.attempted === 1 ? "grant" : "grants"}, removed {feedback.result.succeeded}, and left {feedback.result.pending} pending.
        </p>
      ) : null}
      {feedback.kind === "error" ? (
        <p className="notice notice-error" role="alert">Authorization cleanup could not be completed. Try again.</p>
      ) : null}
    </div>
  );
}
