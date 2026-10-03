"use client";

import { useId, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Home, ShieldCheck } from "lucide-react";
import type { WorkspaceOAuthConfigurationsResponse } from "@energy-agent-tools/sdk";

type Configuration = WorkspaceOAuthConfigurationsResponse["configurations"][number];

export function HomeAssistantConnect({ configurations }: { configurations: Configuration[] }) {
  const formId = useId();
  const [selectedId, setSelectedId] = useState(configurations[0]?.id ?? "");
  const [mappingReviewed, setMappingReviewed] = useState(false);
  const [authorization, setAuthorization] = useState<"idle" | "pending" | "error">("idle");
  const selected = configurations.find((configuration) => configuration.id === selectedId) ?? null;

  async function authorize(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (authorization === "pending") return;
    const body = new URLSearchParams();
    for (const [name, value] of new FormData(event.currentTarget)) {
      if (typeof value !== "string") { setAuthorization("error"); return; }
      body.append(name, value);
    }
    setAuthorization("pending");
    try {
      const response = await fetch("/api/workspace/authorizations", {
        method: "POST", body, credentials: "same-origin", cache: "no-store",
        signal: AbortSignal.timeout(30_000),
      });
      if (response.status === 401) { window.location.assign("/"); return; }
      if (!response.ok) throw new Error("Authorization refused.");
      const payload: unknown = await response.json();
      if (!isRecord(payload) || payload.ok !== true || typeof payload.authorization_url !== "string") {
        throw new Error("Authorization response is invalid.");
      }
      const destination = new URL(payload.authorization_url);
      const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(destination.hostname);
      if (destination.username || destination.password || destination.hash ||
          destination.pathname !== "/auth/authorize" ||
          !(destination.protocol === "https:" || destination.protocol === "http:" && loopback)) {
        throw new Error("Authorization destination is invalid.");
      }
      window.location.assign(destination.toString());
    } catch { setAuthorization("error"); }
  }

  return (
    <section className="home-assistant-connect" aria-labelledby="home-assistant-heading">
      <div className="home-assistant-heading-row">
        <span className="home-assistant-mark" aria-hidden="true"><Home size={17} strokeWidth={1.8} /></span>
        <div>
          <h2 id="home-assistant-heading">Home Assistant</h2>
          <p>Choose an operator-approved instance, then authorize one sensor.</p>
        </div>
      </div>

      {configurations.length === 0 ? (
        <div className="home-assistant-empty">
          <ShieldCheck size={15} aria-hidden="true" />
          <p>No Home Assistant instances are configured. Ask the gateway operator to add an approved instance and callback URL.</p>
        </div>
      ) : (
        <>
          <div className="home-assistant-instance-list" role="group" aria-label="Configured Home Assistant instances">
            {configurations.map((configuration) => {
              const pendingCleanup = configuration.pending_cleanup ?? 0;
              return (
                <div className="home-assistant-instance-row" key={configuration.id}>
                  <button
                    className={`home-assistant-instance${selectedId === configuration.id ? " home-assistant-instance-selected" : ""}`}
                    type="button"
                    disabled={authorization === "pending"}
                    aria-pressed={selectedId === configuration.id}
                    onClick={() => {
                      setSelectedId(configuration.id);
                      setMappingReviewed(false);
                    }}
                  >
                    <span>{configuration.name}</span>
                    <span>{selectedId === configuration.id ? "Selected" : "Choose"}</span>
                  </button>
                  {pendingCleanup > 0 ? (
                    <AuthorizationCleanupAction configurationId={configuration.id} pendingCount={pendingCleanup} />
                  ) : null}
                </div>
              );
            })}
          </div>

          {selected ? (
            <form className="home-assistant-form" action="/api/workspace/authorizations" method="post" onSubmit={authorize} aria-busy={authorization === "pending"}>
              <fieldset disabled={authorization === "pending"} style={{display:"contents"}}>
              <input type="hidden" name="configuration_id" value={selected.id} />
              <div className="field-stack">
                <label htmlFor={`${formId}-entity`}>Home Assistant entity ID</label>
                <input
                  id={`${formId}-entity`}
                  name="entity_id"
                  autoComplete="off"
                  maxLength={256}
                  pattern="[A-Za-z0-9_.:-]+"
                  placeholder="sensor.home_energy"
                  required
                />
                <span className="field-hint">Only this entity is enrolled for the selected instance.</span>
              </div>

              <fieldset className="home-assistant-mapping">
                <legend>Optional telemetry mapping</legend>
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
                <p className="field-hint">The gateway records an explicit metered mapping only when you confirm the sensor details.</p>
              </fieldset>

              <button className="button button-primary home-assistant-submit" type="submit">
                {authorization === "pending" ? "Opening Home Assistant…" : "Authorize with Home Assistant"} <ArrowRight size={15} aria-hidden="true" />
              </button>
              </fieldset>
              {authorization === "pending" ? <p className="mutation-feedback" role="status">Preparing a secure authorization request.</p> : null}
              {authorization === "error" ? <p className="notice notice-error" role="alert">Authorization could not be started. Check the gateway and try again.</p> : null}
            </form>
          ) : null}
        </>
      )}
    </section>
  );
}

type CleanupResult = { attempted: number; succeeded: number; pending: number };
type CleanupFeedback = { kind: "idle" } | { kind: "pending" } | { kind: "complete"; result: CleanupResult } | { kind: "error" };

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
