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
  const [state, setState] = useState<"idle" | "pending" | "error" | "denied">("idle");

  async function mapConnection() {
    if (!siteId || state === "pending") return;
    setState("pending");
    try {
      const response = await fetch("/api/workspace/connections/action", {
        method: "POST",
        body: new URLSearchParams({ connection_id: connectionId, action: "map", site_id: siteId }),
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      });
      if (response.status === 401) {
        window.location.assign("/");
        return;
      }
      if (response.status === 403) {
        setState("denied");
        return;
      }
      if (!response.ok) throw new Error("Connection could not be mapped.");
      window.location.assign("/?view=connections");
    } catch {
      setState("error");
    }
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
              onChange={(event) => setSiteId(event.currentTarget.value)}
            >
              <option value="">Choose a site</option>
              {availableSites.map((site) => (
                <option key={site.id} value={site.id}>{site.name}</option>
              ))}
            </select>
          </div>
          <button className="button button-primary" type="button" disabled={!siteId || state === "pending"} onClick={() => void mapConnection()}>
            {state === "pending" ? "Mapping…" : "Map connection"}
          </button>
          <button className="text-button" type="button" onClick={() => setCreateSite((visible) => !visible)}>
            {createSite ? "Use an existing site" : "Create a site"}
          </button>
        </div>
      ) : (
        <div className="mapping-first-site">
          <p className="form-support">Create a site now, then choose it to finish mapping this connection.</p>
          {!createSite ? (
            <button className="button button-secondary" type="button" onClick={() => setCreateSite(true)}>
              Create a site
            </button>
          ) : null}
        </div>
      )}

      {createSite ? (
        <WorkspaceSiteForm
          compact
          onCreated={(site) => {
            setAvailableSites((current) => current.some((item) => item.id === site.id) ? current : [...current, site]);
            setSiteId(site.id);
            setCreateSite(false);
          }}
        />
      ) : null}
      {state === "denied" ? <p className="notice notice-error" role="alert">That site is outside this workspace. Choose one of the sites above.</p> : null}
      {state === "error" ? <p className="notice notice-error" role="alert">The connection could not be mapped. Refresh the workspace and try again.</p> : null}
    </div>
  );
}
