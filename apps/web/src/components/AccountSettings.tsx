"use client";

import { useState } from "react";
import { ArrowRight, Check, Copy, Globe2, KeyRound, LogOut, ShieldCheck, UserRound } from "lucide-react";
import type { ManagedDashboardData } from "@/lib/types";
import { GatewayForm } from "./GatewayForm";
import styles from "./AccountSettings.module.css";

type KeyRecord = ManagedDashboardData["keys"][number];
type ScopedKeyStatus =
  | { kind: "active"; expiresAt: string }
  | { kind: "active-no-expiry" }
  | { kind: "expired"; expiresAt: string }
  | { kind: "revoked" };

function scopedKeyStatus(key: Pick<KeyRecord, "revoked" | "expires_at">, now: number): ScopedKeyStatus {
  if (key.revoked) return { kind: "revoked" };
  if (key.expires_at === null) return { kind: "active-no-expiry" };
  if (Date.parse(key.expires_at) <= now) return { kind: "expired", expiresAt: key.expires_at };
  return { kind: "active", expiresAt: key.expires_at };
}

function statusLabel(status: ScopedKeyStatus): string {
  switch (status.kind) {
    case "active":
    case "active-no-expiry": return "Active";
    case "expired": return "Expired";
    case "revoked": return "Revoked";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

function statusClass(status: ScopedKeyStatus): string {
  switch (status.kind) {
    case "active":
    case "active-no-expiry": return styles.statusActive ?? "";
    case "expired": return styles.statusExpired ?? "";
    case "revoked": return styles.statusMuted ?? "";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

function statusDetail(status: ScopedKeyStatus): string {
  switch (status.kind) {
    case "active": return `Expires ${formatDate(status.expiresAt)}`;
    case "active-no-expiry": return "No expiry recorded";
    case "expired": return `Expired ${formatDate(status.expiresAt)}`;
    case "revoked": return "Access has been revoked";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "date unavailable";
  return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function AccountSettings({
  data,
  onConnectAgent,
}: {
  data: ManagedDashboardData;
  onConnectAgent: () => void;
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const now = Date.now();
  const agentKeys = data.keys.filter((key) => "site_ids" in key.access);
  const keyRecords = agentKeys.flatMap((key) => {
    if (!("site_ids" in key.access)) return [];
    return [{ key, siteIds: key.access.site_ids, status: scopedKeyStatus(key, now) }];
  });
  const activeKeyCount = keyRecords.filter(({ status }) => status.kind === "active" || status.kind === "active-no-expiry").length;
  const expiredKeyCount = keyRecords.filter(({ status }) => status.kind === "expired").length;
  const revokedKeyCount = keyRecords.filter(({ status }) => status.kind === "revoked").length;

  async function copyGatewayUrl() {
    if (!data.publicGatewayUrl) return;
    try {
      await navigator.clipboard.writeText(data.publicGatewayUrl);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  }

  return (
    <section className={styles.accountSettings} aria-label="Account and settings">
      <header className={styles.intro}>
        <span className={styles.introIcon} aria-hidden="true"><UserRound size={20} strokeWidth={1.8} /></span>
        <div>
          <p className={styles.eyebrow}>ACCOUNT</p>
          <h2>Your workspace</h2>
          <p>Manage agent access and your gateway session.</p>
        </div>
      </header>

      <div className={styles.overviewGrid}>
        <article className={styles.card} aria-labelledby="account-identity-heading">
          <div className={styles.cardHeading}>
            <span className={styles.cardIcon} aria-hidden="true"><ShieldCheck size={17} /></span>
            <div>
              <h3 id="account-identity-heading">Identity and access</h3>
              <p>Your current workspace and permissions.</p>
            </div>
          </div>
          <dl className={styles.detailsList}>
            <div className={styles.detailRow}><dt>Workspace</dt><dd>{data.workspace.name}</dd></div>
          </dl>
          <details className="integration-details">
            <summary>Technical account details</summary>
            <dl className={styles.detailsList}>
              <div className={styles.detailRow}><dt>Gateway user ID</dt><dd><code>{data.identity.user_id}</code></dd></div>
              <div className={styles.detailRow}><dt>Workspace ID</dt><dd><code>{data.workspace.id}</code></dd></div>
              <div className={styles.detailRow}><dt>Workspace mode</dt><dd>{data.workspace.mode === "managed" ? "Managed" : "Operator"}</dd></div>
            </dl>
          </details>
          <div className={styles.permissionBlock}>
            <h4>Gateway permissions</h4>
            <ul className={styles.permissionList}>
              <li>
                <span>Manage workspace</span>
                <strong className={data.identity.can_manage_workspace === true ? styles.granted : styles.notGranted}>
                  {data.identity.can_manage_workspace === true ? "Granted" : "Not granted"}
                </strong>
              </li>
              <li>
                <span>Manage connections</span>
                <strong className={data.identity.can_manage_connections === true ? styles.granted : styles.notGranted}>
                  {data.identity.can_manage_connections === true ? "Granted" : "Not granted"}
                </strong>
              </li>
            </ul>
          </div>
        </article>

        <article className={styles.card} aria-labelledby="account-gateway-heading">
          <div className={styles.cardHeading}>
            <span className={styles.cardIcon} aria-hidden="true"><Globe2 size={17} /></span>
            <div>
              <h3 id="account-gateway-heading">Public gateway URL</h3>
              <p>Base address used when setting up agents and sharing access.</p>
            </div>
          </div>
          {data.publicGatewayUrl ? (
            <>
              <code className={styles.gatewayUrl}>{data.publicGatewayUrl}</code>
              <button className={styles.secondaryButton} type="button" onClick={() => void copyGatewayUrl()}>
                {copyState === "copied" ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
                {copyState === "copied" ? "Copied" : "Copy URL"}
              </button>
              {copyState === "error" ? <p className={styles.feedbackError} role="alert">Clipboard access was unavailable. Select the URL and copy it.</p> : null}
              {copyState === "copied" ? <p className={styles.feedbackStatus} role="status">Gateway URL copied.</p> : null}
            </>
          ) : (
            <div className={styles.missingGateway} role="status">
              <strong>Not configured</strong>
              <p>Ask the operator to set <code>ENERGY_PUBLIC_GATEWAY_URL</code> to the externally reachable HTTP(S) gateway address in the web app runtime, then restart or redeploy the web app. The address is used to build agent connection details.</p>
            </div>
          )}
        </article>
      </div>

      <section className={styles.card} aria-labelledby="account-agent-keys-heading">
        <div className={styles.keyHeader}>
          <div className={styles.cardHeading}>
            <span className={styles.cardIcon} aria-hidden="true"><KeyRound size={17} /></span>
            <div>
              <h3 id="account-agent-keys-heading">Scoped agent keys</h3>
              <p>Keys are limited to the sites selected when they were created. Secret values are shown only once.</p>
            </div>
          </div>
          <button className={styles.primaryButton} type="button" onClick={onConnectAgent}>
            Manage agent keys <ArrowRight size={15} aria-hidden="true" />
          </button>
        </div>

        <div className={styles.keySummary} aria-label="Scoped agent key counts">
          <div><strong>{activeKeyCount}</strong><span>Active</span></div>
          <div><strong>{expiredKeyCount}</strong><span>Expired</span></div>
          <div><strong>{revokedKeyCount}</strong><span>Revoked</span></div>
        </div>

        {keyRecords.length === 0 ? (
          <div className={styles.emptyKeys}>
            <h4>No scoped agent keys</h4>
            <p>Create an agent key and grant access to one or more workspace sites.</p>
          </div>
        ) : (
          <ul className={styles.keyList} aria-label="Scoped agent key status and expiry">
            {keyRecords.map(({ key, siteIds, status }) => {
              const siteNames = siteIds.map((siteId) => data.sites.find((site) => site.id === siteId)?.name ?? "Site unavailable");
              return (
                <li className={styles.keyRow} key={key.id}>
                  <div className={styles.keyMain}>
                    <div className={styles.keyNameLine}>
                      <h4>{key.name}</h4>
                      <span className={`${styles.status} ${statusClass(status)}`}>{statusLabel(status)}</span>
                    </div>
                    <p>Site access: {siteNames.join(", ")}</p>
                    <span className={styles.keyMeta}>Created {formatDate(key.created_at)} · {statusDetail(status)}</span>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className={`${styles.card} ${styles.sessionCard}`} aria-labelledby="account-session-heading">
        <div className={styles.sessionCopy}>
          <span className={styles.cardIcon} aria-hidden="true"><LogOut size={17} /></span>
          <div>
            <h3 id="account-session-heading">Current session</h3>
            <p>Signing out clears this browser’s gateway session.</p>
          </div>
        </div>
        <GatewayForm action="/api/logout" pendingLabel="Signing out…">
          <button className={styles.secondaryButton} type="submit"><LogOut size={15} aria-hidden="true" /> Sign out</button>
        </GatewayForm>
      </section>
    </section>
  );
}
