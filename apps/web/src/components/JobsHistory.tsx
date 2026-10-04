"use client";

import { AlertTriangle, Clock3, Download, RefreshCw, Trash2, XCircle } from "lucide-react";
import { useEffect, useId, useState, type FormEvent } from "react";
import type { JobHistoryResponse } from "@energy-agent-tools/sdk";
import type { JobHistoryState } from "@/lib/types";
import styles from "./JobsHistory.module.css";

type Job = JobHistoryResponse["jobs"][number];
type JobStatus = Job["status"];
type JobAction = "result" | "cancel" | "delete";

const UTC_FORMAT = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});

const BYTE_FORMAT = new Intl.NumberFormat("en-GB");
const EMPTY_SITE_NAMES: Readonly<Record<string, string>> = {};

const OPERATION_LABELS: Record<Job["operation"], string> = {
  heat_loss: "Heat loss",
  power_flow: "Power flow",
  battery: "Battery simulation",
  solar: "Solar simulation",
  network_power_flow: "Network power flow",
  network_dispatch: "Network dispatch",
};

const STATUS_OPTIONS: ReadonlyArray<{ value: JobStatus; label: string }> = [
  { value: "pending", label: "Pending" },
  { value: "running", label: "Running" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
  { value: "interrupted", label: "Interrupted" },
];

const STATUS_LABELS: Record<JobStatus, string> = {
  pending: "Pending",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
};

function statusClass(status: JobStatus): string {
  switch (status) {
    case "pending": return styles.statusPending ?? "";
    case "running": return styles.statusRunning ?? "";
    case "completed": return styles.statusCompleted ?? "";
    case "failed": return styles.statusFailed ?? "";
    case "cancelled": return styles.statusCancelled ?? "";
    case "interrupted": return styles.statusInterrupted ?? "";
    default: {
      const exhaustive: never = status;
      return exhaustive;
    }
  }
}

function historyHref({ before, status }: { before?: string | undefined; status?: string | undefined }): string {
  const query = ["view=jobs"];
  if (before) query.push(`job_before=${encodeURIComponent(before)}`);
  if (status) query.push(`job_status=${encodeURIComponent(status)}`);
  return `/?${query.join("&")}`;
}

function formatUtc(value: string | null, emptyLabel: string): string {
  return value ? `${UTC_FORMAT.format(new Date(value))} UTC` : emptyLabel;
}

function JobActionForm({ job, operation, label, destructive = false }: {
  job: Job;
  operation: JobAction;
  label: string;
  destructive?: boolean;
}) {
  const [actionState, setActionState] = useState<
    { kind: "idle" | "pending" | "failed" } | { kind: "ready"; url: string }
  >({ kind: "idle" });
  const pending = actionState.kind === "pending";
  useEffect(() => {
    if (actionState.kind !== "ready") return;
    return () => URL.revokeObjectURL(actionState.url);
  }, [actionState]);

  async function submitAction(event: FormEvent<HTMLFormElement>) {
    const form = event.currentTarget;
    event.preventDefault();
    if (pending) return;

    setActionState({ kind: "pending" });
    try {
      const formData = new FormData(form);
      formData.set("job_id", job.job_id);
      formData.set("operation", operation);
      const body = new URLSearchParams();
      for (const [name, value] of formData.entries()) {
        if (typeof value === "string") body.set(name, value);
      }

      const response = await fetch("/api/jobs/action", {
        method: "POST",
        body,
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Job action failed.");

      if (operation === "result") {
        const result = await response.blob();
        setActionState({ kind: "ready", url: URL.createObjectURL(result) });
        return;
      }

      window.location.assign("/?view=jobs");
    } catch {
      setActionState({ kind: "failed" });
    }
  }

  return (
    <form action="/api/jobs/action" method="post" onSubmit={submitAction} aria-busy={pending}>
      <input type="hidden" name="job_id" value={job.job_id} />
      <input type="hidden" name="operation" value={operation} />
      <button className={`button ${destructive ? styles.deleteButton : "button-secondary"}`} type="submit" disabled={pending}>
        {operation === "result" ? <Download size={14} aria-hidden="true" /> : null}
        {operation === "cancel" ? <XCircle size={14} aria-hidden="true" /> : null}
        {operation === "delete" ? <Trash2 size={14} aria-hidden="true" /> : null}
        {pending ? "Working…" : label}
      </button>
      {pending ? <span className={styles.actionPending} role="status">Submitting job action…</span> : null}
      {actionState.kind === "ready" ? <span className={styles.resultReady} role="status">
        Result ready. <a href={actionState.url} download={`energy-job-${job.job_id}.json`}>Save JSON</a>
      </span> : null}
      {actionState.kind === "failed" ? <span className={styles.actionError} role="alert">Could not complete this job action. Try again.</span> : null}
    </form>
  );
}

function JobActions({ job }: { job: Job }) {
  const isActive = job.status === "pending" || job.status === "running";
  const isTerminal = job.status === "completed"
    || job.status === "failed"
    || job.status === "cancelled"
    || job.status === "interrupted";

  return (
    <div className={styles.actions} role="group" aria-label={`Actions for ${OPERATION_LABELS[job.operation]} job`}>
      {job.status === "completed" ? <JobActionForm job={job} operation="result" label="Download result" /> : null}
      {isActive ? <JobActionForm job={job} operation="cancel" label="Cancel job" /> : null}
      {isTerminal ? <JobActionForm job={job} operation="delete" label="Delete job" destructive /> : null}
    </div>
  );
}

function JobScope({ job }: { job: Job }) {
  return (
    <details className={styles.scopeDetails}>
      <summary>Scope references</summary>
      <dl className={styles.scopeGrid}>
        <div><dt>Session</dt><dd>{job.session_id}</dd></div>
        {job.workspace_id ? <div><dt>Workspace</dt><dd>{job.workspace_id}</dd></div> : null}
        <div><dt>Access mode</dt><dd>{job.access_mode}</dd></div>
      </dl>
    </details>
  );
}

function JobRecord({ job, siteNames }: { job: Job; siteNames: Readonly<Record<string, string>> }) {
  const titleId = useId();
  const siteName = job.site_id === null ? "No site assigned" : siteNames[job.site_id]?.trim() || job.site_id;

  return (
    <li className={styles.jobItem}>
      <article className={styles.jobRecord} aria-labelledby={titleId}>
        <header className={styles.jobHeader}>
          <div className={styles.jobIdentity}>
            <h3 id={titleId}>{OPERATION_LABELS[job.operation]}</h3>
            <code className={styles.jobId}>{job.job_id}</code>
          </div>
          <span className={`${styles.statusBadge} ${statusClass(job.status)}`}>{STATUS_LABELS[job.status]}</span>
        </header>

        <dl className={styles.jobFacts}>
          <div className={styles.siteFact}>
            <dt>Site</dt>
            <dd>{siteName}</dd>
          </div>
          <div>
            <dt>Created</dt>
            <dd><time dateTime={job.created_at}>{formatUtc(job.created_at, "Not recorded")}</time></dd>
          </div>
          <div>
            <dt>Started</dt>
            <dd>{job.started_at ? <time dateTime={job.started_at}>{formatUtc(job.started_at, "Not started")}</time> : "Not started"}</dd>
          </div>
          <div>
            <dt>Finished</dt>
            <dd>{job.finished_at ? <time dateTime={job.finished_at}>{formatUtc(job.finished_at, "Not finished")}</time> : "Not finished"}</dd>
          </div>
          <div>
            <dt>Input size</dt>
            <dd>{BYTE_FORMAT.format(job.input_bytes)} bytes</dd>
          </div>
          <div>
            <dt>Output size</dt>
            <dd>{BYTE_FORMAT.format(job.output_bytes)} bytes</dd>
          </div>
          {job.error_code ? <div className={styles.errorFact}><dt>Error code</dt><dd><code>{job.error_code}</code></dd></div> : null}
        </dl>

        <div className={styles.jobFooter}>
          <JobScope job={job} />
          <JobActions job={job} />
        </div>
      </article>
    </li>
  );
}

function UnavailableJobs({ status }: { status: string | undefined }) {
  return (
    <div className={`empty-state compact-empty ${styles.emptyState}`} role="status">
      <div className="empty-icon" aria-hidden="true"><AlertTriangle size={17} /></div>
      <h3>Job history unavailable</h3>
      <p>The gateway job history could not be loaded. Retry to request the current page again.</p>
      <a className="button button-secondary" href={historyHref({ status })}>
        <RefreshCw size={14} aria-hidden="true" />Retry job history
      </a>
    </div>
  );
}

function ReadyJobs({ page, status, siteNames }: {
  page: JobHistoryResponse;
  status: string | undefined;
  siteNames: Readonly<Record<string, string>>;
}) {
  return (
    <>
      {page.jobs.length > 0 ? (
        <ol className={styles.jobList} aria-label="Background jobs">
          {page.jobs.map((job) => <JobRecord key={job.job_id} job={job} siteNames={siteNames} />)}
        </ol>
      ) : (
        <div className={`empty-state compact-empty ${styles.emptyState}`}>
          <div className="empty-icon" aria-hidden="true"><Clock3 size={17} /></div>
          <h3>No jobs returned</h3>
          <p>The gateway returned no jobs for this page and status filter.</p>
        </div>
      )}

      <nav className={styles.pagination} aria-label="Job history pages">
        <a className="button button-secondary" href={historyHref({ status })}>Latest jobs</a>
        {page.next_before ? (
          <a className="button button-secondary" href={historyHref({ before: page.next_before, status })}>Older jobs</a>
        ) : null}
      </nav>
    </>
  );
}

export function JobsHistory({ jobs, status, siteNames }: {
  jobs: JobHistoryState;
  status?: string;
  siteNames?: Readonly<Record<string, string>>;
}) {
  const id = useId();
  const page = jobs.kind === "ready" ? jobs.page : null;
  const resolvedSiteNames = siteNames ?? EMPTY_SITE_NAMES;

  return (
    <section className="content-section" aria-labelledby={`${id}-heading`}>
      <div className={`section-toolbar ${styles.toolbar}`}>
        <div>
          <h2 id={`${id}-heading`}>Job history</h2>
          <p>Review background simulation status, timing, and input/output sizes. Results contain the job output and download only for completed jobs.</p>
        </div>
        <span className="count-label">{page ? `${page.jobs.length} ${page.jobs.length === 1 ? "job" : "jobs"}` : "Unavailable"}</span>
      </div>

      <form className={styles.filters} action="/" method="get">
        <input type="hidden" name="view" value="jobs" />
        <label htmlFor={`${id}-status`}>Filter by status</label>
        <select id={`${id}-status`} name="job_status" defaultValue={status ?? ""}>
          <option value="">All statuses</option>
          {STATUS_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <button className="button button-secondary" type="submit">Apply filter</button>
      </form>

      {jobs.kind === "unavailable"
        ? <UnavailableJobs status={status} />
        : <ReadyJobs page={jobs.page} status={status} siteNames={resolvedSiteNames} />}
    </section>
  );
}
