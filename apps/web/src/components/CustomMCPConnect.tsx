"use client";

import { useEffect, useId, useRef, useState } from "react";
import type {
  MCPConnectionInspectionRequest,
  MCPConnectionInspectionResponse,
  MCPConnectionStageRequest,
  MCPConnectionStageResponse,
} from "@energy-agent-tools/sdk";
import styles from "./CustomMCPConnect.module.css";

type AuthScheme = NonNullable<MCPConnectionInspectionRequest["auth_scheme"]>;
type InspectedTool = MCPConnectionInspectionResponse["inspection"]["tools"][number];
type Inspection = MCPConnectionInspectionResponse["inspection"];
type Review = MCPConnectionStageRequest["reviews"][number];
type Action = Review["actions"][number];
type ResultKind = Review["kind"];

type AuthInput =
  | { scheme: "none" }
  | { scheme: "bearer"; token: string }
  | { scheme: "basic"; username: string; password: string }
  | { scheme: "api-key"; header: string; value: string };

type ToolDraft = {
  name: string;
  selected: boolean;
  reviewed: boolean;
  actions: Action[];
  kind: ResultKind | "";
  unit: string;
};

type InspectionState = { kind: "idle" } | { kind: "ready"; inspection: Inspection; revision: number };
type Operation = { kind: "idle" } | { kind: "inspecting" } | { kind: "staging" };
type Feedback = { kind: "none" } | { kind: "notice" | "error"; message: string };

type PreparedConnection = {
  url: string;
  auth_scheme: AuthScheme;
  auth_header: string;
  credential: string | null;
};

const REQUEST_TIMEOUT_MS = 45_000;
const MAX_SELECTED_TOOLS = 100;

const actionOptions = [
  { value: "read-only", label: "Read only" },
  { value: "calculation", label: "Calculation" },
  { value: "simulation", label: "Simulation" },
  { value: "external-data", label: "External data" },
  { value: "configuration-write", label: "Configuration write" },
  { value: "physical-control", label: "Physical control" },
  { value: "safety-critical", label: "Safety critical" },
] satisfies readonly { value: Action; label: string }[];

const kindOptions = [
  { value: "metered", label: "Metered" },
  { value: "calculated", label: "Calculated" },
  { value: "estimated", label: "Estimated" },
  { value: "simulated", label: "Simulated" },
  { value: "forecast", label: "Forecast" },
] satisfies readonly { value: ResultKind; label: string }[];

const apiHeaderPattern = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$/;
const controlCharacterPattern = /[\u0000-\u001f\u007f]/;
const schemaHashPattern = /^[0-9a-f]{64}$/;

export function CustomMCPConnect({ enabled }: { enabled: boolean }) {
  const id = useId();
  const [displayName, setDisplayName] = useState("");
  const [url, setUrl] = useState("");
  const [auth, setAuth] = useState<AuthInput>({ scheme: "none" });
  const [inspectionState, setInspectionState] = useState<InspectionState>({ kind: "idle" });
  const [drafts, setDrafts] = useState<ToolDraft[]>([]);
  const [operation, setOperation] = useState<Operation>({ kind: "idle" });
  const [feedback, setFeedback] = useState<Feedback>({ kind: "none" });
  const [toolFilter, setToolFilter] = useState("");
  const formRevision = useRef(0);
  const requestNumber = useRef(0);
  const activeRequest = useRef<{ controller: AbortController; number: number } | null>(null);

  const busy = operation.kind !== "idle";
  const inspection = inspectionState.kind === "ready" ? inspectionState.inspection : null;
  const selectedCount = drafts.reduce((count, draft) => count + Number(draft.selected), 0);
  const currentInspection = inspectionState.kind === "ready" && inspectionState.revision === formRevision.current;
  const selectedDrafts = drafts.filter((draft) => draft.selected);
  const allSelectedReviewsComplete = selectedDrafts.every(isCompleteReview);
  const validDisplayName = isBoundedText(displayName.trim(), 256);
  const canStage = enabled && validDisplayName && currentInspection && selectedCount > 0 && selectedCount <= MAX_SELECTED_TOOLS && allSelectedReviewsComplete;

  useEffect(() => {
    if (enabled) return;
    formRevision.current += 1;
    requestNumber.current += 1;
    activeRequest.current?.controller.abort();
    activeRequest.current = null;
    setOperation({ kind: "idle" });
    setInspectionState({ kind: "idle" });
    setDrafts([]);
    setAuth(clearAuthSecrets);
  }, [enabled]);

  useEffect(() => () => {
    requestNumber.current += 1;
    activeRequest.current?.controller.abort();
    activeRequest.current = null;
  }, []);

  function invalidateInspection() {
    formRevision.current += 1;
    setInspectionState({ kind: "idle" });
    setDrafts([]);
    setToolFilter("");
    setFeedback({ kind: "none" });
  }

  function clearSecrets() {
    setAuth(clearAuthSecrets);
  }

  function updateAuth(next: AuthInput) {
    setAuth(next);
    invalidateInspection();
  }

  async function inspectServer() {
    if (!enabled || operation.kind !== "idle") return;
    const connection = prepareConnection(url, auth);
    if (!validDisplayName || !connection) {
      setFeedback({ kind: "error", message: "Enter a connection name and check the endpoint and authentication fields." });
      return;
    }

    const revision = formRevision.current;
    const number = requestNumber.current + 1;
    requestNumber.current = number;
    const controller = new AbortController();
    activeRequest.current = { controller, number };
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    setInspectionState({ kind: "idle" });
    setDrafts([]);
    setFeedback({ kind: "none" });
    setOperation({ kind: "inspecting" });

    const body: MCPConnectionInspectionRequest = {
      url: connection.url,
      auth_scheme: connection.auth_scheme,
      auth_header: connection.auth_header,
      credential: connection.credential,
    };

    try {
      const response = await fetch("/api/workspace/mcp/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      });
      if (!isCurrentRequest(number)) return;
      if (response.status === 401) {
        clearSecrets();
        window.location.assign("/");
        return;
      }
      if (!response.ok) throw new Error("Inspection failed.");

      const payload: unknown = await response.json();
      if (!isInspectionResponse(payload)) throw new Error("Inspection response was invalid.");
      if (formRevision.current !== revision) return;

      setInspectionState({ kind: "ready", inspection: payload.inspection, revision });
      setDrafts(payload.inspection.tools.map((tool) => newToolDraft(tool)));
    } catch {
      if (!isCurrentRequest(number)) return;
      clearSecrets();
      setInspectionState({ kind: "idle" });
      setDrafts([]);
      setFeedback({ kind: "error", message: "The MCP server could not be inspected. Credentials were cleared; check the endpoint and try again." });
    } finally {
      window.clearTimeout(timeout);
      if (isCurrentRequest(number)) {
        activeRequest.current = null;
        setOperation({ kind: "idle" });
      }
    }
  }

  async function stageConnection() {
    if (!canStage || operation.kind !== "idle" || !inspection) return;
    const connection = prepareConnection(url, auth);
    const normalizedName = displayName.trim();
    if (!connection || !isBoundedText(normalizedName, 256)) {
      setFeedback({ kind: "error", message: "Add a valid display name and connection details before saving." });
      return;
    }

    const reviews: Review[] = [];
    for (const draft of selectedDrafts) {
      const review = buildReview(draft);
      if (!review) {
        setFeedback({ kind: "error", message: "Complete the action, result kind, unit, and review confirmation for each selected tool." });
        return;
      }
      reviews.push(review);
    }

    const revision = formRevision.current;
    const number = requestNumber.current + 1;
    requestNumber.current = number;
    const controller = new AbortController();
    activeRequest.current = { controller, number };
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    setFeedback({ kind: "none" });
    setOperation({ kind: "staging" });

    const body: MCPConnectionStageRequest = {
      url: connection.url,
      auth_scheme: connection.auth_scheme,
      auth_header: connection.auth_header,
      credential: connection.credential,
      display_name: normalizedName,
      schema_digest: inspection.schema_digest,
      reviews,
    };

    try {
      const response = await fetch("/api/workspace/mcp/stage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      });
      if (!isCurrentRequest(number)) return;
      if (response.status === 401) {
        clearSecrets();
        window.location.assign("/");
        return;
      }
      if (!response.ok) throw new Error("Save failed.");

      const payload: unknown = await response.json();
      if (!isStageResponse(payload, inspection.schema_digest, reviews.length)) throw new Error("Save response was invalid.");
      if (formRevision.current !== revision) return;

      clearSecrets();
      setInspectionState({ kind: "idle" });
      setDrafts([]);
      window.location.assign("/?view=connections");
    } catch {
      if (!isCurrentRequest(number)) return;
      clearSecrets();
      setInspectionState({ kind: "idle" });
      setDrafts([]);
      setFeedback({ kind: "error", message: "The reviewed connection could not be saved. Credentials were cleared; inspect the server again before retrying." });
    } finally {
      window.clearTimeout(timeout);
      if (isCurrentRequest(number)) {
        activeRequest.current = null;
        clearSecrets();
        setOperation({ kind: "idle" });
      }
    }
  }

  function cancelRequest() {
    const active = activeRequest.current;
    if (!active) return;
    const wasStaging = operation.kind === "staging";
    requestNumber.current += 1;
    activeRequest.current = null;
    active.controller.abort();
    clearSecrets();
    setInspectionState({ kind: "idle" });
    setDrafts([]);
    setOperation({ kind: "idle" });
    setFeedback({
      kind: "notice",
      message: wasStaging
        ? "The request was stopped in this browser. Check Connections before retrying; the server may already have received it. Credentials were cleared."
        : "Inspection was stopped and credentials were cleared.",
    });
  }

  function discardReview() {
    clearSecrets();
    setInspectionState({ kind: "idle" });
    setDrafts([]);
    setToolFilter("");
    setFeedback({ kind: "notice", message: "Review discarded and credentials cleared." });
  }

  function updateDraft(name: string, update: (draft: ToolDraft) => ToolDraft) {
    setDrafts((current) => current.map((draft) => (draft.name === name ? update(draft) : draft)));
  }

  function toggleSelection(name: string, selected: boolean) {
    updateDraft(name, (draft) => ({ ...draft, selected, reviewed: false }));
    setFeedback({ kind: "none" });
  }

  function toggleAction(name: string, action: Action, checked: boolean) {
    updateDraft(name, (draft) => ({
      ...draft,
      actions: checked
        ? [...draft.actions, action]
        : draft.actions.filter((selectedAction) => selectedAction !== action),
      reviewed: false,
    }));
    setFeedback({ kind: "none" });
  }

  function visibleTools(tools: InspectedTool[]): InspectedTool[] {
    const query = toolFilter.trim().toLocaleLowerCase();
    if (query.length === 0) return tools;
    return tools.filter((tool) => tool.name.toLocaleLowerCase().includes(query));
  }

  const filteredTools = inspection ? visibleTools(inspection.tools) : [];
  const selectedNeedsReview = selectedDrafts.some((draft) => !isCompleteReview(draft));

  return (
    <section className={styles.shell} aria-labelledby={`${id}-heading`} aria-busy={busy}>
      <header className={styles.header}>
        <div>
          <h2 id={`${id}-heading`}>Connect a custom MCP server</h2>
          <p className={styles.intro}>
            Inspect the endpoint, then choose which tools to stage. You declare each tool’s actions and result meaning; upstream descriptions and annotations are untrusted.
          </p>
        </div>
        <div className={styles.steps} aria-label="Connection steps">
          <span className={!inspection ? styles.stepActive : undefined}>Endpoint</span>
          <span aria-hidden="true">›</span>
          <span className={inspection ? styles.stepActive : undefined}>Review tools</span>
          <span aria-hidden="true">›</span>
          <span>Pending connection</span>
        </div>
      </header>

      {!enabled ? (
        <p className={styles.disabledNotice} role="status">
          Managed custom MCP connections are unavailable for this workspace.
        </p>
      ) : null}

      <div className={styles.connectionForm}>
        <fieldset className={styles.fieldset} disabled={!enabled || busy}>
          <legend className={styles.legend}>Server details</legend>
          <div className={styles.connectionGrid}>
            <div className={styles.field}>
              <label htmlFor={`${id}-name`}>Connection name</label>
              <input
                id={`${id}-name`}
                value={displayName}
                onChange={(event) => {
                  setDisplayName(event.currentTarget.value);
                  setFeedback({ kind: "none" });
                }}
                maxLength={256}
                autoComplete="off"
                spellCheck={false}
                placeholder="Building energy tools"
                aria-required="true"
              />
              <span className={styles.fieldHint}>Shown in your connections list.</span>
            </div>

            <div className={styles.field}>
              <label htmlFor={`${id}-url`}>MCP server URL</label>
              <input
                id={`${id}-url`}
                type="url"
                value={url}
                onChange={(event) => {
                  setUrl(event.currentTarget.value);
                  invalidateInspection();
                }}
                maxLength={2048}
                autoComplete="url"
                spellCheck={false}
                placeholder="https://mcp.example.com/mcp"
                aria-describedby={`${id}-url-hint`}
              />
              <span id={`${id}-url-hint`} className={styles.fieldHint}>Use a public HTTPS endpoint. Private endpoints require operator approval.</span>
            </div>

            <div className={styles.field}>
              <label htmlFor={`${id}-auth-scheme`}>Authentication</label>
              <select
                id={`${id}-auth-scheme`}
                value={auth.scheme}
                onChange={(event) => {
                  const scheme = event.currentTarget.value;
                  switch (scheme) {
                    case "none": updateAuth({ scheme: "none" }); break;
                    case "bearer": updateAuth({ scheme: "bearer", token: "" }); break;
                    case "basic": updateAuth({ scheme: "basic", username: "", password: "" }); break;
                    case "api-key": updateAuth({ scheme: "api-key", header: "Authorization", value: "" }); break;
                    default: break;
                  }
                }}
              >
                <option value="none">No authentication</option>
                <option value="bearer">Bearer token</option>
                <option value="basic">Username and password</option>
                <option value="api-key">API key</option>
              </select>
              <span className={styles.fieldHint}>Credentials stay in this page’s memory while you review the schema.</span>
            </div>

            {auth.scheme === "bearer" ? (
              <div className={styles.field}>
                <label htmlFor={`${id}-bearer`}>Bearer token</label>
                <input
                  id={`${id}-bearer`}
                  type="password"
                  value={auth.token}
                  onChange={(event) => updateAuth({ scheme: "bearer", token: event.currentTarget.value })}
                  autoComplete="off"
                  maxLength={4096}
                  spellCheck={false}
                  required
                />
              </div>
            ) : null}

            {auth.scheme === "basic" ? (
              <>
                <div className={styles.field}>
                  <label htmlFor={`${id}-username`}>Username</label>
                  <input
                    id={`${id}-username`}
                    value={auth.username}
                    onChange={(event) => updateAuth({ ...auth, username: event.currentTarget.value })}
                    autoComplete="off"
                    maxLength={256}
                    spellCheck={false}
                    required
                  />
                </div>
                <div className={styles.field}>
                  <label htmlFor={`${id}-password`}>Password</label>
                  <input
                    id={`${id}-password`}
                    type="password"
                    value={auth.password}
                    onChange={(event) => updateAuth({ ...auth, password: event.currentTarget.value })}
                    autoComplete="off"
                    maxLength={2048}
                    required
                  />
                  <span className={styles.fieldHint}>Encoded as a Basic credential in memory before the request.</span>
                </div>
              </>
            ) : null}

            {auth.scheme === "api-key" ? (
              <>
                <div className={styles.field}>
                  <label htmlFor={`${id}-api-header`}>API key header</label>
                  <input
                    id={`${id}-api-header`}
                    value={auth.header}
                    onChange={(event) => updateAuth({ ...auth, header: event.currentTarget.value })}
                    autoComplete="off"
                    maxLength={128}
                    pattern="[!#$%&amp;'*+.^_`|~0-9A-Za-z-]{1,128}"
                    spellCheck={false}
                    required
                  />
                </div>
                <div className={styles.field}>
                  <label htmlFor={`${id}-api-key`}>API key</label>
                  <input
                    id={`${id}-api-key`}
                    type="password"
                    value={auth.value}
                    onChange={(event) => updateAuth({ ...auth, value: event.currentTarget.value })}
                    autoComplete="off"
                    maxLength={4096}
                    spellCheck={false}
                    required
                  />
                </div>
              </>
            ) : null}
          </div>
        </fieldset>

        <div className={styles.actions}>
          <button
            className="button button-primary"
            type="button"
            onClick={inspectServer}
            disabled={!enabled || busy}
          >
            {operation.kind === "inspecting" ? "Inspecting endpoint…" : "Inspect tools"}
          </button>
          {busy ? (
            <button className="button button-secondary" type="button" onClick={cancelRequest}>
              Cancel request
            </button>
          ) : null}
          {operation.kind === "inspecting" ? (
            <p className={styles.pendingText} role="status">Waiting for the MCP server. The request stops after 45 seconds.</p>
          ) : null}
        </div>
      </div>

      {feedback.kind === "error" ? <p className={styles.feedbackError} role="alert">{feedback.message}</p> : null}
      {feedback.kind === "notice" ? <p className={styles.feedbackNotice} role="status">{feedback.message}</p> : null}

      {inspection ? (
        <section className={styles.reviewSection} aria-labelledby={`${id}-review-heading`}>
          <div className={styles.reviewHeader}>
            <div>
              <h3 id={`${id}-review-heading`}>Review server tools</h3>
              <p>Tool names and schema hashes identify what the server returned. They do not verify measurement quality or provenance.</p>
            </div>
            <div className={styles.inspectionMeta}>
              <span>{inspection.tools.length} {inspection.tools.length === 1 ? "tool" : "tools"} found</span>
              <span className={styles.untrusted}>Upstream annotations untrusted</span>
            </div>
          </div>

          <div className={styles.schemaLine}>
            <span>Schema digest</span>
            <code>{inspection.schema_digest}</code>
          </div>

          <p className={styles.semanticNotice}>
            You declare each tool’s action, result kind, and unit. Choose “Metered” only when your own review confirms the source and measurement; an upstream label or annotation is not evidence.
          </p>

          {inspection.tools.length === 0 ? (
            <div className={styles.emptyState}>
              <h4>No tools to review</h4>
              <p>This endpoint returned no tools, so there is nothing to stage. Check the server URL or try another endpoint.</p>
            </div>
          ) : (
            <>
              <div className={styles.toolToolbar}>
                <label htmlFor={`${id}-tool-filter`}>Filter tool names</label>
                <input
                  id={`${id}-tool-filter`}
                  value={toolFilter}
                  onChange={(event) => setToolFilter(event.currentTarget.value)}
                  maxLength={256}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="Find a tool"
                  disabled={busy}
                />
                <span>{selectedCount} of {MAX_SELECTED_TOOLS} selected</span>
              </div>

              {inspection.tools.length > MAX_SELECTED_TOOLS ? (
                <p className={styles.limitNotice}>
                  This server returned {inspection.tools.length} tools. Select up to {MAX_SELECTED_TOOLS} to stage in one connection.
                </p>
              ) : null}

              {filteredTools.length === 0 ? (
                <div className={styles.filteredEmpty}>No tool names match “{toolFilter}”.</div>
              ) : (
                <div className={styles.toolList}>
                  {filteredTools.map((tool) => {
                    const draft = drafts.find((item) => item.name === tool.name);
                    if (!draft) return null;
                    const toolId = `${id}-tool-${inspection.tools.indexOf(tool)}`;
                    const selectionDisabled = !draft.selected && selectedCount >= MAX_SELECTED_TOOLS;
                    return (
                      <fieldset className={styles.toolRow} key={tool.name} disabled={busy}>
                        <legend className={styles.visuallyHidden}>{tool.name}</legend>
                        <div className={styles.toolIdentity}>
                          <label className={styles.selectTool} htmlFor={`${toolId}-selected`}>
                            <input
                              id={`${toolId}-selected`}
                              type="checkbox"
                              checked={draft.selected}
                              disabled={selectionDisabled}
                              onChange={(event) => toggleSelection(tool.name, event.currentTarget.checked)}
                            />
                            <span>Select tool</span>
                          </label>
                          <div className={styles.toolNameBlock}>
                            <strong>{tool.name}</strong>
                            <span>Schema hash</span>
                            <code>{tool.schema_hash}</code>
                          </div>
                        </div>

                        {draft.selected ? (
                          <div className={styles.toolReview}>
                            <fieldset className={styles.actionGroup} disabled={busy}>
                              <legend>What can this tool do?</legend>
                              <div className={styles.actionGrid}>
                                {actionOptions.map((option, optionIndex) => {
                                  const actionId = `${toolId}-action-${optionIndex}`;
                                  return (
                                    <label className={styles.choice} key={option.value} htmlFor={actionId}>
                                      <input
                                        id={actionId}
                                        type="checkbox"
                                        checked={draft.actions.includes(option.value)}
                                        onChange={(event) => toggleAction(tool.name, option.value, event.currentTarget.checked)}
                                      />
                                      <span>{option.label}</span>
                                    </label>
                                  );
                                })}
                              </div>
                            </fieldset>

                            <div className={styles.resultFields}>
                              <div className={styles.field}>
                                <label htmlFor={`${toolId}-kind`}>Result kind</label>
                                <select
                                  id={`${toolId}-kind`}
                                  value={draft.kind}
                                  onChange={(event) => {
                                    const kind = event.currentTarget.value;
                                    const selectedKind = kindOptions.find((option) => option.value === kind)?.value ?? "";
                                    updateDraft(tool.name, (current) => ({ ...current, kind: selectedKind, reviewed: false }));
                                  }}
                                  required
                                >
                                  <option value="" disabled>Select a result kind</option>
                                  {kindOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
                                </select>
                              </div>
                              <div className={styles.field}>
                                <label htmlFor={`${toolId}-unit`}>Unit</label>
                                <input
                                  id={`${toolId}-unit`}
                                  value={draft.unit}
                                  onChange={(event) => {
                                    const unit = event.currentTarget.value;
                                    updateDraft(tool.name, (current) => ({ ...current, unit, reviewed: false }));
                                  }}
                                  maxLength={128}
                                  autoComplete="off"
                                  spellCheck={false}
                                  placeholder="kWh, W, or not applicable"
                                  required
                                />
                              </div>
                            </div>

                            <label className={styles.reviewConfirm} htmlFor={`${toolId}-reviewed`}>
                              <input
                                id={`${toolId}-reviewed`}
                                type="checkbox"
                                checked={draft.reviewed}
                                disabled={!isDraftReadyToConfirm(draft)}
                                onChange={(event) => {
                                  const reviewed = event.currentTarget.checked;
                                  updateDraft(tool.name, (current) => ({ ...current, reviewed }));
                                }}
                              />
                              <span>I reviewed this tool’s schema and confirmed the action, result kind, and unit above.</span>
                            </label>
                          </div>
                        ) : (
                          <p className={styles.notSelected}>Not selected. This tool will not be staged.</p>
                        )}
                      </fieldset>
                    );
                  })}
                </div>
              )}

              <div className={styles.stageFooter}>
                <div>
                  {!validDisplayName ? (
                    <p>Enter a connection name above before saving.</p>
                  ) : selectedCount === 0 ? (
                    <p>Select at least one tool to continue. No tools are selected automatically.</p>
                  ) : selectedNeedsReview ? (
                    <p>Finish the action, result kind, unit, and review confirmation for each selected tool.</p>
                  ) : (
                    <p>Save the reviewed selection as a pending connection.</p>
                  )}
                </div>
                <div className={styles.stageActions}>
                  <button className="button button-secondary" type="button" onClick={discardReview} disabled={busy}>
                    Discard review and clear credentials
                  </button>
                  <button
                    className="button button-primary"
                    type="button"
                    onClick={stageConnection}
                    disabled={!canStage || busy}
                  >
                    {operation.kind === "staging" ? "Saving pending connection…" : "Save pending connection"}
                  </button>
                  {operation.kind === "staging" ? (
                    <p className={styles.pendingText} role="status">Saving the reviewed tool selection. The request stops after 45 seconds.</p>
                  ) : null}
                </div>
              </div>
            </>
          )}
        </section>
      ) : null}
    </section>
  );

  function isCurrentRequest(number: number): boolean {
    return requestNumber.current === number;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isBoundedText(value: string, maximum: number): boolean {
  return value.length > 0 && value.length <= maximum && !controlCharacterPattern.test(value);
}

function isToolInspection(value: unknown): value is InspectedTool {
  if (!isRecord(value)) return false;
  return typeof value.name === "string" && isBoundedText(value.name, 256) && value.name.trim().length > 0 &&
    typeof value.schema_hash === "string" && schemaHashPattern.test(value.schema_hash);
}

function isInspectionResponse(value: unknown): value is MCPConnectionInspectionResponse {
  if (!isRecord(value) || !isRecord(value.inspection)) return false;
  const { schema_digest: digest, annotations_untrusted: annotationsUntrusted, tools } = value.inspection;
  if (typeof digest !== "string" || !schemaHashPattern.test(digest) || annotationsUntrusted !== true ||
      !Array.isArray(tools) || tools.length > 500 || !tools.every(isToolInspection)) return false;
  return new Set(tools.map((tool) => tool.name)).size === tools.length;
}

function isStageResponse(value: unknown, expectedDigest: string, expectedCount: number): value is MCPConnectionStageResponse {
  if (!isRecord(value) || value.ok !== true || !isRecord(value.account)) return false;
  const account = value.account;
  return typeof account.id === "string" && typeof account.toolkit === "string" &&
    (typeof account.site_id === "string" || account.site_id === null) &&
    typeof account.enabled === "boolean" && typeof account.auth_scheme === "string" &&
    typeof account.state === "string" && typeof account.verified === "boolean" &&
    value.schema_digest === expectedDigest && value.selected_tool_count === expectedCount;
}

function newToolDraft(tool: InspectedTool): ToolDraft {
  return { name: tool.name, selected: false, reviewed: false, actions: [], kind: "", unit: "" };
}

function clearAuthSecrets(auth: AuthInput): AuthInput {
  switch (auth.scheme) {
    case "none": return auth;
    case "bearer": return { scheme: "bearer", token: "" };
    case "basic": return { scheme: "basic", username: "", password: "" };
    case "api-key": return { scheme: "api-key", header: auth.header, value: "" };
  }
}

function encodeBasicCredentials(username: string, password: string): string | null {
  if (!isBoundedText(username, 256) || username.includes(":") || controlCharacterPattern.test(password) || password.length === 0) return null;
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  if (bytes.length > 3072) return null;
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  try {
    const encoded = window.btoa(binary);
    return encoded.length <= 4096 ? encoded : null;
  } catch {
    return null;
  }
}

function prepareConnection(urlValue: string, auth: AuthInput): PreparedConnection | null {
  const endpoint = urlValue.trim();
  if (!isBoundedText(endpoint, 2048)) return null;
  try {
    const parsed = new URL(endpoint);
    if ((parsed.protocol !== "https:" && parsed.protocol !== "http:") || parsed.username.length > 0 || parsed.password.length > 0 || parsed.hash.length > 0) {
      return null;
    }
  } catch {
    return null;
  }

  switch (auth.scheme) {
    case "none":
      return { url: endpoint, auth_scheme: "none", auth_header: "Authorization", credential: null };
    case "bearer":
      if (!isBoundedText(auth.token, 4096) || auth.token.trim().length === 0) return null;
      return { url: endpoint, auth_scheme: "bearer", auth_header: "Authorization", credential: auth.token };
    case "basic": {
      const credential = encodeBasicCredentials(auth.username, auth.password);
      return credential === null
        ? null
        : { url: endpoint, auth_scheme: "basic", auth_header: "Authorization", credential };
    }
    case "api-key":
      if (!apiHeaderPattern.test(auth.header) || !isBoundedText(auth.value, 4096) || auth.value.trim().length === 0) return null;
      return { url: endpoint, auth_scheme: "api-key", auth_header: auth.header, credential: auth.value };
  }
}

function isDraftReadyToConfirm(draft: ToolDraft): boolean {
  return draft.selected && draft.actions.length > 0 && draft.kind !== "" &&
    isBoundedText(draft.unit.trim(), 128) && draft.unit.trim().length > 0;
}

function isCompleteReview(draft: ToolDraft): boolean {
  return isDraftReadyToConfirm(draft) && draft.reviewed;
}

function buildReview(draft: ToolDraft): Review | null {
  if (!isCompleteReview(draft) || draft.kind === "") return null;
  return {
    name: draft.name,
    reviewed: true,
    actions: [...draft.actions],
    kind: draft.kind,
    unit: draft.unit.trim(),
  };
}
