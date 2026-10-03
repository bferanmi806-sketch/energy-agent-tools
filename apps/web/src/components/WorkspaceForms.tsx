"use client";

import { useId, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import type { ConnectionsResponse, WorkspaceSitesResponse } from "@energy-agent-tools/sdk";

type Site = WorkspaceSitesResponse["sites"][number];
type Connection = ConnectionsResponse["connections"][number];
type FormState = "idle" | "pending" | "error";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseSite(value: unknown): Site | null {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.name !== "string" || typeof value.timezone !== "string") {
    return null;
  }
  return {
    id: value.id,
    user_id: typeof value.user_id === "string" ? value.user_id : "",
    name: value.name,
    timezone: value.timezone,
    latitude: typeof value.latitude === "number" || value.latitude === null ? value.latitude : null,
    longitude: typeof value.longitude === "number" || value.longitude === null ? value.longitude : null,
  };
}

export function WorkspaceSiteForm({
  onCreated,
  compact = false,
}: {
  onCreated?: (site: Site) => void;
  compact?: boolean;
}) {
  const formId = useId();
  const router = useRouter();
  const [state, setState] = useState<FormState>("idle");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (state === "pending") return;
    const values = new FormData(event.currentTarget);
    const name = values.get("name");
    const timezone = values.get("timezone");
    if (typeof name !== "string" || typeof timezone !== "string" || !name.trim() || !timezone.trim()) {
      setState("error");
      return;
    }

    setState("pending");
    try {
      const response = await fetch("/api/workspace/sites", {
        method: "POST",
        body: new URLSearchParams({ name: name.trim(), timezone: timezone.trim() }),
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });
      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (!response.ok) throw new Error("Site could not be created.");
      const payload: unknown = await response.json();
      const site = isRecord(payload) ? parseSite(payload.site) : null;
      if (!site) throw new Error("Site could not be created.");
      if (onCreated) {
        onCreated(site);
        router.refresh();
      }
      else window.location.assign("/?view=sites");
    } catch {
      setState("error");
    }
  }

  return (
    <form className={`workspace-form${compact ? " workspace-form-compact" : ""}`} onSubmit={submit} aria-busy={state === "pending"}>
      {!compact ? <p className="form-support">A site gives agent keys a clear place to read from.</p> : null}
      <div className="field-stack">
        <label htmlFor={`${formId}-name`}>Site name</label>
        <input id={`${formId}-name`} name="name" maxLength={256} required placeholder="e.g. Home" />
      </div>
      <div className="field-stack">
        <label htmlFor={`${formId}-timezone`}>Time zone</label>
        <input id={`${formId}-timezone`} name="timezone" maxLength={80} required placeholder="e.g. Europe/London" />
        <span className="field-hint">Used to align site data with local time.</span>
      </div>
      <button className="button button-primary" type="submit" disabled={state === "pending"}>
        {state === "pending" ? "Creating site…" : compact ? "Create site" : "Add site"}
      </button>
      {state === "error" ? <p className="notice notice-error" role="alert">The site could not be saved. Check the details and try again.</p> : null}
    </form>
  );
}

export function WorkspaceAssetForm({ sites, connections }: { sites: Site[]; connections: Connection[] }) {
  const formId = useId();
  const [state, setState] = useState<FormState>("idle");
  const activeConnections = connections.filter((connection) => connection.enabled && connection.state === "active");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (state === "pending") return;
    const values = new FormData(event.currentTarget);
    const siteId = values.get("site_id");
    const name = values.get("name");
    const kind = values.get("kind");
    if (
      typeof siteId !== "string" || !sites.some((site) => site.id === siteId) ||
      typeof name !== "string" || !name.trim() || typeof kind !== "string" || !kind.trim()
    ) {
      setState("error");
      return;
    }
    const body = new URLSearchParams({ site_id: siteId, name: name.trim(), kind: kind.trim() });
    for (const value of values.getAll("account_id")) {
      if (typeof value === "string") body.append("account_id", value);
    }

    setState("pending");
    try {
      const response = await fetch("/api/workspace/assets", {
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
      if (!response.ok) throw new Error("Asset could not be created.");
      window.location.assign("/?view=sites");
    } catch {
      setState("error");
    }
  }

  return (
    <form className="workspace-form" onSubmit={submit} aria-busy={state === "pending"}>
      <div className="field-stack">
        <label htmlFor={`${formId}-site`}>Site</label>
        <select id={`${formId}-site`} name="site_id" required defaultValue={sites[0]?.id ?? ""}>
          {sites.map((site) => <option key={site.id} value={site.id}>{site.name}</option>)}
        </select>
      </div>
      <div className="field-stack">
        <label htmlFor={`${formId}-name`}>Asset name</label>
        <input id={`${formId}-name`} name="name" maxLength={256} required placeholder="e.g. Main electricity meter" />
      </div>
      <div className="field-stack">
        <label htmlFor={`${formId}-kind`}>Asset type</label>
        <input id={`${formId}-kind`} name="kind" maxLength={80} required placeholder="meter, battery, heat pump…" />
      </div>
      {activeConnections.length > 0 ? (
        <fieldset className="connection-associations">
          <legend>Connections for this asset</legend>
          {activeConnections.map((connection) => (
            <label className="check-row" key={connection.id}>
              <input type="checkbox" name="account_id" value={connection.id} />
              <span>{connection.toolkit.replaceAll("-", " ")}</span>
            </label>
          ))}
        </fieldset>
      ) : null}
      <button className="button button-primary" type="submit" disabled={sites.length === 0 || state === "pending"}>
        {state === "pending" ? "Saving asset…" : "Add asset"}
      </button>
      {state === "error" ? <p className="notice notice-error" role="alert">The asset could not be saved. Check its site and details.</p> : null}
    </form>
  );
}
