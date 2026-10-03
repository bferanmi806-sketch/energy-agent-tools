"use client";

import { useId, useRef, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import type { ManagedDashboardData } from "@/lib/types";
import styles from "./WorkspaceSharing.module.css";

type Member = ManagedDashboardData["members"][number];
type Site = ManagedDashboardData["sites"][number];
type Connection = ManagedDashboardData["connections"][number];
type Grants = { siteIds: string[]; connectionIds: string[] };
type PendingAction =
  | { kind: "add" }
  | { kind: "save"; userId: string }
  | { kind: "remove"; userId: string }
  | { kind: "key"; userId: string };
type IssuedKey = { userId: string; memberName: string; token: string };
type CopyState = "idle" | "copied" | "unavailable";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseIdList(value: unknown): string[] | null {
  if (!Array.isArray(value) || !value.every((id) => typeof id === "string")) return null;
  return value;
}

function parseMember(value: unknown, expectedUserId: string): Member | null {
  if (
    !isRecord(value) || value.user_id !== expectedUserId || typeof value.workspace_id !== "string" ||
    typeof value.name !== "string" || (value.role !== undefined && value.role !== "member")
  ) return null;

  let grants: Member["grants"];
  if (value.grants !== undefined) {
    if (!isRecord(value.grants)) return null;
    const siteIds = parseIdList(value.grants.site_ids);
    const connectionIds = parseIdList(value.grants.connection_ids);
    if (!siteIds || !connectionIds) return null;
    grants = { site_ids: siteIds, connection_ids: connectionIds };
  }

  return {
    user_id: value.user_id,
    workspace_id: value.workspace_id,
    name: value.name,
    ...(value.role === "member" ? { role: "member" } : {}),
    ...(grants ? { grants } : {}),
  };
}

function parseMemberResponse(value: unknown, expectedUserId: string): Member | null {
  if (!isRecord(value) || value.ok !== true) return null;
  return parseMember(value.member, expectedUserId);
}

function isRemovalResponse(value: unknown): boolean {
  return isRecord(value) && value.ok === true && value.removed === true;
}

function parseIssuedKey(value: unknown, userId: string, siteIds: string[]): string | null {
  if (
    !isRecord(value) || value.ok !== true || typeof value.token !== "string" || value.token.length !== 47 ||
    !isRecord(value.key)
  ) return null;

  const key = value.key;
  const access = key.access;
  if (
    typeof key.id !== "string" || typeof key.name !== "string" || typeof key.token_prefix !== "string" ||
    key.token_prefix.length === 0 || key.user_id !== userId || !isRecord(access) || access.kind !== "agent"
  ) return null;
  const returnedSiteIds = parseIdList(access.site_ids);
  if (
    !returnedSiteIds || returnedSiteIds.length !== siteIds.length ||
    !siteIds.every((siteId) => returnedSiteIds.includes(siteId)) ||
    !value.token.startsWith(key.token_prefix)
  ) return null;

  return value.token;
}

function isShareableConnection(connection: Connection): boolean {
  return connection.enabled && connection.state === "active" && connection.site_id !== null;
}

function savedGrantsFor(member: Member, sites: Site[], connections: Connection[]): Grants {
  const availableSites = new Set(sites.map((site) => site.id));
  const siteIds = [...new Set((member.grants?.site_ids ?? []).filter((siteId) => availableSites.has(siteId)))];
  const selectedSites = new Set(siteIds);
  const availableConnections = new Set(
    connections
      .filter(isShareableConnection)
      .filter((connection) => connection.site_id !== null && selectedSites.has(connection.site_id))
      .map((connection) => connection.id),
  );
  const connectionIds = [...new Set((member.grants?.connection_ids ?? []).filter((id) => availableConnections.has(id)))];
  return { siteIds, connectionIds };
}

function grantsMatch(left: Grants, right: Grants): boolean {
  return left.siteIds.length === right.siteIds.length &&
    left.connectionIds.length === right.connectionIds.length &&
    left.siteIds.every((id) => right.siteIds.includes(id)) &&
    left.connectionIds.every((id) => right.connectionIds.includes(id));
}

function connectionName(connection: Connection): string {
  return connection.display_name?.trim() || connection.toolkit.replaceAll("-", " ");
}

async function postWorkspaceMutation(body: URLSearchParams): Promise<unknown> {
  const response = await fetch("/api/workspace/members", {
    method: "POST",
    body,
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
  });
  if (response.status === 401) {
    window.location.assign("/");
    throw new Error("Workspace session expired.");
  }
  if (!response.ok) throw new Error("Workspace request failed.");
  const payload: unknown = await response.json();
  return payload;
}

function cloneGrants(grants: Grants): Grants {
  return { siteIds: [...grants.siteIds], connectionIds: [...grants.connectionIds] };
}

export function WorkspaceSharing({
  members,
  sites,
  connections,
  gatewayUrl,
}: {
  members: ManagedDashboardData["members"];
  sites: ManagedDashboardData["sites"];
  connections: ManagedDashboardData["connections"];
  gatewayUrl: string | null;
}) {
  const router = useRouter();
  const formId = useId();
  const busyRef = useRef(false);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [failedAction, setFailedAction] = useState<PendingAction | null>(null);
  const [drafts, setDrafts] = useState(() => new Map(members.map((member) => [
    member.user_id,
    savedGrantsFor(member, sites, connections),
  ])));
  const [savedGrants, setSavedGrants] = useState(() => new Map(members.map((member) => [
    member.user_id,
    savedGrantsFor(member, sites, connections),
  ])));
  const [addedMembers, setAddedMembers] = useState<Member[]>([]);
  const [removedUserIds, setRemovedUserIds] = useState(() => new Set<string>());
  const [memberId, setMemberId] = useState("");
  const [confirmingRemovalId, setConfirmingRemovalId] = useState<string | null>(null);
  const [issuedKey, setIssuedKey] = useState<IssuedKey | null>(null);
  const [copyState, setCopyState] = useState<CopyState>("idle");

  const visibleMembers = [
    ...members,
    ...addedMembers.filter((added) => !members.some((member) => member.user_id === added.user_id)),
  ].filter((member) => !removedUserIds.has(member.user_id));
  const shareableConnections = connections.filter(isShareableConnection);
  const enrolledIds = new Set(visibleMembers.map((member) => member.user_id));
  const memberIdForAdd = memberId.trim();
  const pendingNow = pending !== null;

  function grantsFor(member: Member): Grants {
    return drafts.get(member.user_id) ?? savedGrants.get(member.user_id) ?? savedGrantsFor(member, sites, connections);
  }

  function savedFor(member: Member): Grants {
    return savedGrants.get(member.user_id) ?? savedGrantsFor(member, sites, connections);
  }

  function fail(action: PendingAction) {
    setFailedAction(action);
  }

  async function runMutation(action: PendingAction, body: URLSearchParams): Promise<unknown | null> {
    if (busyRef.current) return null;
    busyRef.current = true;
    setPending(action);
    setFailedAction(null);
    try {
      return await postWorkspaceMutation(body);
    } catch {
      fail(action);
      return null;
    } finally {
      busyRef.current = false;
      setPending(null);
    }
  }

  async function addMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busyRef.current) return;
    const form = new FormData(event.currentTarget);
    const submittedId = form.get("user_id");
    if (
      typeof submittedId !== "string" || !submittedId.trim() || submittedId.trim().length > 256 ||
      /[\u0000-\u001f\u007f]/.test(submittedId) || enrolledIds.has(submittedId.trim())
    ) {
      fail({ kind: "add" });
      return;
    }

    const userId = submittedId.trim();
    const response = await runMutation({ kind: "add" }, new URLSearchParams({ operation: "add", user_id: userId }));
    const member = parseMemberResponse(response, userId);
    if (!member) {
      fail({ kind: "add" });
      return;
    }

    const emptyGrants: Grants = { siteIds: [], connectionIds: [] };
    setAddedMembers((current) => [...current.filter((item) => item.user_id !== userId), member]);
    setRemovedUserIds((current) => {
      const next = new Set(current);
      next.delete(userId);
      return next;
    });
    setDrafts((current) => new Map(current).set(userId, emptyGrants));
    setSavedGrants((current) => new Map(current).set(userId, emptyGrants));
    setMemberId("");
    router.refresh();
  }

  async function saveGrants(member: Member) {
    if (busyRef.current) return;
    const draft = grantsFor(member);
    const body = new URLSearchParams({ operation: "update", user_id: member.user_id });
    for (const siteId of draft.siteIds) body.append("site_id", siteId);
    for (const connectionId of draft.connectionIds) body.append("connection_id", connectionId);

    const response = await runMutation({ kind: "save", userId: member.user_id }, body);
    if (!parseMemberResponse(response, member.user_id)) {
      fail({ kind: "save", userId: member.user_id });
      return;
    }

    const saved = cloneGrants(draft);
    setDrafts((current) => new Map(current).set(member.user_id, saved));
    setSavedGrants((current) => new Map(current).set(member.user_id, saved));
    router.refresh();
  }

  async function removeMember(member: Member) {
    if (busyRef.current) return;
    const body = new URLSearchParams({ operation: "remove", user_id: member.user_id });
    const response = await runMutation({ kind: "remove", userId: member.user_id }, body);
    if (!isRemovalResponse(response)) {
      fail({ kind: "remove", userId: member.user_id });
      return;
    }

    setRemovedUserIds((current) => new Set(current).add(member.user_id));
    setAddedMembers((current) => current.filter((item) => item.user_id !== member.user_id));
    setDrafts((current) => {
      const next = new Map(current);
      next.delete(member.user_id);
      return next;
    });
    setSavedGrants((current) => {
      const next = new Map(current);
      next.delete(member.user_id);
      return next;
    });
    setConfirmingRemovalId(null);
    setIssuedKey((current) => current?.userId === member.user_id ? null : current);
    setCopyState("idle");
    router.refresh();
  }

  async function issueMemberKey(event: FormEvent<HTMLFormElement>, member: Member) {
    event.preventDefault();
    if (busyRef.current) return;
    const saved = savedFor(member);
    const draft = grantsFor(member);
    const isDirty = !grantsMatch(draft, saved);
    const form = new FormData(event.currentTarget);
    const submittedName = form.get("name");
    if (
      !saved.siteIds.length || isDirty || typeof submittedName !== "string" ||
      !submittedName.trim() || submittedName.trim().length > 256
    ) {
      fail({ kind: "key", userId: member.user_id });
      return;
    }

    const keyName = submittedName.trim();
    const body = new URLSearchParams({ operation: "key", user_id: member.user_id, name: keyName });
    for (const siteId of saved.siteIds) body.append("site_id", siteId);
    const response = await runMutation({ kind: "key", userId: member.user_id }, body);
    const token = parseIssuedKey(response, member.user_id, saved.siteIds);
    if (!token) {
      fail({ kind: "key", userId: member.user_id });
      return;
    }

    setIssuedKey({ userId: member.user_id, memberName: member.name, token });
    setCopyState("idle");
    router.refresh();
  }

  function toggleSite(member: Member, siteId: string, selected: boolean) {
    setDrafts((current) => {
      const existing = current.get(member.user_id) ?? savedFor(member);
      const siteIds = selected
        ? [...existing.siteIds, siteId]
        : existing.siteIds.filter((id) => id !== siteId);
      const retainedSites = new Set(siteIds);
      const connectionIds = existing.connectionIds.filter((id) => {
        const connection = shareableConnections.find((item) => item.id === id);
        return connection !== undefined && connection.site_id !== null && retainedSites.has(connection.site_id);
      });
      return new Map(current).set(member.user_id, { siteIds, connectionIds });
    });
    setFailedAction(null);
  }

  function toggleConnection(member: Member, connection: Connection, selected: boolean) {
    setDrafts((current) => {
      const existing = current.get(member.user_id) ?? savedFor(member);
      if (!isShareableConnection(connection) || connection.site_id === null || !existing.siteIds.includes(connection.site_id)) {
        return current;
      }
      const connectionIds = selected
        ? [...existing.connectionIds, connection.id]
        : existing.connectionIds.filter((id) => id !== connection.id);
      return new Map(current).set(member.user_id, { ...existing, connectionIds: [...new Set(connectionIds)] });
    });
    setFailedAction(null);
  }

  async function copyIssuedKey(token: string) {
    try {
      await navigator.clipboard.writeText(token);
      setCopyState("copied");
    } catch {
      setCopyState("unavailable");
    }
  }

  const addPending = pending?.kind === "add";

  return (
    <div className={styles.layout} aria-busy={pendingNow}>
      <section className={styles.enrollment} aria-labelledby={`${formId}-enroll-title`}>
        <div className="section-subheading">
          <h2 id={`${formId}-enroll-title`}>Add a workspace member</h2>
          <p>Enroll an existing account on this self-hosted gateway. This does not create an account or send an invitation.</p>
        </div>
        <form className="workspace-form" onSubmit={addMember} aria-busy={addPending}>
          <div className="field-stack">
            <label htmlFor={`${formId}-user-id`}>Public user ID</label>
            <input
              id={`${formId}-user-id`}
              name="user_id"
              value={memberId}
              onChange={(event) => {
                setMemberId(event.currentTarget.value);
                setFailedAction(null);
              }}
              maxLength={256}
              autoComplete="off"
              required
              placeholder="Paste the person’s public user ID"
            />
            <span className="field-hint">The gateway confirms the existing user before adding them.</span>
          </div>
          <button className="button button-primary" type="submit" disabled={pendingNow || !memberIdForAdd || enrolledIds.has(memberIdForAdd)}>
            {addPending ? "Adding member…" : "Add member"}
          </button>
          {failedAction?.kind === "add" ? (
            <p className="notice notice-error" role="alert">Could not add this person. Check the public user ID and try again.</p>
          ) : null}
        </form>
      </section>

      <section className={styles.membersSection} aria-labelledby={`${formId}-members-title`}>
        <div className="section-subheading">
          <h2 id={`${formId}-members-title`}>Members and access</h2>
          <p>Choose each member’s sites first, then allow only the active mapped connections they need.</p>
        </div>
        {visibleMembers.length === 0 ? (
          <div className="empty-state empty-state-list">
            <h3>No members added</h3>
            <p>Add someone who already has an account on this gateway, then choose their workspace access here.</p>
          </div>
        ) : (
          <ul className={styles.memberList}>
            {visibleMembers.map((member, index) => {
              const draft = grantsFor(member);
              const saved = savedFor(member);
              const dirty = !grantsMatch(draft, saved);
              const selectedSites = new Set(draft.siteIds);
              const grantableConnections = shareableConnections.filter(
                (connection) => connection.site_id !== null && selectedSites.has(connection.site_id),
              );
              const confirming = confirmingRemovalId === member.user_id;
              const saving = pending?.kind === "save" && pending.userId === member.user_id;
              const removing = pending?.kind === "remove" && pending.userId === member.user_id;
              const keying = pending?.kind === "key" && pending.userId === member.user_id;
              const canIssueKey = saved.siteIds.length > 0 && !dirty;
              const keyError = failedAction?.kind === "key" && failedAction.userId === member.user_id;

              return (
                <li className={styles.member} key={member.user_id}>
                  <div className={styles.memberHeading}>
                    <div className={styles.identity}>
                      <strong>{member.name}</strong>
                      <code>{member.user_id}</code>
                    </div>
                    <div className={styles.memberStatus}>
                      {dirty ? <span className="status-badge status-caution">Unsaved changes</span> : <span className="status-badge status-good">Permissions saved</span>}
                    </div>
                  </div>

                  <div className={styles.scopeGrid}>
                    <fieldset className={styles.permissionGroup} disabled={pendingNow}>
                      <legend>Sites this member can access</legend>
                      {sites.length > 0 ? sites.map((site) => (
                        <label className="check-row" key={site.id}>
                          <input
                            type="checkbox"
                            checked={draft.siteIds.includes(site.id)}
                            onChange={(event) => toggleSite(member, site.id, event.currentTarget.checked)}
                          />
                          <span>{site.name}</span>
                          <small>{site.timezone}</small>
                        </label>
                      )) : <p className={styles.emptyGrant}>Create a site before assigning member access.</p>}
                    </fieldset>

                    <fieldset className={styles.permissionGroup} disabled={pendingNow || selectedSites.size === 0}>
                      <legend>Active connections on selected sites</legend>
                      {selectedSites.size === 0 ? (
                        <p className={styles.emptyGrant}>Select a site to see its mapped connections.</p>
                      ) : grantableConnections.length > 0 ? grantableConnections.map((connection) => (
                        <label className="check-row" key={connection.id}>
                          <input
                            type="checkbox"
                            checked={draft.connectionIds.includes(connection.id)}
                            onChange={(event) => toggleConnection(member, connection, event.currentTarget.checked)}
                          />
                          <span>{connectionName(connection)}</span>
                          <small>{sites.find((site) => site.id === connection.site_id)?.name ?? "Selected site"}</small>
                        </label>
                      )) : (
                        <p className={styles.emptyGrant}>No active mapped connections are available for the selected sites.</p>
                      )}
                    </fieldset>
                  </div>

                  <div className={styles.memberActions}>
                    <div className="inline-actions">
                      <button
                        className="button button-primary"
                        type="button"
                        disabled={pendingNow || !dirty}
                        onClick={() => void saveGrants(member)}
                      >
                        {saving ? "Saving permissions…" : "Save permissions"}
                      </button>
                      {confirming ? (
                        <div className={styles.confirmation} role="group" aria-label={`Confirm removal of ${member.name}`}>
                          <span>Remove {member.name} and their workspace grants?</span>
                          <button className="button button-secondary" type="button" disabled={pendingNow} onClick={() => setConfirmingRemovalId(null)}>Keep member</button>
                          <button className="button button-danger" type="button" disabled={pendingNow} onClick={() => void removeMember(member)}>
                            {removing ? "Removing…" : "Confirm removal"}
                          </button>
                        </div>
                      ) : (
                        <button
                          className="button button-secondary"
                          type="button"
                          disabled={pendingNow}
                          onClick={() => {
                            setFailedAction(null);
                            setConfirmingRemovalId(member.user_id);
                          }}
                        >
                          Remove member
                        </button>
                      )}
                    </div>
                    {failedAction?.kind === "save" && failedAction.userId === member.user_id ? (
                      <p className="notice notice-error" role="alert">Permissions could not be saved. Try again.</p>
                    ) : null}
                    {failedAction?.kind === "remove" && failedAction.userId === member.user_id ? (
                      <p className="notice notice-error" role="alert">This member could not be removed. Try again.</p>
                    ) : null}
                  </div>

                  <form className={styles.keyForm} onSubmit={(event) => void issueMemberKey(event, member)} aria-busy={keying}>
                    <div className={styles.keyIntro}>
                      <h3>Issue an agent key for {member.name}</h3>
                      <p>This separate key belongs to this member’s agent and uses the member’s saved site access.</p>
                    </div>
                    <div className={styles.keyFields}>
                      <div className="field-stack">
                        <label htmlFor={`${formId}-key-name-${index}`}>Key name</label>
                        <input
                          id={`${formId}-key-name-${index}`}
                          name="name"
                          maxLength={256}
                          required
                          defaultValue={`${member.name} agent`}
                          disabled={pendingNow || !canIssueKey}
                        />
                      </div>
                      <button className="button button-secondary" type="submit" disabled={pendingNow || !canIssueKey}>
                        {keying ? "Issuing key…" : "Issue member key"}
                      </button>
                    </div>
                    {!saved.siteIds.length ? <p className={styles.keyHint}>Save at least one site grant before issuing this member a key.</p> : null}
                    {saved.siteIds.length > 0 && dirty ? <p className={styles.keyHint}>Save permission changes before issuing a key for this access.</p> : null}
                    {keyError ? <p className="notice notice-error" role="alert">The member key could not be issued. Check the saved site grants and try again.</p> : null}
                  </form>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {issuedKey ? (
        <section className={`one-time-key ${styles.oneTimeKey}`} aria-labelledby={`${formId}-issued-key-title`}>
          <div className="one-time-key-heading">
            <h3 id={`${formId}-issued-key-title`}>Key for {issuedKey.memberName}’s agent</h3>
            <button
              className="text-button"
              type="button"
              onClick={() => {
                setIssuedKey(null);
                setCopyState("idle");
              }}
            >Hide key</button>
          </div>
          <p>Copy it now. The gateway shows the secret only once. Revoking this key requires the member’s agent to reconnect with a new key.</p>
          <div className="secret-value-row">
            <code>{issuedKey.token}</code>
            <button className="button button-secondary" type="button" onClick={() => void copyIssuedKey(issuedKey.token)}>Copy key</button>
          </div>
          {copyState === "copied" ? <p className="mutation-feedback" role="status">Copied to clipboard.</p> : null}
          {copyState === "unavailable" ? <p className="notice notice-error" role="alert">Clipboard access was unavailable. Select the key and copy it.</p> : null}
          {gatewayUrl ? <p className="field-hint">The member’s agent should connect to this workspace gateway: <code>{gatewayUrl}</code></p> : null}
          <button className="text-button" type="button" onClick={() => {
            setIssuedKey(null);
            setCopyState("idle");
          }}>Done</button>
        </section>
      ) : null}
    </div>
  );
}
