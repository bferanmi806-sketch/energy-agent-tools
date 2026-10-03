"use client";

import { useId, useState, type FormEvent } from "react";
import type { ConnectionSetupsResponse } from "@energy-agent-tools/sdk";

type ConnectionSetup = ConnectionSetupsResponse["setups"][number];
type Submission = "idle" | "pending" | "error";

export function ManagedConnectionForm({ setup }: { setup: ConnectionSetup }) {
  const formId = useId();
  const [submission, setSubmission] = useState<Submission>("idle");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submission === "pending") return;

    const form = event.currentTarget;
    const values = new FormData(form);
    const body = new URLSearchParams();
    let valid = true;
    const provider = values.get("provider");
    if (provider !== setup.provider) valid = false;
    else body.append("provider", provider);

    for (const field of setup.fields) {
      const value = values.get(field.name);
      if (typeof value !== "string" || value.length < field.min_length || value.length > field.max_length) {
        valid = false;
        continue;
      }
      body.append(field.name, value);
    }

    for (const input of form.querySelectorAll<HTMLInputElement>('input[type="password"]')) {
      input.value = "";
    }

    if (!valid) {
      setSubmission("error");
      return;
    }

    setSubmission("pending");
    try {
      const response = await fetch("/api/workspace/connections", {
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
      if (!response.ok) {
        setSubmission("error");
        return;
      }
      window.location.assign("/?view=connections");
    } catch {
      setSubmission("error");
    }
  }

  return (
    <form
      className="signin-form managed-connect-form"
      onSubmit={submit}
      aria-busy={submission === "pending"}
    >
      <input type="hidden" name="provider" value={setup.provider} />
      <p className="form-support">The gateway checks the meter details and encrypts an accepted provider key.</p>
      <fieldset disabled={!setup.enabled || submission === "pending"}>
        {setup.fields.map((field) => {
          const id = `${formId}-${field.name}`;
          return (
            <div className="field-stack" key={field.name}>
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
        <button className="button button-primary" type="submit">
          {submission === "pending" ? "Checking details…" : "Verify and connect"}
        </button>
      </fieldset>
      {!setup.enabled ? (
        <p className="notice notice-neutral" role="status">
          {setup.unavailable_reason === "storage_unavailable"
            ? "Encrypted connection storage is unavailable. Ask the gateway operator to enable it."
            : "This connection setup is unavailable for the current gateway key."}
        </p>
      ) : null}
      {submission === "error" ? (
        <p className="notice notice-error" role="alert">
          The connection could not be verified. Check the meter details and access key, then try again.
        </p>
      ) : null}
    </form>
  );
}
