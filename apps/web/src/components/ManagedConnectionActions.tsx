"use client";

import { useState } from "react";

type Action = "verify" | "disconnect";
type HealthStatus = "healthy" | "unhealthy";
type Feedback =
  | { kind: "idle" }
  | { kind: "confirm-disconnect" }
  | { kind: "pending"; action: Action }
  | { kind: "verified"; status: HealthStatus; checkedAt: string }
  | { kind: "error"; action: Action };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formattedTimestamp(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 64) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function ManagedConnectionActions({ connectionId, enabled, state }: {
  connectionId: string;
  enabled: boolean;
  state: string;
}) {
  const [feedback, setFeedback] = useState<Feedback>({ kind: "idle" });
  const pending = feedback.kind === "pending";
  const normalizedState = state.trim().toLowerCase();
  const canVerify = enabled && normalizedState === "active";
  const canDisconnect = !["disabled", "revoked"].includes(normalizedState);

  async function sendAction(action: Action) {
    if (pending || (action === "verify" && !canVerify) || (action === "disconnect" && !canDisconnect)) return;
    setFeedback({ kind: "pending", action });
    try {
      const response = await fetch("/api/workspace/connections/action", {
        method: "POST",
        body: new URLSearchParams({ connection_id: connectionId, action }),
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });
      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (!response.ok) throw new Error("Connection action failed.");
      if (action === "disconnect") {
        window.location.assign("/?view=connections");
        return;
      }

      const payload: unknown = await response.json();
      if (!isRecord(payload) || payload.ok !== true || payload.action !== "verify") {
        throw new Error("Connection action failed.");
      }
      const checkedAt = formattedTimestamp(payload.checked_at);
      if ((payload.status !== "healthy" && payload.status !== "unhealthy") || !checkedAt) {
        throw new Error("Connection action failed.");
      }
      setFeedback({ kind: "verified", status: payload.status, checkedAt });
    } catch {
      setFeedback({ kind: "error", action });
    }
  }

  return (
    <div className="managed-actions" aria-busy={pending}>
      {feedback.kind === "confirm-disconnect" ? (
        <div className="notice notice-neutral" role="group" aria-label="Confirm disconnect">
          <p>Remove this saved provider access? The gateway will stop using this connection.</p>
          <div className="inline-actions">
            <button className="button button-secondary" type="button" onClick={() => setFeedback({ kind: "idle" })}>Cancel</button>
            <button className="button button-danger" type="button" onClick={() => void sendAction("disconnect")}>Confirm disconnect</button>
          </div>
        </div>
      ) : (
        <div className="inline-actions">
          {canVerify ? (
            <button className="button button-secondary" type="button" disabled={pending} onClick={() => void sendAction("verify")}>
              {pending && feedback.kind === "pending" && feedback.action === "verify" ? "Checking…" : "Check connection"}
            </button>
          ) : null}
          {canDisconnect ? (
            <button className="button button-secondary" type="button" disabled={pending} onClick={() => setFeedback({ kind: "confirm-disconnect" })}>
              Disconnect
            </button>
          ) : null}
        </div>
      )}
      {pending ? <p className="mutation-feedback" role="status">Updating connection…</p> : null}
      {feedback.kind === "verified" ? (
        <p className="notice notice-neutral" role="status">
          <strong>{feedback.status === "healthy" ? "Connection is healthy" : "Connection needs attention"}</strong>
          Last checked {feedback.checkedAt}.
          {feedback.status === "unhealthy" ? <> Check your provider account and try again. If access was revoked, disconnect this connection and <a href="/?view=apps">connect the account again</a>.</> : null}
        </p>
      ) : null}
      {feedback.kind === "error" ? (
        <p className="notice notice-error" role="alert">
          {feedback.action === "verify" ? "The connection check could not be completed. Try again." : "The connection could not be disconnected. Try again."}
        </p>
      ) : null}
    </div>
  );
}
