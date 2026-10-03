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

type ParsedResponse =
  | { kind: "verified"; status: HealthStatus; checkedAt: string }
  | { kind: "disconnected" };

type ConnectionActionsProps = {
  connectionId: string;
  enabled: boolean;
  state: string;
  canDisconnect: boolean;
};

const checkedAtPattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatCheckedAt(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 64 || !checkedAtPattern.test(value)) return null;

  const checkedAt = new Date(value);
  if (!Number.isFinite(checkedAt.getTime())) return null;

  return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(checkedAt);
}

function parseResponse(value: unknown, action: Action): ParsedResponse | null {
  if (!isRecord(value) || value.ok !== true || value.action !== action) return null;

  switch (action) {
    case "verify": {
      const status = value.status;
      if (status !== "healthy" && status !== "unhealthy") return null;
      const checkedAt = formatCheckedAt(value.checked_at);
      return checkedAt ? { kind: "verified", status, checkedAt } : null;
    }
    case "disconnect":
      return { kind: "disconnected" };
    default: {
      const exhaustive: never = action;
      return exhaustive;
    }
  }
}

export function ConnectionActions({ connectionId, enabled, state, canDisconnect }: ConnectionActionsProps) {
  const [feedback, setFeedback] = useState<Feedback>({ kind: "idle" });
  const unavailable = !enabled || ["disabled", "revoked"].includes(state.trim().toLowerCase());
  const pending = feedback.kind === "pending";

  async function sendAction(action: Action) {
    if (unavailable || pending) return;

    setFeedback({ kind: "pending", action });

    try {
      const body = new URLSearchParams();
      body.append("connection_id", connectionId);
      body.append("action", action);

      const response = await fetch("/api/connections/action", {
        method: "POST",
        body,
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });

      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (response.status !== 200) throw new Error("Action failed.");

      const payload: unknown = await response.json();
      const result = parseResponse(payload, action);
      if (!result) throw new Error("Action failed.");

      if (result.kind === "disconnected") {
        window.location.reload();
        return;
      }

      setFeedback(result);
    } catch {
      setFeedback({ kind: "error", action });
    }
  }

  return (
    <div
      aria-busy={pending}
      style={{ display: "grid", gap: 8, justifyItems: "start", maxWidth: "28rem" }}
    >
      {unavailable ? (
        <p className="notice notice-neutral" role="status" style={{ margin: 0 }}>
          {canDisconnect ? "This connection needs reconnecting. Add the account again from app setup to restore access." : "Use a workspace management key to reconnect this account."}
        </p>
      ) : (
        <>
          {feedback.kind === "confirm-disconnect" ? (
            <div
              className="notice notice-neutral"
              role="group"
              aria-label="Confirm disconnect"
              style={{ display: "grid", maxWidth: "28rem", width: "100%" }}
            >
              <p>Disconnecting removes this saved access key. To reconnect, you’ll enter it again.</p>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                <button
                  className="button button-secondary"
                  type="button"
                  onClick={() => setFeedback({ kind: "idle" })}
                  aria-label="Cancel disconnect"
                >
                  Cancel
                </button>
                <button
                  className="button button-primary"
                  type="button"
                  onClick={() => void sendAction("disconnect")}
                  aria-label="Confirm disconnect"
                >
                  Confirm disconnect
                </button>
              </div>
            </div>
          ) : (
            <div role="group" aria-label="Connection actions" style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              <button
                className="button button-secondary"
                type="button"
                disabled={pending}
                onClick={() => void sendAction("verify")}
                aria-label="Check connection"
              >
                Check connection
              </button>
              {canDisconnect ? <button
                className="button button-secondary"
                type="button"
                disabled={pending}
                onClick={() => setFeedback({ kind: "confirm-disconnect" })}
                aria-label="Disconnect connection"
              >
                Disconnect
              </button> : null}
            </div>
          )}

          {feedback.kind === "pending" ? (
            <p className="notice notice-neutral" role="status" style={{ margin: 0 }}>
              {feedback.action === "verify" ? "Checking connection…" : "Disconnecting connection…"}
            </p>
          ) : null}
          {feedback.kind === "verified" ? (
            <p className="notice notice-neutral" role="status" style={{ margin: 0 }}>
              <strong>{feedback.status === "healthy" ? "Connection is healthy" : "Connection needs attention"}</strong>{" "}
              Last checked {feedback.checkedAt}.
            </p>
          ) : null}
          {feedback.kind === "error" ? (
            <p className="notice notice-error" role="status" style={{ margin: 0 }}>
              {feedback.action === "verify"
                ? "The connection check could not be completed. Try again."
                : "The connection could not be disconnected. Try again."}
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
