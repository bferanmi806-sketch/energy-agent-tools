"use client";

import { useId, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Home, ShieldCheck } from "lucide-react";
import type { WorkspaceOAuthConfigurationsResponse } from "@energy-agent-tools/sdk";
import styles from "./ConnectionForm.module.css";

type Configuration = WorkspaceOAuthConfigurationsResponse["configurations"][number];
type AuthorizationState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "opened" }
  | { kind: "error"; reason: "instance" | "access" | "gateway" | "popup" };

const FLOW_STEPS = ["Select provider", "Home Assistant account", "Authorize and verify", "Map to a site"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function authorizationError(reason: "instance" | "access" | "gateway" | "popup"): string {
  switch (reason) {
    case "popup":
      return "Your browser blocked the authorization tab. Allow pop-ups for this gateway, then try again.";
    case "instance":
      return "This Home Assistant instance could not start authorization. Ask the gateway operator to check its approved OAuth setup.";
    case "access":
      return "Your gateway account cannot connect this Home Assistant instance. Sign in with a workspace management account.";
    case "gateway":
      return "Authorization could not be started. Check the gateway connection and try again.";
  }
}

export function HomeAssistantConnect({ configurations }: { configurations: Configuration[] }) {
  const router = useRouter();
  const formId = useId();
  const headingId = `${formId}-heading`;
  const [selectedId, setSelectedId] = useState(configurations[0]?.id ?? "");
  const [mappingReviewed, setMappingReviewed] = useState(false);
  const [authorization, setAuthorization] = useState<AuthorizationState>({ kind: "idle" });
  const selected = configurations.find((configuration) => configuration.id === selectedId) ?? configurations[0] ?? null;
  const activeStep = authorization.kind === "pending" || authorization.kind === "opened" ? 2 : 1;

  async function authorize(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (authorization.kind === "pending") return;
    const body = new URLSearchParams();
    for (const [name, value] of new FormData(event.currentTarget)) {
      if (typeof value !== "string") {
        setAuthorization({ kind: "error", reason: "gateway" });
        return;
      }
      body.append(name, value);
    }
    const providerTab = window.open("/connect/home-assistant", "_blank");
    if (!providerTab) {
      setAuthorization({ kind: "error", reason: "popup" });
      return;
    }
    providerTab.opener = null;
    setAuthorization({ kind: "pending" });
    try {
      const response = await fetch("/api/workspace/authorizations", {
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
          reason: response.status === 422 ? "instance" : response.status === 403 ? "access" : "gateway",
        });
        return;
      }

      const payload: unknown = await response.json();
      if (!isRecord(payload) || payload.ok !== true || typeof payload.authorization_url !== "string") {
        throw new Error("Authorization response is invalid.");
      }
      const destination = new URL(payload.authorization_url);
      const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(destination.hostname);
      if (
        destination.username || destination.password || destination.hash ||
        destination.pathname !== "/auth/authorize" ||
        !(destination.protocol === "https:" || destination.protocol === "http:" && loopback)
      ) {
        throw new Error("Authorization destination is invalid.");
      }
      if (providerTab.closed) throw new Error("Authorization tab was closed.");
      providerTab.location.replace(destination.toString());
      setAuthorization({ kind: "opened" });
    } catch {
      providerTab.close();
      setAuthorization({ kind: "error", reason: "gateway" });
    }
  }

  return (
    <section className={`${styles.homeAssistant} home-assistant-connect`} aria-labelledby={headingId}>
      <div className="home-assistant-heading-row">
        <span className="home-assistant-mark" aria-hidden="true"><Home size={17} strokeWidth={1.8} /></span>
        <div>
          <h2 id={headingId}>Home Assistant</h2>
          <p>Authorize one sensor through an instance approved by your gateway operator.</p>
        </div>
      </div>

      <ol className={`${styles.steps} ${styles.homeAssistantSteps}`} aria-label="Connection setup progress">
        {FLOW_STEPS.map((label, index) => {
          const state = index < activeStep ? "complete" : index === activeStep ? "current" : "upcoming";
          return (
            <li
              className={`${styles.step} ${state === "complete" ? styles.stepComplete : ""} ${state === "current" ? styles.stepCurrent : ""}`}
              key={label}
              aria-current={state === "current" ? "step" : undefined}
            >
              <span className={styles.stepNumber} aria-hidden="true">{state === "complete" ? "✓" : index + 1}</span>
              <span>{label}</span>
            </li>
          );
        })}
      </ol>

      {configurations.length === 0 ? (
        <div className="home-assistant-empty" role="status">
          <ShieldCheck size={15} aria-hidden="true" />
          <p>No Home Assistant instances are configured. Ask the gateway operator to add an approved instance and callback URL.</p>
        </div>
      ) : (
        <>
          <div className={styles.accountNote}>
            <p><strong>Connect your Home Assistant account.</strong> Choose an approved instance you own. Your gateway sign-in is separate; you will authorize with Home Assistant after choosing the instance.</p>
            <p>Enter one entity ID to limit this connection to a single sensor. The site mapping happens after Home Assistant verifies it.</p>
          </div>

          <div className="home-assistant-instance-list" role="group" aria-label="Configured Home Assistant instances">
            {configurations.map((configuration) => {
              const pendingCleanup = configuration.pending_cleanup ?? 0;
              const isSelected = selected?.id === configuration.id;
              return (
                <div className="home-assistant-instance-row" key={configuration.id}>
                  <button
                    className={`home-assistant-instance${isSelected ? " home-assistant-instance-selected" : ""}`}
                    type="button"
                    disabled={authorization.kind === "pending" || authorization.kind === "opened"}
                    aria-pressed={isSelected}
                    onClick={() => {
                      setSelectedId(configuration.id);
                      setMappingReviewed(false);
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
            <form className={`home-assistant-form ${styles.providerForm}`} action="/api/workspace/authorizations" method="post" onSubmit={authorize} aria-busy={authorization.kind === "pending"}>
              <fieldset disabled={authorization.kind === "pending" || authorization.kind === "opened"} className={styles.formContents}>
                <input type="hidden" name="configuration_id" value={selected.id} />
                <div className={`field-stack ${styles.providerField}`}>
                  <label htmlFor={`${formId}-entity`}>Home Assistant entity ID</label>
                  <input
                    id={`${formId}-entity`}
                    name="entity_id"
                    autoComplete="off"
                    maxLength={256}
                    pattern="[A-Za-z0-9_.:-]+"
                    placeholder="sensor.home_energy"
                    required
                    aria-describedby={`${formId}-entity-hint`}
                  />
                  <span className={styles.fieldHint} id={`${formId}-entity-hint`}>Only this entity is enrolled for the selected instance.</span>
                </div>

                <fieldset className="home-assistant-mapping">
                  <legend>Optional sensor mapping</legend>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      name="reviewed_mapping"
                      value="reviewed"
                      checked={mappingReviewed}
                      onChange={(event) => setMappingReviewed(event.currentTarget.checked)}
                    />
                    <span>I checked the sensor’s unit and measurement shape.</span>
                  </label>
                  <fieldset className="home-assistant-mapping-fields" disabled={!mappingReviewed}>
                    <div className="field-stack">
                      <label htmlFor={`${formId}-role`}>Telemetry role</label>
                      <select id={`${formId}-role`} name="telemetry_role" defaultValue="" required>
                        <option value="" disabled>Select a role</option>
                        <option value="consumption_interval">Interval consumption</option>
                        <option value="current_power">Current power</option>
                        <option value="generation">Generation</option>
                        <option value="export">Export</option>
                        <option value="storage_state">Storage state</option>
                      </select>
                    </div>
                    <div className="field-stack">
                      <label htmlFor={`${formId}-unit`}>Unit</label>
                      <select id={`${formId}-unit`} name="unit" defaultValue="" required>
                        <option value="" disabled>Select a unit</option>
                        <option value="kWh">kWh</option>
                        <option value="W">W</option>
                        <option value="kW">kW</option>
                        <option value="MW">MW</option>
                        <option value="%">%</option>
                      </select>
                    </div>
                    <div className="field-stack">
                      <label htmlFor={`${formId}-shape`}>Measurement shape</label>
                      <select id={`${formId}-shape`} name="quantity_shape" defaultValue="" required>
                        <option value="" disabled>Select a shape</option>
                        <option value="interval">Interval total</option>
                        <option value="instantaneous">Instantaneous reading</option>
                      </select>
                    </div>
                  </fieldset>
                  <p className="field-hint">This optional sensor mapping is separate from the site mapping you’ll complete after verification.</p>
                </fieldset>

                <button className="button button-primary home-assistant-submit" type="submit">
                  {authorization.kind === "pending" ? "Opening Home Assistant…" : "Authorize in a new tab"} <ArrowRight size={15} aria-hidden="true" />
                </button>
              </fieldset>
              {authorization.kind === "pending" ? <p className={styles.pendingMessage} role="status">Opening the approved Home Assistant instance.</p> : null}
              {authorization.kind === "opened" ? (
                <div className={styles.accountNote} role="status">
                  <p>Continue in the Home Assistant tab. After authorization, that tab returns here to verify the sensor and map your connection to a site.</p>
                  <button className="button button-secondary" type="button" onClick={() => router.refresh()}>Refresh connections</button>
                  <button className="button button-secondary" type="button" onClick={() => setAuthorization({ kind: "idle" })}>Start again if cancelled</button>
                </div>
              ) : null}
              {authorization.kind === "error" ? <p className="notice notice-error" role="alert">{authorizationError(authorization.reason)}</p> : null}
              <p className={styles.revokeNote}>The authorization request expires within an hour; if it does, start again here. After sensor verification, choose a site in Connections. Disconnecting turns off saved access immediately and asks Home Assistant to revoke its grant; if Home Assistant is unavailable, cleanup stays pending and can be retried here.</p>
            </form>
          ) : null}
        </>
      )}
    </section>
  );
}

type CleanupResult = { attempted: number; succeeded: number; pending: number };
type CleanupFeedback = { kind: "idle" } | { kind: "pending" } | { kind: "complete"; result: CleanupResult } | { kind: "error" };

function isCleanupResult(value: unknown): value is CleanupResult {
  if (!isRecord(value)) return false;
  const { attempted, succeeded, pending } = value;
  return typeof attempted === "number" && Number.isSafeInteger(attempted) && attempted >= 0 &&
    typeof succeeded === "number" && Number.isSafeInteger(succeeded) && succeeded >= 0 && succeeded <= attempted &&
    typeof pending === "number" && Number.isSafeInteger(pending) && pending >= 0;
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
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });
      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (!response.ok) throw new Error("Authorization cleanup failed.");
      const payload: unknown = await response.json();
      if (!isRecord(payload) || payload.ok !== true || !isRecord(payload.cleanup) || !isCleanupResult(payload.cleanup)) {
        throw new Error("Authorization cleanup failed.");
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
