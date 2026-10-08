"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import type { WorkspaceKeysResponse, WorkspaceSitesResponse } from "@energy-agent-tools/sdk";
import { AgentSetup } from "./AgentSetup";

type KeyRecord = WorkspaceKeysResponse["keys"][number];
type Site = WorkspaceSitesResponse["sites"][number];
type OneTimeKey = {
  id: string;
  name: string;
  token: string;
  tokenPrefix: string;
  siteIds: string[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseIssuedKey(value: unknown): OneTimeKey | null {
  if (!isRecord(value) || typeof value.token !== "string" || value.token.length !== 47 || !isRecord(value.key)) {
    return null;
  }
  const key = value.key;
  const access = key.access;
  if (
    typeof key.id !== "string" || typeof key.name !== "string" || typeof key.token_prefix !== "string" ||
    !isRecord(access) || access.kind !== "agent" || !Array.isArray(access.site_ids) ||
    !access.site_ids.every((siteId) => typeof siteId === "string")
  ) {
    return null;
  }
  return {
    id: key.id,
    name: key.name,
    token: value.token,
    tokenPrefix: key.token_prefix,
    siteIds: access.site_ids,
  };
}

export function AgentKeyPanel({
  keys,
  sites,
  gatewayUrl,
}: {
  keys: KeyRecord[];
  sites: Site[];
  gatewayUrl: string | null;
}) {
  const router = useRouter();
  const [submission, setSubmission] = useState<"idle" | "pending" | "error">("idle");
  const [oneTimeKey, setOneTimeKey] = useState<OneTimeKey | null>(null);
  const [copied, setCopied] = useState<"idle" | "copied" | "error">("idle");
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState(false);
  const agentKeys = keys.filter((key) => key.access.kind === "agent");

  async function issueKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submission === "pending") return;
    const values = new FormData(event.currentTarget);
    const name = values.get("name");
    const siteIds = values.getAll("site_id").filter((value): value is string => typeof value === "string");
    if (
      typeof name !== "string" || !name.trim() || siteIds.length === 0 ||
      siteIds.some((siteId) => !sites.some((site) => site.id === siteId))
    ) {
      setSubmission("error");
      return;
    }
    const body = new URLSearchParams({ name: name.trim() });
    for (const siteId of siteIds) body.append("site_id", siteId);

    setSubmission("pending");
    try {
      const response = await fetch("/api/workspace/keys", {
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
      if (!response.ok) throw new Error("Agent key could not be issued.");
      const payload: unknown = await response.json();
      const parsed = parseIssuedKey(payload);
      if (!parsed) throw new Error("Agent key could not be issued.");
      setOneTimeKey(parsed);
      setCopied("idle");
      setSubmission("idle");
      router.refresh();
    } catch {
      setSubmission("error");
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied("copied");
    } catch {
      setCopied("error");
    }
  }

  async function revokeKey(keyId: string) {
    setRevokingId(keyId);
    setRevokeError(false);
    try {
      const response = await fetch("/api/workspace/keys/action", {
        method: "POST",
        body: new URLSearchParams({ key_id: keyId }),
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });
      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (!response.ok) throw new Error("Key could not be revoked.");
      window.location.assign("/?view=agent");
    } catch {
      setRevokingId(null);
      setRevokeError(true);
    }
  }

  return (
    <div className="agent-keys-layout">
      <section className="agent-key-issue" aria-labelledby="issue-agent-key">
        <div className="section-subheading">
          <h2 id="issue-agent-key">Create a scoped agent key</h2>
          <p>Choose the sites this agent can access. The key cannot change workspace connections or sites.</p>
        </div>
        {sites.length > 0 ? (
          <form className="workspace-form" onSubmit={issueKey} aria-busy={submission === "pending"}>
            <div className="field-stack">
              <label htmlFor="agent-key-name">Key name</label>
              <input id="agent-key-name" name="name" maxLength={256} required placeholder="e.g. Home energy assistant" />
            </div>
            <fieldset className="site-grants">
              <legend>Allow access to</legend>
              {sites.map((site) => (
                <label className="check-row" key={site.id}>
                  <input type="checkbox" name="site_id" value={site.id} />
                  <span>{site.name}</span>
                  <small>{site.timezone}</small>
                </label>
              ))}
            </fieldset>
            <button className="button button-primary" type="submit" disabled={submission === "pending"}>
              {submission === "pending" ? "Creating key…" : "Create agent key"}
            </button>
            {submission === "error" ? <p className="notice notice-error" role="alert">Choose at least one site and try again.</p> : null}
          </form>
        ) : (
          <p className="notice notice-neutral" role="status">Create a site before issuing a scoped agent key.</p>
        )}

        {oneTimeKey ? (
          <section className="one-time-key" aria-labelledby="one-time-key-title">
            <div className="one-time-key-heading">
              <h3 id="one-time-key-title">{oneTimeKey.name} is ready</h3>
              <button className="text-button" type="button" onClick={() => setOneTimeKey(null)}>Hide key</button>
            </div>
            <p>Copy this key now. The gateway will not show it again.</p>
            <div className="secret-value-row">
              <code>{oneTimeKey.token}</code>
              <button className="button button-secondary" type="button" onClick={() => void copy(oneTimeKey.token)}>Copy key</button>
            </div>
            {copied === "copied" ? <p className="mutation-feedback" role="status">Copied to clipboard.</p> : null}
            {copied === "error" ? <p className="notice notice-error" role="alert">Clipboard access was unavailable. Select the text and copy it.</p> : null}
            <AgentSetup gatewayUrl={gatewayUrl} siteIds={oneTimeKey.siteIds} token={oneTimeKey.token} />
            <button className="text-button" type="button" onClick={() => setOneTimeKey(null)}>Done</button>
          </section>
        ) : null}
      </section>

      <section className="agent-key-list" aria-labelledby="agent-key-list-title">
        <div className="section-subheading">
          <h2 id="agent-key-list-title">Agent keys</h2>
          <p>Only key names, prefixes and site grants are retained here.</p>
        </div>
        {agentKeys.length === 0 ? (
          <div className="empty-state empty-state-list">
            <h3>No agent keys yet</h3>
            <p>Create a key with access to one or more of your sites.</p>
          </div>
        ) : (
          <ul className="managed-key-list">
            {agentKeys.map((key) => {
              const grant = "site_ids" in key.access ? key.access.site_ids : null;
              if (!grant) return null;
              const isConfirming = confirmingId === key.id;
              return (
                <li className="managed-key-row" key={key.id}>
                  <div className="managed-key-main">
                    <strong>{key.name}</strong>
                    <code>{key.token_prefix}…</code>
                    <span>{grant.map((id) => sites.find((site) => site.id === id)?.name ?? "Unknown site").join(", ")}</span>
                  </div>
                  <div className="managed-key-actions">
                    {key.revoked ? <span className="status-badge status-muted">Revoked</span> : isConfirming ? (
                      <>
                        <button className="button button-secondary" type="button" onClick={() => setConfirmingId(null)}>Cancel</button>
                        <button className="button button-danger" type="button" disabled={revokingId === key.id} onClick={() => void revokeKey(key.id)}>
                          {revokingId === key.id ? "Revoking…" : "Confirm revoke"}
                        </button>
                      </>
                    ) : (
                      <button className="button button-secondary" type="button" onClick={() => setConfirmingId(key.id)}>Revoke</button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        {revokeError ? <p className="notice notice-error" role="alert">The key could not be revoked. Try again.</p> : null}
      </section>
    </div>
  );
}
