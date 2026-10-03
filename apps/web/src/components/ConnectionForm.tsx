"use client";

import { useId, useState, type FormEvent } from "react";
import type { ConnectionSetupsResponse } from "@energy-agent-tools/sdk";

type ConnectionSetup = ConnectionSetupsResponse["setups"][number];
type Submission =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error" }
  | { kind: "success" };

type ConnectionFormProps = {
  setup: ConnectionSetup;
  siteName: string | null;
};

const connectionError =
  "Connection could not be completed. Check the meter details and access key, then try again.";

export function ConnectionForm({ setup, siteName }: ConnectionFormProps) {
  const formId = useId();
  const [submission, setSubmission] = useState<Submission>({ kind: "idle" });
  const unavailable = !setup.enabled || !siteName;
  const disabled = unavailable || submission.kind === "pending" || submission.kind === "success";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled) return;

    const form = event.currentTarget;
    const formData = new FormData(form);
    const body = new URLSearchParams();
    const provider = formData.get("provider");
    let valid = typeof provider === "string" && provider === setup.provider;

    if (valid) body.append("provider", setup.provider);

    for (const field of setup.fields) {
      const value = formData.get(field.name);
      if (typeof value !== "string") {
        valid = false;
        break;
      }
      body.append(field.name, value);
    }

    for (const input of form.querySelectorAll<HTMLInputElement>('input[type="password"]')) {
      input.value = "";
    }

    if (!valid) {
      setSubmission({ kind: "error" });
      return;
    }

    setSubmission({ kind: "pending" });
    try {
      const response = await fetch("/api/connections", {
        method: "POST",
        body,
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });
      if (response.status === 401) { window.location.assign("/"); return; }
      if (response.status !== 201) throw new Error("Connection refused.");

      setSubmission({ kind: "success" });
      window.location.assign("/?view=connections");
    } catch {
      setSubmission({ kind: "error" });
    }
  }

  return (
    <form
      className="signin-form"
      action="/api/connections"
      method="post"
      onSubmit={submit}
      aria-busy={submission.kind === "pending"}
    >
      <input type="hidden" name="provider" value={setup.provider} />
      {siteName ? (
        <p style={{ margin: "0 0 8px", color: "var(--ink-soft)", fontSize: 12 }}>
          Add this connection to <strong>{siteName}</strong>.
        </p>
      ) : null}
      <p style={{ margin: 0, color: "var(--ink-muted)", fontSize: 10 }}>
        The gateway verifies these details and stores accepted credentials encrypted.
      </p>

      {unavailable ? (
        <p className="notice notice-neutral" role="status" style={{ margin: 0 }}>
          {!siteName
            ? "Choose a site in the workbench before adding a connection."
            : setup.unavailable_reason === "management_key_required"
              ? "Use a workspace management key to add connections."
              : "Encrypted connection storage is unavailable. Ask the gateway operator to enable it."}
        </p>
      ) : null}

      <fieldset
        aria-label="Connection details"
        disabled={disabled}
        style={{ border: 0, display: "grid", gap: 8, margin: 0, minWidth: 0, padding: 0 }}
      >
        {setup.fields.map((field) => {
          const id = `${formId}-${field.name}`;
          return (
            <div key={field.name} style={{ display: "grid", gap: 8 }}>
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
              />
            </div>
          );
        })}
        <button className="button button-primary" type="submit" style={{ width: "100%" }}>
          {submission.kind === "pending"
            ? "Verifying connection…"
            : submission.kind === "success"
              ? "Connection saved"
              : "Add connection"}
        </button>
      </fieldset>

      {submission.kind === "error" ? (
        <p className="notice notice-error" role="alert">{connectionError}</p>
      ) : null}
    </form>
  );
}
