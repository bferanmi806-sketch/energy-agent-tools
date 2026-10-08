"use client";

import { useState } from "react";
import type { WorkspaceSitesResponse } from "@energy-agent-tools/sdk";
import { WorkspaceSiteForm } from "./WorkspaceForms";

type Site = WorkspaceSitesResponse["sites"][number];

export function PendingConnection({
  connectionId,
  sites,
}: {
  connectionId: string;
  sites: Site[];
}) {
  const [availableSites, setAvailableSites] = useState(sites);
  const [siteId, setSiteId] = useState("");
  const [createSite, setCreateSite] = useState(false);
  const [createdSiteId, setCreatedSiteId] = useState<string | null>(null);
  const [state, setState] = useState<"idle" | "pending" | "error" | "denied">("idle");

  async function mapConnection(targetSiteId = siteId, newlyCreated = targetSiteId === createdSiteId) {
    if (!targetSiteId || state === "pending") return;
    setState("pending");
    try {
      const response = await fetch("/api/workspace/connections/action", {
        method: "POST",
        body: new URLSearchParams({ connection_id: connectionId, action: "map", site_id: targetSiteId }),
        credentials: "same-origin",
        cache: "no-store",
        signal: AbortSignal.timeout(30_000),
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });
      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (response.status === 403) {
        setState(newlyCreated ? "error" : "denied");
        return;
      }
      if (!response.ok) throw new Error("Connection could not be mapped.");
      window.location.assign("/?view=connections");
    } catch {
      setState("error");
    }
  }

  async function handleCreatedSite(site: Site) {
    setAvailableSites((current) => current.some((item) => item.id === site.id) ? current : [...current, site]);
    setSiteId(site.id);
    setCreatedSiteId(site.id);
    setCreateSite(false);
    await mapConnection(site.id, true);
  }

  return (
    <div className="mapping-panel">
      {availableSites.length > 0 ? (
        <div className="mapping-choice">
          <div className="field-stack">
            <label htmlFor={`site-for-${connectionId}`}>Map to a site</label>
            <select
              id={`site-for-${connectionId}`}
              value={siteId}
              disabled={state === "pending"}
              onChange={(event) => {
                setSiteId(event.currentTarget.value);
                setState("idle");
              }}
            >
              <option value="">Choose a site</option>
              {availableSites.map((site) => (
                <option key={site.id} value={site.id}>{site.name}</option>
              ))}
            </select>
          </div>
          <button className="button button-primary" type="button" disabled={!siteId || state === "pending" || createSite} onClick={() => void mapConnection()}>
            {state === "pending" ? "Mapping…" : "Map connection"}
          </button>
          <button className="text-button" type="button" disabled={state === "pending"} onClick={() => {
            setCreateSite((visible) => !visible);
            setState("idle");
          }}>
            {createSite ? "Use an existing site" : "Create another site"}
          </button>
        </div>
      ) : (
        <div className="mapping-first-site">
          <p className="form-support">Create a site to finish mapping this connection.</p>
        </div>
      )}

      <span className="visually-hidden" role="status" aria-live="polite">
        {state === "pending" ? "Mapping connection to the selected site." : ""}
      </span>

      {availableSites.length === 0 || createSite ? (
        <WorkspaceSiteForm
          compact
          submitLabel="Create site and connect"
          onCreated={handleCreatedSite}
        />
      ) : null}
      {state === "denied" ? <p className="notice notice-error" role="alert">That site is outside this workspace. Choose one of the sites above.</p> : null}
      {state === "error" ? <p className="notice notice-error" role="alert">{createdSiteId === siteId ? "The site was created, but the connection could not be mapped. Select Map connection to retry." : "The connection could not be mapped. Select Map connection to retry."}</p> : null}
    </div>
  );
}
