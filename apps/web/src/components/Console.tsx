"use client";

import { GatewayForm } from "./GatewayForm";

import {
  Activity,
  ArrowRight,
  Box,
  Building2,
  Check,
  ChevronRight,
  CircleHelp,
  Clock3,
  Copy,
  Database,
  ExternalLink,
  FileText,
  KeyRound,
  LayoutGrid,
  LogOut,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import type { DashboardData } from "@/lib/types";

type ViewId = "apps" | "connections" | "shared" | "sites" | "skills" | "artifacts" | "agents" | "jobs" | "logs" | "mcp" | "settings";

interface NavigationItem {
  id: ViewId;
  label: string;
  icon: LucideIcon;
}

const NAVIGATION: { label: string; items: NavigationItem[] }[] = [
  {
    label: "Connect",
    items: [
      { id: "apps", label: "Connect apps", icon: LayoutGrid },
      { id: "connections", label: "Connections", icon: Activity },
      { id: "shared", label: "Shared connections", icon: ShieldCheck },
      { id: "sites", label: "Sites & assets", icon: Building2 },
    ],
  },
  {
    label: "Build",
    items: [
      { id: "skills", label: "Skills", icon: Sparkles },
      { id: "artifacts", label: "Artifacts", icon: Database },
      { id: "agents", label: "Connect my agent", icon: ArrowRight },
      { id: "mcp", label: "Custom MCP", icon: Wrench },
    ],
  },
  {
    label: "Operate",
    items: [
      { id: "jobs", label: "Jobs", icon: Clock3 },
      { id: "logs", label: "Activity log", icon: FileText },
      { id: "settings", label: "Settings", icon: Settings2 },
    ],
  },
];

const VIEW_CONTENT: Record<ViewId, { title: string; description: string }> = {
  apps: { title: "Connect apps", description: "Browse the gateway registry and inspect the setup requirements for each toolkit." },
  connections: { title: "Connections", description: "Review the connection records returned for this gateway identity." },
  shared: { title: "Shared connections", description: "Connection sharing requires explicit access rules managed by the gateway." },
  sites: { title: "Sites & assets", description: "Choose the site that scopes agent requests and inspect its registered assets." },
  skills: { title: "Skills", description: "See the energy workflows and the evidence checks each one requires." },
  artifacts: { title: "Artifacts", description: "Browse the artifacts currently retained in the gateway session." },
  agents: { title: "Connect my agent", description: "Copy a site-scoped MCP endpoint into an agent that supports Streamable HTTP." },
  mcp: { title: "Custom MCP", description: "Review the current boundary for adding a custom MCP server." },
  jobs: { title: "Jobs", description: "Background job controls are not part of this web view yet." },
  logs: { title: "Activity log", description: "Gateway events are not exposed in this web view yet." },
  settings: { title: "Settings", description: "Inspect the identity and gateway scope used by this workspace." },
};

const RUNTIME_LABELS: Record<DashboardData["toolkits"][number]["runtime"], string> = {
  http: "HTTP",
  python: "Python",
  native: "Native",
  executable: "Executable",
  "mcp-local": "Local MCP",
  "mcp-remote": "Remote MCP",
};

function statusClass(status: DashboardData["toolkits"][number]["status"]): string {
  switch (status) {
    case "stable":
      return "status-good";
    case "experimental":
      return "status-caution";
    case "requires credentials":
      return "status-neutral";
    case "unavailable":
      return "status-muted";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

function readableNumber(value: number): string {
  return new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 }).format(value);
}

function shortReference(value: string): string {
  return value.length > 16 ? `${value.slice(0, 9)}…${value.slice(-5)}` : value;
}

function formattedDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function safeDocumentationUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username !== "" || url.password !== "") return null;
    return url.href;
  } catch {
    return null;
  }
}

function mcpEndpoint(gatewayUrl: string | null, siteId: string | null): string | null {
  if (!gatewayUrl || !siteId) return null;
  try {
    const url = new URL(gatewayUrl);
    if (
      (url.protocol !== "https:" && url.protocol !== "http:") ||
      url.username !== "" ||
      url.password !== "" ||
      url.search !== "" ||
      url.hash !== ""
    ) return null;
    const basePath = url.pathname.replace(/\/$/, "");
    url.pathname = `${basePath}/mcp/${encodeURIComponent(siteId)}`;
    return url.href;
  } catch {
    return null;
  }
}

function siteName(data: DashboardData, siteId: string | null): string {
  if (!siteId) return "No site scope";
  const site = data.identity.sites.find((candidate) => candidate.id === siteId);
  return site?.name ?? `Site ${siteId}`;
}

export function Console({ data }: { data: DashboardData }) {
  const [view, setView] = useState<ViewId>("apps");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string | null>(null);
  const [selectedToolkitId, setSelectedToolkitId] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const currentView = VIEW_CONTENT[view];
  const selectedSite = data.identity.sites.find((site) => site.id === data.siteId) ?? null;

  const categories = useMemo(() => {
    const values = new Set<string>();
    for (const toolkit of data.toolkits) {
      for (const value of toolkit.categories ?? []) values.add(value);
    }
    return [...values].sort((left, right) => left.localeCompare(right));
  }, [data.toolkits]);

  const filteredToolkits = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return data.toolkits.filter((toolkit) => {
      const matchesCategory = category === null || (toolkit.categories ?? []).includes(category);
      const searchable = [toolkit.name, toolkit.id, toolkit.description, toolkit.runtime, ...(toolkit.categories ?? [])]
        .join(" ")
        .toLocaleLowerCase();
      return matchesCategory && (!normalizedQuery || searchable.includes(normalizedQuery));
    });
  }, [category, data.toolkits, query]);

  const selectedToolkit = filteredToolkits.find((toolkit) => toolkit.id === selectedToolkitId) ?? null;
  const endpoint = mcpEndpoint(data.publicGatewayUrl, data.siteId);

  async function copyAgentConfig() {
    if (!endpoint) return;
    const config = {
      mcpServers: {
        "energy-agent-tools": {
          url: endpoint,
          headers: { Authorization: "Bearer PASTE_GATEWAY_ACCESS_KEY" },
        },
      },
    };
    try {
      await navigator.clipboard.writeText(JSON.stringify(config, null, 2));
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  }

  return (
    <div className="console-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Energy Agent Tools home">
          <span className="brand-mark" aria-hidden="true"><span className="brand-mark-line" /></span>
          <span className="brand-name">Energy Agent Tools</span>
        </a>
        <nav className="product-navigation" aria-label="Workspace sections">
          {NAVIGATION.map((group) => (
            <div className="nav-group" key={group.label}>
              <p className="nav-group-label">{group.label}</p>
              <div className="nav-items">
                {group.items.map((item) => {
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.id}
                      className={`nav-item${view === item.id ? " nav-item-active" : ""}`}
                      type="button"
                      aria-current={view === item.id ? "page" : undefined}
                      onClick={() => setView(item.id)}
                    >
                      <Icon size={17} strokeWidth={1.8} aria-hidden="true" />
                      <span>{item.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-session">
            <span className="session-indicator" aria-hidden="true" />
            <span>Gateway session authenticated</span>
          </div>
          <GatewayForm action="/api/logout" pendingLabel="Signing out…">
            <button className="signout-button" type="submit"><LogOut size={16} aria-hidden="true" /> Sign out</button>
          </GatewayForm>
        </div>
      </aside>

      <div className="workspace-column">
        <header className="topbar">
          <div className="breadcrumb" aria-label="Breadcrumb">
            <span>Workspace</span><ChevronRight size={14} aria-hidden="true" /><span>{currentView.title}</span>
          </div>
          <div className="topbar-status"><ShieldCheck size={15} aria-hidden="true" /><span>Authenticated gateway</span></div>
        </header>
        <main className="workspace-main">
          <div className="page-heading">
            <div>
              <h1>{currentView.title}</h1>
              <p>{currentView.description}</p>
            </div>
            <div className="active-site-summary">
              <span className="active-site-label">Current site</span>
              <span className="active-site-value">{selectedSite?.name ?? "No site selected"}</span>
            </div>
          </div>

          {view === "apps" ? (
            <AppsView
              categories={categories}
              category={category}
              data={data}
              filteredToolkits={filteredToolkits}
              query={query}
              selectedToolkit={selectedToolkit}
              selectedToolkitId={selectedToolkit?.id ?? null}
              onCategoryChange={setCategory}
              onQueryChange={setQuery}
              onSelectToolkit={setSelectedToolkitId}
            />
          ) : null}
          {view === "connections" ? <ConnectionsView data={data} onBrowseApps={() => setView("apps")} /> : null}
          {view === "shared" ? <SharedConnectionsView /> : null}
          {view === "sites" ? <SitesView data={data} /> : null}
          {view === "skills" ? <SkillsView data={data} /> : null}
          {view === "artifacts" ? <ArtifactsView data={data} /> : null}
          {view === "agents" ? (
            <AgentsView
              copyState={copyState}
              endpoint={endpoint}
              hasSite={data.siteId !== null}
              onCopy={copyAgentConfig}
              onViewSites={() => setView("sites")}
            />
          ) : null}
          {view === "mcp" ? <UnsupportedView kind="custom-mcp" /> : null}
          {view === "jobs" ? <UnsupportedView kind="jobs" /> : null}
          {view === "logs" ? <UnsupportedView kind="logs" /> : null}
          {view === "settings" ? <SettingsView data={data} selectedSite={selectedSite} /> : null}
        </main>
        <footer className="workspace-footer">
          <span>Energy Agent Tools</span>
          <span>Gateway owns provider connections and scope enforcement.</span>
          <span>Self-hosted by your operator</span>
        </footer>
      </div>
    </div>
  );
}

function AppsView({
  categories,
  category,
  data,
  filteredToolkits,
  query,
  selectedToolkit,
  selectedToolkitId,
  onCategoryChange,
  onQueryChange,
  onSelectToolkit,
}: {
  categories: string[];
  category: string | null;
  data: DashboardData;
  filteredToolkits: DashboardData["toolkits"];
  query: string;
  selectedToolkit: DashboardData["toolkits"][number] | null;
  selectedToolkitId: string | null;
  onCategoryChange: (value: string | null) => void;
  onQueryChange: (value: string) => void;
  onSelectToolkit: (value: string) => void;
}) {
  return (
    <section className="apps-workbench" aria-label="Toolkit catalogue">
      <div className="catalogue-column">
        <div className="catalogue-tools">
          <label className="search-field">
            <Search size={17} aria-hidden="true" />
            <span className="visually-hidden">Search toolkits</span>
            <input value={query} onChange={(event) => onQueryChange(event.currentTarget.value)} placeholder="Search toolkits, categories or runtimes" />
            {query ? <button type="button" className="clear-search" aria-label="Clear search" onClick={() => onQueryChange("")}><X size={15} /></button> : null}
          </label>
          <div className="filter-caption"><span>Category</span><span>{filteredToolkits.length} {filteredToolkits.length === 1 ? "toolkit" : "toolkits"}</span></div>
          <div className="category-filters" aria-label="Filter by category">
            <button className={`filter-chip${category === null ? " filter-chip-selected" : ""}`} type="button" aria-pressed={category === null} onClick={() => onCategoryChange(null)}>All</button>
            {categories.map((value) => (
              <button key={value} className={`filter-chip${category === value ? " filter-chip-selected" : ""}`} type="button" aria-pressed={category === value} onClick={() => onCategoryChange(category === value ? null : value)}>{value}</button>
            ))}
          </div>
        </div>

        {filteredToolkits.length > 0 ? (
        <div className="toolkit-list" aria-label="Available toolkits">
            <div className="toolkit-list-header" aria-hidden="true"><span>Toolkit</span><span>Runtime</span><span>Registry status</span><span /></div>
            {filteredToolkits.map((toolkit) => {
              const active = selectedToolkitId === toolkit.id;
              return (
                <Fragment key={toolkit.id}>
                <button
                  className={`toolkit-row${active ? " toolkit-row-selected" : ""}`}
                  type="button"
                  aria-pressed={active}
                  onClick={() => onSelectToolkit(toolkit.id)}
                >
                  <span className="toolkit-name-cell">
                    <span className="toolkit-name">{toolkit.name}</span>
                    <span className="toolkit-description">{toolkit.description}</span>
                  </span>
                  <span className="runtime-cell">{RUNTIME_LABELS[toolkit.runtime]}</span>
                  <span className={`status-badge ${statusClass(toolkit.status)}`}>{toolkit.status}</span>
                  <span className="toolkit-action">View setup <ArrowRight size={14} aria-hidden="true" /></span>
                </button>
                {active ? <ToolkitSetup selectedToolkit={toolkit} className="mobile-setup" titleId="mobile-setup-title" /> : null}
                </Fragment>
              );
            })}
          </div>
        ) : (
          <div className="empty-state empty-state-list">
            <div className="empty-icon" aria-hidden="true"><Search size={18} /></div>
            <h3>{data.toolkits.length === 0 ? "No toolkits in this registry" : "No matching toolkits"}</h3>
            <p>{data.toolkits.length === 0 ? "The gateway returned an empty catalogue. Check the registry configuration with your operator." : "Try a different search or clear the category filter."}</p>
            {data.toolkits.length > 0 ? <button type="button" className="text-button" onClick={() => { onQueryChange(""); onCategoryChange(null); }}>Clear filters</button> : null}
          </div>
        )}
        <p className="catalogue-note"><ShieldCheck size={15} aria-hidden="true" /> Registry metadata comes from the authenticated gateway. Status reflects its catalogue record.</p>
      </div>

      <ToolkitSetup selectedToolkit={selectedToolkit} className="desktop-setup" titleId="desktop-setup-title" />
    </section>
  );
}

function ToolkitSetup({ selectedToolkit, className, titleId }: {
  selectedToolkit: DashboardData["toolkits"][number] | null;
  className: string;
  titleId: string;
}) {
  const documentationUrl = safeDocumentationUrl(selectedToolkit?.docs_url);
  return (
      <aside className={`setup-panel ${className}`} aria-labelledby={titleId}>
        {selectedToolkit ? (
          <>
            <div className="setup-panel-topline"><span>Setup detail</span><span className={`status-dot ${statusClass(selectedToolkit.status)}`} aria-hidden="true" /></div>
            <h2 id={titleId}>{selectedToolkit.name}</h2>
            <p className="setup-description">{selectedToolkit.description}</p>
            <dl className="metadata-list">
              <div><dt>Toolkit ID</dt><dd><code>{selectedToolkit.id}</code></dd></div>
              <div><dt>Runtime</dt><dd>{RUNTIME_LABELS[selectedToolkit.runtime]}</dd></div>
              <div><dt>Registry status</dt><dd><span className={`status-badge ${statusClass(selectedToolkit.status)}`}>{selectedToolkit.status}</span></dd></div>
              <div><dt>Authentication</dt><dd>{selectedToolkit.auth_required === true ? "Credentials required" : "No credentials declared"}</dd></div>
              <div><dt>Version</dt><dd>{selectedToolkit.version ?? "Not supplied"}</dd></div>
            </dl>
            <div className="setup-categories">
              <h3>Categories</h3>
              {selectedToolkit.categories && selectedToolkit.categories.length > 0 ? (
                <ul className="tag-list">{selectedToolkit.categories.map((value) => <li key={value}>{value}</li>)}</ul>
              ) : <p>No category metadata supplied.</p>}
            </div>
            <div className="setup-limitation" role="note">
              <p className="note-title">Provider setup</p>
              <p>This interface can inspect registry metadata. Provider credentials and account onboarding are not available here yet.</p>
            </div>
            {documentationUrl ? (
              <a className="button button-secondary docs-link" href={documentationUrl} target="_blank" rel="noreferrer">
                Open provider setup guide <ExternalLink size={15} aria-hidden="true" />
              </a>
            ) : (
              <p className="muted-note">The registry does not provide a safe HTTPS setup link for this toolkit.</p>
            )}
          </>
        ) : (
          <div className="setup-empty">
            <div className="empty-icon" aria-hidden="true"><Box size={18} /></div>
            <h2 id={titleId}>Setup detail</h2>
            <p>Select a toolkit to inspect its runtime, registry status and available setup documentation.</p>
          </div>
        )}
      </aside>
  );
}

function ConnectionsView({ data, onBrowseApps }: { data: DashboardData; onBrowseApps: () => void }) {
  return (
    <section className="content-section" aria-labelledby="connections-heading">
      <div className="section-toolbar">
        <div><h2 id="connections-heading">Connected accounts</h2><p>Records returned for this authenticated identity. Status is read from the gateway record; this page does not run a live provider health check.</p></div>
        <span className="count-label">{data.connections.length} {data.connections.length === 1 ? "record" : "records"}</span>
      </div>
      {data.connections.length === 0 ? (
        <div className="empty-state compact-empty"><div className="empty-icon" aria-hidden="true"><Activity size={17} /></div><h3>No connections returned</h3><p>There are no connected account records in this gateway response.</p><p className="muted-note">Provider onboarding is not available in this web view yet.</p><button type="button" className="text-button" onClick={onBrowseApps}>Browse app setup details <ArrowRight size={14} aria-hidden="true" /></button></div>
      ) : (
        <div className="record-list">
          {data.connections.map((connection) => {
            const provider = data.toolkits.find((toolkit) => toolkit.id === connection.toolkit);
            return (
              <article className="record-row" key={connection.id}>
                <div className="record-primary">
                  <div className="record-icon" aria-hidden="true"><Activity size={17} /></div>
                  <div><h3>{provider?.name ?? connection.toolkit}</h3><p className="record-id">{connection.id}</p></div>
                </div>
                <div className="record-field"><span>Site scope</span><strong>{siteName(data, connection.site_id)}</strong></div>
                <div className="record-field"><span>Gateway state</span><strong>{connection.state}</strong></div>
                <div className="record-field"><span>Access</span><strong>{connection.enabled ? "Enabled" : "Disabled"} · {connection.auth_scheme}</strong></div>
                <div className="record-health">
                  <span className={`status-badge ${connection.verified ? "status-good" : "status-muted"}`}>{connection.verified ? "Verified record" : "Not verified"}</span>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function SharedConnectionsView() {
  return (
    <section className="content-section" aria-labelledby="shared-connections-heading">
      <div className="section-toolbar">
        <div><h2 id="shared-connections-heading">Shared connections</h2><p>Sharing must be explicit and scoped by the gateway.</p></div>
      </div>
      <div className="sharing-boundary">
        <div className="unsupported-icon" aria-hidden="true"><ShieldCheck size={20} /></div>
        <div>
          <h3>Connection sharing is not enabled in this interface</h3>
          <p>This web application cannot create or edit connection access rules yet. It only displays records returned for the authenticated identity; access to another identity’s connection is never assumed.</p>
          <span className="sharing-status"><span className="sharing-status-dot" aria-hidden="true" /> Explicit ACL controls unavailable</span>
        </div>
      </div>
    </section>
  );
}

function SitesView({ data }: { data: DashboardData }) {
  const selectedSite = data.identity.sites.find((site) => site.id === data.siteId) ?? null;
  const scopedAssets = selectedSite ? data.identity.assets.filter((asset) => asset.site_id === selectedSite.id) : [];

  return (
    <section className="content-section" aria-labelledby="sites-heading">
      <div className="section-toolbar">
        <div><h2 id="sites-heading">Site scope</h2><p>Agent sessions use the site selected for this identity. Changing it updates the server-side session scope.</p></div>
      </div>
      {data.identity.sites.length > 0 ? (
        <GatewayForm className="site-picker" action="/api/site" pendingLabel="Updating site…">
          <div className="site-picker-copy"><span className="section-label">Active site</span><strong>{selectedSite?.name ?? "Choose a site"}</strong><span>{selectedSite ? selectedSite.timezone : "No site is selected for this workspace."}</span></div>
          <label className="site-select-label" htmlFor="site-picker-select">Switch site</label>
          <select id="site-picker-select" name="site_id" defaultValue={data.siteId ?? ""}>
            <option value="">No site selected</option>
            {data.identity.sites.map((site) => <option key={site.id} value={site.id}>{site.name}</option>)}
          </select>
          <button className="button button-primary" type="submit">Use this site <ArrowRight size={15} aria-hidden="true" /></button>
        </GatewayForm>
      ) : (
        <div className="notice notice-neutral"><Building2 size={18} aria-hidden="true" /><p>This identity has no sites in the gateway response. Ask the operator to provision a site before configuring agent scope.</p></div>
      )}

      <div className="section-subheading"><div><h2>Assets at this site</h2><p>Asset records associated with the selected site.</p></div><span className="count-label">{scopedAssets.length} {scopedAssets.length === 1 ? "asset" : "assets"}</span></div>
      {scopedAssets.length > 0 ? (
        <div className="record-list asset-list">
          {scopedAssets.map((asset) => <AssetRow key={asset.id} asset={asset} />)}
        </div>
      ) : (
        <div className="empty-state compact-empty"><div className="empty-icon" aria-hidden="true"><Box size={17} /></div><h3>{selectedSite ? "No assets returned for this site" : "Choose a site to inspect assets"}</h3><p>{selectedSite ? "This identity has no asset records associated with the selected site." : "Select an available site above to view its registered assets."}</p></div>
      )}
    </section>
  );
}

function AssetRow({ asset }: { asset: DashboardData["identity"]["assets"][number] }) {
  return (
    <article className="record-row asset-row">
      <div className="record-primary"><div className="record-icon" aria-hidden="true"><Box size={17} /></div><div><h3>{asset.name}</h3><p className="record-id">{asset.id}</p></div></div>
      <div className="record-field"><span>Kind</span><strong>{asset.kind}</strong></div>
      <div className="record-field"><span>Parent</span><strong>{asset.parent_id ?? "No visible parent"}</strong></div>
      <div className="record-field"><span>Visible linked accounts</span><strong>{asset.account_ids?.length ?? 0}</strong></div>
    </article>
  );
}

function SkillsView({ data }: { data: DashboardData }) {
  return (
    <section className="content-section" aria-labelledby="skills-heading">
      <div className="section-toolbar"><div><h2 id="skills-heading">Gateway skills</h2><p>Intent, execution sequence and known pitfalls are supplied by the gateway.</p></div><span className="count-label">{data.skills.length} {data.skills.length === 1 ? "skill" : "skills"}</span></div>
      {data.skills.length === 0 ? (
        <div className="empty-state compact-empty"><div className="empty-icon" aria-hidden="true"><Sparkles size={17} /></div><h3>No skills returned</h3><p>The gateway did not return any workflow descriptions for this identity.</p></div>
      ) : (
        <div className="skill-list">
          {data.skills.map((skill) => (
            <article className="skill-row" key={skill.id}>
              <div className="skill-heading"><div><span className="record-id">{skill.id}</span><h3>{skill.intent}</h3></div></div>
              <div className="skill-details">
                <div><h4>Capabilities</h4>{skill.capabilities.length > 0 ? <ul className="tag-list">{skill.capabilities.map((item) => <li key={item}>{item}</li>)}</ul> : <p>None listed.</p>}</div>
                <div><h4>Sequence</h4>{skill.sequence.length > 0 ? <ol className="sequence-list">{skill.sequence.map((item, index) => <li key={`${item}-${index}`}><span>{item}</span></li>)}</ol> : <p>No sequence supplied.</p>}</div>
                {skill.supporting_tools && skill.supporting_tools.length > 0 ? <div><h4>Supporting tools</h4><ul className="tag-list">{skill.supporting_tools.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
                <div className="skill-pitfalls"><h4>Known pitfalls</h4>{skill.pitfalls.length > 0 ? <ul className="pitfall-list">{skill.pitfalls.map((item) => <li key={item}>{item}</li>)}</ul> : <p>None listed by the gateway.</p>}</div>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function ArtifactsView({ data }: { data: DashboardData }) {
  return (
    <section className="content-section" aria-labelledby="artifacts-heading">
      <div className="section-toolbar"><div><h2 id="artifacts-heading">Session artifacts</h2><p>Stored outputs currently visible in this gateway session. Values are metadata only; artifacts are not opened here.</p></div><span className="count-label">{data.artifacts.length} {data.artifacts.length === 1 ? "artifact" : "artifacts"}</span></div>
      {data.artifacts.length === 0 ? (
        <div className="empty-state compact-empty"><div className="empty-icon" aria-hidden="true"><Database size={17} /></div><h3>No artifacts in this session</h3><p>Gateway outputs will appear here after an operation persists an artifact.</p></div>
      ) : (
        <div className="record-list">
          {data.artifacts.map((artifact, index) => {
            const artifactId = "artifact_id" in artifact && typeof artifact.artifact_id === "string" ? artifact.artifact_id : null;
            const byteLength = "bytes" in artifact && typeof artifact.bytes === "number" && Number.isFinite(artifact.bytes) ? artifact.bytes : null;
            const expiry = "expires_at" in artifact && typeof artifact.expires_at === "string" ? artifact.expires_at : null;
            return (
              <article className="record-row artifact-row" key={artifactId ?? `artifact-${index}`}>
                <div className="record-primary"><div className="record-icon" aria-hidden="true"><Database size={17} /></div><div><h3>{artifactId ? `Artifact ${shortReference(artifactId)}` : "Stored artifact"}</h3>{artifactId ? <details className="reference-details"><summary>Show artifact reference</summary><code>{artifactId}</code></details> : <p className="record-id">Reference not supplied</p>}</div></div>
                <div className="record-field"><span>Size</span><strong>{byteLength === null ? "Not supplied" : `${readableNumber(byteLength)} bytes`}</strong></div>
                <div className="record-field"><span>Retained until</span><strong>{expiry ? formattedDate(expiry) : "Not supplied"}</strong></div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function agentConfiguration(endpoint: string): string {
  return JSON.stringify({
    mcpServers: {
      "energy-agent-tools": {
        url: endpoint,
        headers: { Authorization: "Bearer PASTE_GATEWAY_ACCESS_KEY" },
      },
    },
  }, null, 2);
}

function AgentsView({
  copyState,
  endpoint,
  hasSite,
  onCopy,
  onViewSites,
}: {
  copyState: "idle" | "copied" | "error";
  endpoint: string | null;
  hasSite: boolean;
  onCopy: () => void;
  onViewSites: () => void;
}) {
  const config = endpoint ? agentConfiguration(endpoint) : null;
  return (
    <section className="content-section agent-config-section" aria-labelledby="agent-config-heading">
      <div className="section-toolbar"><div><h2 id="agent-config-heading">Connect through MCP</h2><p>Use the selected site endpoint so agent calls arrive with the intended gateway scope.</p></div></div>
      {!hasSite ? (
        <div className="notice notice-neutral"><Building2 size={18} aria-hidden="true" /><div><strong>Select a site first</strong><p>A site-scoped MCP endpoint is only available after a site has been selected.</p><button type="button" className="text-button" onClick={onViewSites}>Choose a site <ArrowRight size={14} aria-hidden="true" /></button></div></div>
      ) : !endpoint ? (
        <div className="notice notice-neutral"><CircleHelp size={18} aria-hidden="true" /><div><strong>Public gateway URL is not configured</strong><p>Ask the operator to provide a safe public gateway URL before copying a remote MCP endpoint.</p></div></div>
      ) : (
        <>
          <div className="config-toolbar"><div><span className="section-label">Site-scoped endpoint</span><strong>{endpoint}</strong></div><span className="scope-tag">Current site</span></div>
          <pre className="config-block"><code>{config}</code></pre>
          <div className="config-actions">
            <button className="button button-primary" type="button" onClick={onCopy}>
              {copyState === "copied" ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
              {copyState === "copied" ? "Copied" : "Copy agent config"}
            </button>
            <span aria-live="polite" className={`copy-feedback${copyState === "error" ? " copy-feedback-error" : ""}`}>
              {copyState === "copied" ? "Configuration copied. Add your gateway access key in the agent’s credential settings." : copyState === "error" ? "Clipboard access failed. Select and copy the configuration above." : "The copied config contains a placeholder, never your access key."}
            </span>
          </div>
        </>
      )}
      <div className="agent-instructions">
        <h3>Before your agent connects</h3>
        <ul>
          <li>Replace <code>PASTE_GATEWAY_ACCESS_KEY</code> in the agent’s secret or credential settings.</li>
          <li>Keep the key private. Do not include it in a prompt, repository or shared document.</li>
          <li>Gateway calls use the selected site and the permissions attached to this identity.</li>
        </ul>
      </div>
    </section>
  );
}

type UnsupportedKind = "custom-mcp" | "jobs" | "logs";

const UNSUPPORTED_CONTENT: Record<UnsupportedKind, { icon: LucideIcon; title: string; description: string; available: string }> = {
  "custom-mcp": {
    icon: Wrench,
    title: "Custom MCP setup is not available here yet",
    description: "This web view does not create or review imported MCP servers. Review, namespace and schema-drift checks remain part of the gateway’s operator and SDK workflow.",
    available: "No server is created or changed from this page.",
  },
  jobs: {
    icon: Clock3,
    title: "Job controls are not available here yet",
    description: "The web dashboard does not load job history or expose pause, resume or cancellation controls.",
    available: "No background job is started or changed from this page.",
  },
  logs: {
    icon: FileText,
    title: "Gateway activity is not available here yet",
    description: "This web view does not expose an event or audit log endpoint, so there is no activity history to display.",
    available: "No activity records were requested by this page.",
  },
};

function UnsupportedView({ kind }: { kind: UnsupportedKind }) {
  const content = UNSUPPORTED_CONTENT[kind];
  const Icon = content.icon;
  return (
    <section className="unsupported-panel" aria-labelledby="unsupported-title">
      <div className="unsupported-icon" aria-hidden="true"><Icon size={20} /></div>
      <div><h2 id="unsupported-title">{content.title}</h2><p>{content.description}</p><p className="muted-note">{content.available}</p></div>
    </section>
  );
}

function SettingsView({ data, selectedSite }: { data: DashboardData; selectedSite: DashboardData["identity"]["sites"][number] | null }) {
  return (
    <section className="content-section" aria-labelledby="settings-heading">
      <div className="section-toolbar"><div><h2 id="settings-heading">Workspace identity</h2><p>Values are read from the authenticated gateway identity and current session scope.</p></div></div>
      <dl className="settings-list">
        <div><dt>Authentication</dt><dd><span className="setting-state"><ShieldCheck size={15} aria-hidden="true" /> Operator-provisioned gateway key</span></dd></div>
        <div><dt>User ID</dt><dd><code>{data.identity.user_id}</code></dd></div>
        <div><dt>Selected site</dt><dd>{selectedSite?.name ?? "No site selected"}</dd></div>
        <div><dt>Site ID</dt><dd>{data.siteId ? <code>{data.siteId}</code> : "Not set"}</dd></div>
        <div><dt>Available sites</dt><dd>{data.identity.sites.length}</dd></div>
        <div><dt>Visible assets</dt><dd>{data.identity.assets.length}</dd></div>
        <div><dt>Gateway endpoint for agents</dt><dd>{data.publicGatewayUrl ? "Configured for the agent view" : "Not configured"}</dd></div>
      </dl>
      <div className="settings-note"><KeyRound size={17} aria-hidden="true" /><p>The gateway operator manages identities, access keys, provider secrets and connection permissions. This interface does not offer user or shared-connection administration.</p></div>
    </section>
  );
}
