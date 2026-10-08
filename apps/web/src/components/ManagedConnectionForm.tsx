"use client";

import { useId, useState, type FormEvent } from "react";
import type { ConnectionSetupsResponse } from "@energy-agent-tools/sdk";
import styles from "./ConnectionForm.module.css";

type ConnectionSetup = ConnectionSetupsResponse["setups"][number];
type Submission =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error"; reason: "details" | "access" | "gateway" }
  | { kind: "success" };

const FLOW_STEPS = ["Select provider", "Provider credentials", "Verify account", "Map to a site"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function errorMessage(reason: "details" | "access" | "gateway"): string {
  switch (reason) {
    case "details":
      return "Octopus could not verify these details. Check the API key, MPAN, and meter serial number, then try again.";
    case "access":
      return "This gateway account cannot add provider connections. Sign in with a workspace management account.";
    case "gateway":
      return "The gateway could not complete the connection check. Check that it is available, then try again.";
  }
}

export function ManagedConnectionForm({ setup }: { setup: ConnectionSetup }) {
  const formId = useId();
  const [submission, setSubmission] = useState<Submission>({ kind: "idle" });
  const activeStep = submission.kind === "success" ? 3 : submission.kind === "pending" ? 2 : 1;
  const disabled = !setup.enabled || setup.fields.length === 0 || submission.kind === "pending" || submission.kind === "success";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled) return;

    const form = event.currentTarget;
    const values = new FormData(form);
    const body = new URLSearchParams();
    const provider = values.get("provider");
    let valid = provider === setup.provider;
    if (valid) body.append("provider", setup.provider);

    for (const field of setup.fields) {
      const value = values.get(field.name);
      if (
        typeof value !== "string" ||
        value.length < field.min_length ||
        value.length > field.max_length
      ) {
        valid = false;
        continue;
      }
      body.append(field.name, value);
    }

    if (!valid) {
      setSubmission({ kind: "error", reason: "details" });
      return;
    }

    setSubmission({ kind: "pending" });
    try {
      const response = await fetch("/api/workspace/connections", {
        method: "POST",
        body,
        credentials: "same-origin",
        cache: "no-store",
        signal: AbortSignal.timeout(30_000),
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });
      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (!response.ok) {
        setSubmission({
          kind: "error",
          reason: response.status === 422 ? "details" : response.status === 403 ? "access" : "gateway",
        });
        return;
      }

      const payload: unknown = await response.json();
      if (
        !isRecord(payload) || payload.ok !== true ||
        typeof payload.connection_id !== "string" || payload.connection_id.length === 0 ||
        payload.state !== "pending_mapping"
      ) {
        setSubmission({ kind: "error", reason: "gateway" });
        return;
      }

      for (const input of form.querySelectorAll<HTMLInputElement>('input[type="password"]')) {
        input.value = "";
      }
      setSubmission({ kind: "success" });
      window.location.assign("/?view=connections");
    } catch {
      setSubmission({ kind: "error", reason: "gateway" });
    }
  }

  return (
    <form
      className={`signin-form managed-connect-form ${styles.providerForm}`}
      onSubmit={submit}
      aria-busy={submission.kind === "pending"}
    >
      <input type="hidden" name="provider" value={setup.provider} />
      <ol className={`${styles.steps} ${styles.managedSteps}`} aria-label="Connection setup progress">
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

      <div className={`${styles.accountNote} ${styles.managedAccountNote}`}>
        <p><strong>Connect your Octopus account.</strong> Enter the API key from your provider account and the electricity meter details to verify access.</p>
        <p>Your gateway sign-in is separate from your Octopus API key. The gateway stores the key only after Octopus verifies it.</p>
      </div>

      <fieldset disabled={disabled}>
        {setup.fields.map((field) => {
          const id = `${formId}-${field.name}`;
          const hintId = field.secret ? `${id}-hint` : undefined;
          return (
            <div className={`field-stack ${styles.providerField}`} key={field.name}>
              <label htmlFor={id}>{field.label}</label>
              <input
                id={id}
                name={field.name}
                type={field.secret ? "password" : "text"}
                autoComplete="off"
                required
                minLength={field.min_length}
                maxLength={field.max_length}
                pattern={field.pattern ?? undefined}
                spellCheck={false}
                aria-describedby={hintId}
              />
              {field.secret ? <span className={styles.fieldHint} id={hintId}>Provider credential; never use your gateway access key here.</span> : null}
            </div>
          );
        })}
        <button className="button button-primary" type="submit">
          {submission.kind === "pending" ? "Verifying with Octopus…" : "Verify and connect account"}
        </button>
      </fieldset>

      {!setup.enabled ? (
        <p className="notice notice-neutral" role="status">
          {setup.unavailable_reason === "storage_unavailable"
            ? "Encrypted connection storage is unavailable. Ask the gateway operator to enable it."
            : "This gateway needs a workspace management account before it can connect provider accounts."}
        </p>
      ) : null}
      {setup.fields.length === 0 ? (
        <p className="notice notice-neutral" role="status">The gateway has not published the required Octopus account fields.</p>
      ) : null}
      {submission.kind === "pending" ? (
        <p className={styles.pendingMessage} role="status">The gateway is checking your meter and API key with Octopus. Your key stays in this form until verification finishes.</p>
      ) : null}
      {submission.kind === "error" ? (
        <p className="notice notice-error" role="alert">{errorMessage(submission.reason)}</p>
      ) : null}
      {submission.kind === "success" ? (
        <div className={styles.successMessage} role="status">
          <strong>Octopus verified. Your connection is ready for a site.</strong>
          <span>The API key has been cleared from this form. Map this verified connection to a site to finish setup.</span>
          <a href="/?view=connections">Choose a site in Connections</a>
        </div>
      ) : null}
    </form>
  );
}
