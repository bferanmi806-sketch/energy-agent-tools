import { Activity, AlertTriangle, CircleHelp, Clock3 } from "lucide-react";
import type { ExecutionLogResponse } from "@energy-agent-tools/sdk";
import type { ExecutionActivityState } from "@/lib/types";
import styles from "./ExecutionActivity.module.css";

type ExecutionEntry = ExecutionLogResponse["entries"][number];

const recordedAtFormat = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "short",
  timeZone: "UTC",
});

const durationFormat = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 2 });

function formatDuration(durationMs: number): string {
  return durationMs < 1000
    ? `${durationFormat.format(durationMs)} ms`
    : `${durationFormat.format(durationMs / 1000)} s`;
}

function executionCount(count: number): string {
  return `${count} ${count === 1 ? "execution" : "executions"}`;
}

function ScopeReferences({ entry }: { entry: ExecutionEntry }) {
  const references: ReadonlyArray<{ label: string; value: string | null }> = [
    { label: "Session", value: entry.session_id },
    { label: "Workspace", value: entry.workspace_id },
    { label: "Site", value: entry.site_id },
    { label: "Account", value: entry.account_id },
    { label: "Access mode", value: entry.access_mode },
  ];

  return (
    <details className={styles.scopeDetails}>
      <summary>Scope references</summary>
      <dl className={styles.scopeGrid}>
        {references.map(({ label, value }) => value === null ? null : (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function ActivityEntry({ entry }: { entry: ExecutionEntry }) {
  const succeeded = entry.outcome.kind === "success";
  const dataKind = entry.outcome.kind === "success" ? entry.outcome.data_kind : null;

  return (
    <li className={styles.activityItem}>
      <article className={styles.entry} aria-label={`${entry.tool}, ${succeeded ? "succeeded" : "failed"}`}>
        <header className={styles.entryHeader}>
          <h3>{entry.tool}</h3>
          <div className={`${styles.outcome}${succeeded ? ` ${styles.outcomeSuccess}` : ` ${styles.outcomeFailure}`}`}>
            <span>{succeeded ? "Succeeded" : "Failed"}</span>
            {entry.outcome.kind === "failure" ? <code>{entry.outcome.error_code}</code> : null}
          </div>
        </header>

        <dl className={styles.entryFields}>
          <div className={styles.recordedField}>
            <dt>Recorded at</dt>
            <dd><time dateTime={entry.recorded_at}>{recordedAtFormat.format(new Date(entry.recorded_at))}</time></dd>
          </div>
          <div>
            <dt>Duration</dt>
            <dd><Clock3 size={13} aria-hidden="true" />{formatDuration(entry.duration_ms)}</dd>
          </div>
          <div>
            <dt>Data kind</dt>
            <dd>{dataKind ?? "Not reported"}</dd>
          </div>
        </dl>

        <ScopeReferences entry={entry} />
      </article>
    </li>
  );
}

function UnavailableActivity() {
  return (
    <div className={`empty-state compact-empty ${styles.emptyState}`} role="status">
      <div className="empty-icon" aria-hidden="true"><CircleHelp size={17} /></div>
      <h3>Activity history unavailable</h3>
      <p>The activity feed could not be loaded. Try requesting the latest gateway page again.</p>
      <a className="button button-secondary" href="/?view=activity">Retry activity history</a>
    </div>
  );
}

export function ExecutionActivity({ activity }: { activity: ExecutionActivityState }) {
  return (
    <section className="content-section" aria-labelledby="execution-activity-heading">
      <div className={`section-toolbar ${styles.toolbar}`}>
        <div>
          <h2 id="execution-activity-heading">Execution activity</h2>
          <p>Recent tool runs visible to your current site and connection access.</p>
        </div>
        <span className="count-label">{activity.kind === "ready" ? executionCount(activity.page.entries.length) : "Unavailable"}</span>
      </div>

      {activity.kind === "unavailable" ? <UnavailableActivity /> : (
        <>
          {activity.page.recording_status === "unavailable" ? (
            <div className={styles.recordingNotice} role="status">
              <AlertTriangle size={16} aria-hidden="true" />
              <div>
                <strong>Execution recording is unavailable</strong>
                <p>Some executions may be missing. Existing records remain available.</p>
              </div>
            </div>
          ) : null}

          {activity.page.entries.length > 0 ? (
            <ol className={styles.activityList} aria-label="Recorded executions">
              {activity.page.entries.map((entry) => <ActivityEntry key={entry.sequence} entry={entry} />)}
            </ol>
          ) : (
            <div className={`empty-state compact-empty ${styles.emptyState}`}>
              <div className="empty-icon" aria-hidden="true"><Activity size={17} /></div>
              <h3>No execution records returned</h3>
              <p>The gateway returned no execution records for this page.</p>
            </div>
          )}

          <p className={styles.retention}>History is bounded to {activity.page.retention_limit.toLocaleString("en-GB")} recent executions per user and workspace, with a shared gateway limit.</p>
          <nav className={styles.pagination} aria-label="Execution activity pages">
            <a className="button button-secondary" href="/?view=activity">Latest activity</a>
            {activity.page.next_before !== null ? (
              <a className="button button-secondary" href={`/?view=activity&activity_before=${activity.page.next_before}`}>Older activity</a>
            ) : null}
          </nav>
        </>
      )}
    </section>
  );
}
