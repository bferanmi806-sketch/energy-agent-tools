"use client";

import {
  Activity,
  ArrowRight,
  Check,
  ChevronRight,
  CircleHelp,
  Settings,
  Zap,
  Plug,
  LayoutGrid,
  LogOut,
  Search,
  ShieldCheck,
  Sparkles,
  Users,
  Waves,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import type { ManagedDashboardData } from "@/lib/types";
import { GatewayForm } from "./GatewayForm";
import { ManagedConnectionActions } from "./ManagedConnectionActions";
import { ManagedConnectionForm } from "./ManagedConnectionForm";
import { HomeAssistantConnect } from "./HomeAssistantConnect";
import { PendingConnection } from "./PendingConnection";
import { AgentKeyPanel } from "./AgentKeyPanel";
import { WorkspaceAssetForm, WorkspaceSiteForm } from "./WorkspaceForms";
import { WorkspaceSharing } from "./WorkspaceSharing";
import { JobsHistory } from "./JobsHistory";
import { ExecutionActivity } from "./ExecutionActivity";
import { SkillsCatalogue } from "./SkillsCatalogue";
import { AccountSettings } from "./AccountSettings";
import { CustomMCPConnect } from "./CustomMCPConnect";

type ViewId = "apps" | "mcp" | "connections" | "sites" | "sharing" | "agent" | "skills" | "activity" | "jobs" | "settings";

interface NavigationItem {
  id: ViewId;
  label: string;
  icon: LucideIcon;
  group: "Connect" | "Workspace" | "Advanced";
}

const NAVIGATION: NavigationItem[] = [
  { group: "Connect", id: "apps", label: "Connect apps", icon: LayoutGrid },
  { group: "Workspace", id: "settings", label: "Account & settings", icon: Settings },
  { group: "Advanced", id: "mcp", label: "Add MCP server", icon: Wrench },
  { group: "Connect", id: "connections", label: "Connections", icon: Activity },
  { group: "Workspace", id: "sites", label: "Sites & assets", icon: ShieldCheck },
  { group: "Advanced", id: "sharing", label: "Sharing", icon: Users },
  { group: "Connect", id: "agent", label: "Connect my agent", icon: ArrowRight },
  { group: "Advanced", id: "skills", label: "Skills", icon: Sparkles },
  { group: "Advanced", id: "jobs", label: "Jobs", icon: Activity },
  { group: "Advanced", id: "activity", label: "Activity log", icon: Activity },
];

const VIEW_CONTENT: Record<ViewId, { title: string; description: string }> = {
  settings: { title: "Account & settings", description: "Your gateway identity, workspace scope and agent access." },
  apps: {
    title: "Connect a system",
    description: "Connect your own provider account, verify access, then map it to a site.",
  },
  mcp: {
    title: "Add MCP server",
    description: "Inspect a custom MCP endpoint, review the tools it exposes, and save a pending connection.",
  },
  connections: {
    title: "Connections",
    description: "See which systems are waiting for a site, active, or disconnected.",
  },
  sites: {
    title: "Sites & assets",
    description: "Create the sites and assets that give your agent a clear scope.",
  },
  agent: {
    title: "Connect your agent",
    description: "Create an agent key with access only to the sites you select.",
  },
  jobs: { title: "Jobs", description: "Recover numerical simulation jobs and their results across sessions." },
  activity: { title: "Activity log", description: "Review recent tool executions in your current scope." },
  skills: {
    title: "Skills",
    description: "Explore the energy questions your agent can ask and the evidence each workflow needs.",
  },
  sharing: {
    title: "Shared connections",
    description: "Give workspace members access to selected sites and active connections.",
  },
};

const RUNTIME_LABELS: Record<ManagedDashboardData["toolkits"][number]["runtime"], string> = {
  http: "HTTP",
  python: "Python",
  native: "Native",
  executable: "Executable",
  "mcp-local": "Local MCP",
  "mcp-remote": "Remote MCP",
};

function canConnect(toolkit: ManagedDashboardData["toolkits"][number], data: ManagedDashboardData): boolean {
  return data.connectionSetups.some(setup => setup.toolkit_id === toolkit.id)
    || toolkit.id === "home-assistant" && data.authConfigurations.length > 0;
}

function ProviderMark({ toolkit }: { toolkit: ManagedDashboardData["toolkits"][number] }) {
  if (toolkit.id === "home-assistant") return <span className="provider-mark" aria-hidden="true"><img src="/providers/home-assistant.svg" alt="" width={26} height={26} /></span>;
  const Icon = toolkit.id.startsWith("octopus") ? Zap : toolkit.runtime.startsWith("mcp") ? Plug : Wrench;
  return <span className="provider-mark" aria-hidden="true"><Icon size={20} strokeWidth={1.7} /></span>;
}

type AuthorizationResult = "connected" | "cancelled" | "invalid" | "failed";

const AUTHORIZATION_MESSAGES: Record<AuthorizationResult, string> = {
  connected: "Home Assistant verified the selected sensor. Choose a site below to finish setup.",
  cancelled: "Home Assistant authorization was cancelled. You can try again when you are ready.",
  invalid: "This authorization return could not be matched to the current workspace session. Start again from a configured instance.",
  failed: "The gateway could not complete Home Assistant authorization. Check the instance and try again.",
};

function statusClass(status: ManagedDashboardData["toolkits"][number]["status"]): string {
  switch (status) {
    case "stable": return "status-good";
    case "experimental": return "status-caution";
    case "requires credentials": return "status-neutral";
    case "unavailable": return "status-muted";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

function connectionLabel(state: string): string {
  switch (state) {
    case "pending_mapping": return "Needs a site";
    case "active": return "Active";
    case "revoked": return "Disconnected";
    default: return state.replaceAll("_", " ");
  }
}

function connectionDisplayName(connection: ManagedDashboardData["connections"][number]): string {
  const displayName = connection.display_name?.trim();
  if (displayName) return displayName;
  return connection.toolkit.startsWith("custom-mcp-")
    ? "Custom MCP server"
    : connection.toolkit.replaceAll("-", " ");
}

function connectionStatusClass(state: string): string {
  if (state === "active") return "status-good";
  if (state === "pending_mapping") return "status-caution";
  return "status-muted";
}

function runtimeDescription(toolkit: ManagedDashboardData["toolkits"][number]): string {
  const categories = (toolkit.categories ?? []).join(" · ");
  return categories ? `${RUNTIME_LABELS[toolkit.runtime]} · ${categories}` : RUNTIME_LABELS[toolkit.runtime];
}

function workspaceStep(data: ManagedDashboardData): "connect" | "map" | "agent" | "complete" {
  const usableConnections = data.connections.filter((connection) => connection.state !== "revoked");
  if (usableConnections.length === 0) return "connect";
  if (!usableConnections.some((connection) => connection.state === "active" && connection.site_id !== null)) return "map";
  if (!data.keys.some((key) => key.access.kind === "agent" && !key.revoked && (!key.expires_at || Date.parse(key.expires_at) > Date.now()))) return "agent";
  return "complete";
}

export function ManagedConsole({
  data,
  initialView = "apps",
  authorizationResult,
  jobStatus,
}: {
  data: ManagedDashboardData;
  initialView?: ViewId;
  authorizationResult?: AuthorizationResult;
  jobStatus?: string;
}) {
  const canManageCustomMCP = data.identity.can_manage_workspace === true && data.workspace.mode === "managed";
  const [view, setView] = useState<ViewId>(() => initialView === "mcp" && !canManageCustomMCP ? "apps" : initialView);
  const [catalogueMode, setCatalogueMode] = useState<"accounts" | "tools">("accounts");
  const [query, setQuery] = useState("");
  const [selectedToolkitId, setSelectedToolkitId] = useState(
    () => data.toolkits.find((toolkit) => canConnect(toolkit, data))?.id ?? null,
  );
  const currentViewId = view === "mcp" && !canManageCustomMCP ? "apps" : view;
  const currentView = VIEW_CONTENT[currentViewId];
  const visibleNavigation = canManageCustomMCP ? NAVIGATION : NAVIGATION.filter((item) => item.id !== "mcp");
  const filteredToolkits = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return data.toolkits.filter((toolkit) => {
      const searchable = [toolkit.id, toolkit.name, toolkit.description, toolkit.runtime, ...(toolkit.categories ?? [])]
        .join(" ").toLocaleLowerCase();
      return (catalogueMode === "accounts" ? canConnect(toolkit, data) : !canConnect(toolkit, data))
        && (!normalized || searchable.includes(normalized));
    });
  }, [data, query, catalogueMode]);
  const selectedToolkit = filteredToolkits.find((toolkit) => toolkit.id === selectedToolkitId) ?? filteredToolkits[0] ?? null;
  const selectedSetup = data.connectionSetups.find((setup) => setup.toolkit_id === selectedToolkit?.id) ?? null;
  const stage = workspaceStep(data);

  function navigate(next: ViewId) {
    if (next === "mcp" && !canManageCustomMCP) {
      setView("apps");
      window.history.replaceState(null, "", "/?view=apps");
      return;
    }
    setView(next);
    window.history.replaceState(null, "", `/?view=${next}`);
  }

  return (
    <div className="console-shell managed-console-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Energy Agent Tools home">
          <span className="brand-mark" aria-hidden="true"><Waves size={19} strokeWidth={1.8} /></span>
          <span className="brand-name">Energy Agent Tools</span>
        </a>
        <div className="managed-workspace-identity">
          <span className="managed-workspace-label">Workspace</span>
          <strong>{data.workspace.name}</strong>
        </div>
        <nav className="product-navigation managed-navigation" aria-label="Workspace sections">
          {(["Connect", "Workspace", "Advanced"] as const).map(group => (
          <div className="navigation-group" key={group}>
          <p className="nav-group-label">{group}</p>
          <div className="nav-items">
            {visibleNavigation.filter(item => item.group === group).map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.id}
                  className={`nav-item${currentViewId === item.id ? " nav-item-active" : ""}`}
                  type="button"
                  aria-current={currentViewId === item.id ? "page" : undefined}
                  onClick={() => navigate(item.id)}
                >
                  <Icon size={17} strokeWidth={1.8} aria-hidden="true" />
                  <span>{item.label}</span>
                  {item.id === "connections" && data.connections.some((connection) => connection.state === "pending_mapping") ? (
                    <span className="nav-count" aria-label="Connections waiting for a site">
                      {data.connections.filter((connection) => connection.state === "pending_mapping").length}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div></div>
          ))}
          <button className="workspace-help-link" type="button" onClick={() => navigate("apps")}>
            <CircleHelp size={15} aria-hidden="true" /> Browse gateway catalogue
          </button>
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-session">
            <span className="session-indicator" aria-hidden="true" />
            <span>Workspace manager</span>
          </div>
          <GatewayForm action="/api/logout" pendingLabel="Signing out…">
            <button className="signout-button" type="submit"><LogOut size={16} aria-hidden="true" /> Sign out</button>
          </GatewayForm>
        </div>
      </aside>

      <div className="workspace-column">
        <header className="topbar">
          <div className="breadcrumb" aria-label="Breadcrumb">
            <span>{data.workspace.name}</span><ChevronRight size={14} aria-hidden="true" /><span>{currentView.title}</span>
          </div>
          <div className="topbar-status"><ShieldCheck size={15} aria-hidden="true" /><span>Managed workspace</span></div>
        </header>
        <main className="workspace-main managed-main">
          <div className="page-heading managed-page-heading">
            <div>
              <h1>{currentView.title}</h1>
              <p>{currentView.description}</p>
            </div>
            <div className="active-site-summary">
              <span className="active-site-label">Sites</span>
              <span className="active-site-value">{data.sites.length === 0 ? "None created" : `${data.sites.length} available`}</span>
            </div>
          </div>

          {stage !== "complete" && ["apps", "connections", "agent", "sites"].includes(currentViewId) ? <WorkspaceProgress stage={stage} onNavigate={navigate} /> : null}

          {authorizationResult ? (
            <div className={`notice ${authorizationResult === "connected" ? "notice-neutral" : "notice-error"} managed-oauth-notice`} role={authorizationResult === "connected" ? "status" : "alert"}>
              <span>{AUTHORIZATION_MESSAGES[authorizationResult]}</span>
            </div>
          ) : null}

          {currentViewId === "apps" ? (
            <AppsView
              catalogueMode={catalogueMode}
              onCatalogueModeChange={setCatalogueMode}
              data={data}
              filteredToolkits={filteredToolkits}
              query={query}
              selectedToolkit={selectedToolkit}
              selectedToolkitId={selectedToolkit?.id ?? null}
              selectedSetup={selectedSetup}
              onQueryChange={setQuery}
              onSelectToolkit={setSelectedToolkitId}
            />
          ) : null}
          {currentViewId === "mcp" && canManageCustomMCP ? <CustomMCPConnect enabled={canManageCustomMCP} /> : null}
          {currentViewId === "connections" ? <ConnectionsView data={data} onBrowseApps={() => navigate("apps")} /> : null}
          {currentViewId === "sites" ? <SitesView data={data} /> : null}
          {currentViewId === "sharing" ? <WorkspaceSharing members={data.members} sites={data.sites} connections={data.connections} gatewayUrl={data.publicGatewayUrl} /> : null}
          {currentViewId === "jobs" ? <JobsHistory jobs={data.jobs} siteNames={Object.fromEntries(data.identity.sites.map(site => [site.id, site.name]))} {...(jobStatus ? { status: jobStatus } : {})} /> : null}
          {currentViewId === "activity" ? <ExecutionActivity activity={data.activity} /> : null}
          {currentViewId === "skills" ? <SkillsCatalogue skills={data.skills} /> : null}
          {currentViewId === "settings" ? <AccountSettings data={data} onConnectAgent={() => navigate("agent")} /> : null}
          {currentViewId === "agent" ? <AgentKeyPanel keys={data.keys} sites={data.sites} gatewayUrl={data.publicGatewayUrl} /> : null}
        </main>
        <footer className="workspace-footer">
          <span>Energy Agent Tools</span>
          <span>Provider access and workspace scope stay with the gateway.</span>
          <span>Self-hosted by your operator</span>
        </footer>
      </div>
    </div>
  );
}

function WorkspaceProgress({
  stage,
  onNavigate,
}: {
  stage: "connect" | "map" | "agent" | "complete";
  onNavigate: (view: ViewId) => void;
}) {
  const steps: { id: Exclude<typeof stage, "complete">; label: string; view: ViewId }[] = [
    { id: "connect", label: "Connect a system", view: "apps" },
    { id: "map", label: "Map to a site", view: "connections" },
    { id: "agent", label: "Connect an agent", view: "agent" },
  ];
  const activeIndex = stage === "complete" ? steps.length : steps.findIndex((step) => step.id === stage);
  return (
    <nav className="workspace-progress" aria-label="Workspace setup">
      {steps.map((step, index) => {
        const complete = stage === "complete" || index < activeIndex;
        const current = stage !== "complete" && index === activeIndex;
        return (
          <button
            key={step.id}
            className={`progress-step${complete ? " progress-step-complete" : ""}${current ? " progress-step-current" : ""}`}
            type="button"
            aria-current={current ? "step" : undefined}
            onClick={() => onNavigate(step.view)}
          >
            <span className="progress-step-mark" aria-hidden="true">{complete ? <Check size={13} /> : index + 1}</span>
            <span>{step.label}</span>
          </button>
        );
      })}
      {stage === "complete" ? <span className="progress-complete-label"><Check size={13} /> Ready for your agent</span> : null}
    </nav>
  );
}

function AppsView({
  catalogueMode,
  onCatalogueModeChange,
  data,
  filteredToolkits,
  query,
  selectedToolkit,
  selectedToolkitId,
  selectedSetup,
  onQueryChange,
  onSelectToolkit,
}: {
  catalogueMode: "accounts" | "tools";
  onCatalogueModeChange: (value: "accounts" | "tools") => void;
  data: ManagedDashboardData;
  filteredToolkits: ManagedDashboardData["toolkits"];
  query: string;
  selectedToolkit: ManagedDashboardData["toolkits"][number] | null;
  selectedToolkitId: string | null;
  selectedSetup: ManagedDashboardData["connectionSetups"][number] | null;
  onQueryChange: (value: string) => void;
  onSelectToolkit: (value: string) => void;
}) {
  return (
    <section className="apps-workbench managed-apps-workbench" aria-label="Toolkit catalogue">
      <div className="catalogue-column">
        <div className="catalogue-switch" role="group" aria-label="Integration type">
          <button type="button" aria-pressed={catalogueMode === "accounts"} onClick={() => onCatalogueModeChange("accounts")}>Connect accounts</button>
          <button type="button" aria-pressed={catalogueMode === "tools"} onClick={() => onCatalogueModeChange("tools")}>Tool catalogue</button>
        </div>
        <div className="catalogue-tools">
          <label className="search-field">
            <Search size={17} aria-hidden="true" />
            <span className="visually-hidden">Search toolkits</span>
            <input value={query} onChange={(event) => onQueryChange(event.currentTarget.value)} placeholder={catalogueMode === "accounts" ? "Search providers" : "Search data and engineering tools"} />
            {query ? <button type="button" className="clear-search" aria-label="Clear search" onClick={() => onQueryChange("")}><X size={15} /></button> : null}
          </label>
          <div className="filter-caption"><span>{catalogueMode === "accounts" ? "Provider account connections" : "Data & engineering tools"}</span><span>{filteredToolkits.length} {filteredToolkits.length === 1 ? "system" : "systems"}</span></div>
          <p className="catalogue-categories">{catalogueMode === "accounts" ? "Use your own account. Credentials remain encrypted in your gateway." : "These tools have no account connection form here. Availability depends on your operator’s configuration."}</p>
        </div>

        {filteredToolkits.length > 0 ? (
          <div className="toolkit-list" aria-label="Available toolkits">
            <div className="toolkit-list-header managed-toolkit-header" aria-hidden="true"><span>System</span><span>Details</span><span>Access</span><span /></div>
            {filteredToolkits.map((toolkit) => {
              const active = selectedToolkitId === toolkit.id;
              return (
                <Fragment key={toolkit.id}>
                  <button
                    className={`toolkit-row managed-toolkit-row${active ? " toolkit-row-selected" : ""}`}
                    type="button"
                    aria-pressed={active}
                    onClick={() => onSelectToolkit(toolkit.id)}
                  >
                    <span className="provider-name-group"><ProviderMark toolkit={toolkit} /><span className="toolkit-name-cell">
                      <span className="toolkit-name">{toolkit.name}</span>
                      <span className="toolkit-description">{toolkit.description}</span>
                    </span></span>
                    <span className="runtime-cell">{runtimeDescription(toolkit)}</span>
                    <span className="status-badge status-neutral">{canConnect(toolkit, data) ? "Account" : "Tool"}</span>
                    <span className="toolkit-action">{canConnect(toolkit, data) ? "Connect" : "Details"} <ArrowRight size={14} aria-hidden="true" /></span>
                  </button>
                  {active ? <ToolkitSetup toolkit={toolkit} setup={selectedSetup} configurations={data.authConfigurations} mobile /> : null}
                </Fragment>
              );
            })}
          </div>
        ) : (
          <div className="empty-state empty-state-list">
            <div className="empty-icon" aria-hidden="true"><Search size={18} /></div>
            <h3>{data.toolkits.length === 0 ? "No systems are available" : "No matching systems"}</h3>
            <p>{data.toolkits.length === 0 ? "The gateway returned an empty hosted catalogue. Ask the operator to check registry visibility." : "Try another search term."}</p>
            {data.toolkits.length > 0 ? <button className="text-button" type="button" onClick={() => onQueryChange("")}>Clear search</button> : null}
          </div>
        )}
        <p className="catalogue-note"><ShieldCheck size={15} aria-hidden="true" /> Catalogue details come from this gateway. A registry entry does not mean an account is connected.</p>
      </div>
      <ToolkitSetup toolkit={selectedToolkit} setup={selectedSetup} configurations={data.authConfigurations} />
    </section>
  );
}

function ToolkitSetup({
  toolkit,
  setup,
  mobile = false,
  configurations,
}: {
  toolkit: ManagedDashboardData["toolkits"][number] | null;
  setup: ManagedDashboardData["connectionSetups"][number] | null;
  mobile?: boolean;
  configurations: ManagedDashboardData["authConfigurations"];
}) {
  const className = `setup-panel managed-setup-panel${mobile ? " mobile-setup" : " desktop-setup"}`;
  if (!toolkit) {
    return (
      <aside className={className}>
        <div className="setup-empty">
          <div className="empty-icon" aria-hidden="true"><Search size={17} /></div>
          <h2>Choose a system</h2>
          <p>Select a toolkit to see what this gateway supports.</p>
        </div>
      </aside>
    );
  }
  return (
    <aside className={className} aria-label={`${toolkit.name} setup`}>
      <h2>{toolkit.name}</h2>
      <p className="setup-description">{toolkit.description}</p>
      <dl className="metadata-list">
        <div><dt>Registry ID</dt><dd><code>{toolkit.id}</code></dd></div>
        <div><dt>Runtime</dt><dd>{RUNTIME_LABELS[toolkit.runtime]}</dd></div>
        <div><dt>Status</dt><dd><span className={`status-badge ${statusClass(toolkit.status)}`}>{toolkit.status}</span></dd></div>
      </dl>
      {toolkit.id === "home-assistant" ? <HomeAssistantConnect configurations={configurations} /> : setup ? (
        <div className="managed-provider-setup">
          <div className="setup-categories">
            <h3>{setup.provider === "octopus" ? "Octopus account" : setup.provider}</h3>
            <p>{setup.description}</p>
          </div>
          <ManagedConnectionForm setup={setup} />
        </div>
      ) : (
        <div className="setup-limitation">
          <p className="note-title">Catalogue tool</p>
          <p>This tool has no account connection form in this workspace. Your operator configures its runtime and access. Once available, your agent discovers it through the same gateway.</p>
        </div>
      )}
    </aside>
  );
}

function ConnectionsView({ data, onBrowseApps }: { data: ManagedDashboardData; onBrowseApps: () => void }) {
  const pending = data.connections.filter((connection) => connection.state === "pending_mapping");
  const active = data.connections.filter((connection) => connection.state === "active");
  const inactive = data.connections.filter((connection) => !["pending_mapping", "active"].includes(connection.state));
  return (
    <section className="managed-content-section">
      {data.connections.length === 0 ? (
        <div className="empty-state managed-empty-state">
          <div className="empty-icon" aria-hidden="true"><Activity size={18} /></div>
          <h2>No systems connected yet</h2>
          <p>Connect and verify a system first. You can create its site mapping after the gateway checks it.</p>
          <button className="button button-primary" type="button" onClick={onBrowseApps}>Browse systems <ArrowRight size={15} /></button>
        </div>
      ) : (
        <div className="managed-connections-list">
          {pending.length > 0 ? (
            <section className="managed-record-group" aria-labelledby="pending-connections-title">
              <div className="section-subheading"><h2 id="pending-connections-title">Ready to map</h2><p>The gateway verified these systems. Choose or create the site that owns each connection.</p></div>
              {pending.map((connection) => (
                <article className="managed-connection-row" key={connection.id}>
                  <div className="managed-record-main">
                    <span className="record-icon"><Activity size={16} aria-hidden="true" /></span>
                    <div><h3>{connectionDisplayName(connection)}</h3><code>{connection.id}</code></div>
                  </div>
                  <span className={`status-badge ${connectionStatusClass(connection.state)}`}>{connectionLabel(connection.state)}</span>
                  <PendingConnection connectionId={connection.id} sites={data.sites} />
                  <ManagedConnectionActions connectionId={connection.id} enabled={connection.enabled} state={connection.state} />
                </article>
              ))}
            </section>
          ) : null}
          {active.length > 0 ? (
            <section className="managed-record-group" aria-labelledby="active-connections-title">
              <div className="section-subheading"><h2 id="active-connections-title">Active connections</h2><p>Mapped connections are available to site-scoped agent keys.</p></div>
              {active.map((connection) => {
                const site = data.sites.find((candidate) => candidate.id === connection.site_id);
                return (
                  <article className="managed-connection-row managed-active-row" key={connection.id}>
                    <div className="managed-record-main">
                      <span className="record-icon"><Activity size={16} aria-hidden="true" /></span>
                      <div><h3>{connectionDisplayName(connection)}</h3><code>{connection.id}</code></div>
                    </div>
                    <span className="managed-record-site">{site?.name ?? "Site unavailable"}</span>
                    <span className={`status-badge ${connectionStatusClass(connection.state)}`}>{connectionLabel(connection.state)}</span>
                    <ManagedConnectionActions connectionId={connection.id} enabled={connection.enabled} state={connection.state} />
                  </article>
                );
              })}
            </section>
          ) : null}
          {inactive.length > 0 ? (
            <section className="managed-record-group" aria-labelledby="inactive-connections-title">
              <div className="section-subheading"><h2 id="inactive-connections-title">Disconnected</h2><p>These records no longer provide access to provider data.</p></div>
              {inactive.map((connection) => (
                <article className="managed-connection-row managed-inactive-row" key={connection.id}>
                  <div className="managed-record-main">
                    <span className="record-icon"><Activity size={16} aria-hidden="true" /></span>
                    <div><h3>{connectionDisplayName(connection)}</h3><code>{connection.id}</code></div>
                  </div>
                  <span className={`status-badge ${connectionStatusClass(connection.state)}`}>{connectionLabel(connection.state)}</span>
                </article>
              ))}
            </section>
          ) : null}
        </div>
      )}
    </section>
  );
}

function SitesView({ data }: { data: ManagedDashboardData }) {
  return (
    <section className="managed-sites-layout">
      <div className="managed-sites-column">
        <div className="section-subheading"><h2>Your sites</h2><p>Sites belong to this workspace. Connections stay pending until mapped to one of them.</p></div>
        {data.sites.length > 0 ? (
          <div className="managed-site-list">
            {data.sites.map((site) => {
              const siteAssets = data.assets.filter((asset) => asset.site_id === site.id);
              return (
                <article className="managed-site-row" key={site.id}>
                  <div className="managed-site-heading">
                    <div><h3>{site.name}</h3><span>{site.timezone}</span></div>
                    <code>{site.id}</code>
                  </div>
                  {siteAssets.length > 0 ? (
                    <ul className="managed-asset-list">
                      {siteAssets.map((asset) => (
                        <li key={asset.id}><strong>{asset.name}</strong><span>{asset.kind}</span></li>
                      ))}
                    </ul>
                  ) : <p className="field-hint">No assets have been added to this site.</p>}
                </article>
              );
            })}
          </div>
        ) : (
          <div className="empty-state managed-empty-state">
            <div className="empty-icon" aria-hidden="true"><ShieldCheck size={18} /></div>
            <h3>No sites yet</h3>
            <p>Connect a system first. When the gateway verifies it, create a site here and map the connection.</p>
          </div>
        )}
        {data.sites.length > 0 ? (
          <div className="managed-asset-create">
            <div className="section-subheading"><h2>Add an asset</h2><p>Describe equipment at one of your sites and optionally attach an active connection.</p></div>
            <WorkspaceAssetForm sites={data.sites} connections={data.connections} />
          </div>
        ) : null}
      </div>
      <aside className="setup-panel managed-create-site-panel">
        <div className="setup-panel-topline"><span>Create a site</span><ShieldCheck size={14} aria-hidden="true" /></div>
        <h2>Give systems a home</h2>
        <p className="setup-description">Use the name and local time zone that make sense for the place.</p>
        <WorkspaceSiteForm />
      </aside>
    </section>
  );
}
