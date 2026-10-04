# Persistent job history

Numerical jobs have durable actor, workspace, site and local/hosted ownership.
The Jobs page reads metadata across originating sessions. Closing a REST
session leaves its numerical jobs available. Results and controls recheck the
current caller's access, even after a gateway restart.

## Discover jobs

An authenticated `POST /jobs` accepts only `limit`, `before` and `status`.
The gateway derives actor, workspace, hosted mode and permitted sites from the
current bearer principal. It does not accept caller-supplied ownership filters.
A workspace manager sees their own jobs. Shared members see their own jobs
within their current site grants.

```typescript
const page = await energy.jobHistory({ limit: 50, status: "completed" });
const older = page.next_before
  ? await energy.jobHistory({ before: page.next_before, status: "completed" })
  : null;
```

The page contains operation, status, ownership references, UTC timestamps,
input/output byte counts and an optional fixed error code. It excludes inputs,
results, raw error messages and private file paths. Pages sort by creation time
and job identifier, newest first. The exclusive cursor remains usable after
its job is deleted. Status changes require refreshing the page.

Page size is 1–100, default 50. Invalid queries and cursors fail with a fixed
400 error. An unreadable, locked or invalid metadata store fails with a fixed
503 `job_history_unavailable` response. A failed history load leaves the rest
of the web console usable.

## Recover results and control work

`POST /jobs/{job_id}` accepts `operation` with one of `status`, `result`,
`cancel` or `delete`. It derives the originating session and site from stored
ownership, then checks the current gateway and engine policy. It neither
registers nor closes a REST session, preserving that session's artifacts.

```typescript
const result = await energy.jobAction(jobId, { operation: "result" });
if (!result.ok) throw new Error("The job result is unavailable.");
```

The web page prepares completed results with a Save JSON link, cancels pending/running jobs and
deletes terminal jobs. It shows pending action state and a fixed failure
message without displaying the gateway's raw response.

Python callers can recover the same current-site job through a new bound
session:

```python
page = session.job_history(status="completed", limit=50)
result = await session.job_action(job_id, "result")
```

The existing session-bound `job()` API continues to submit numerical work and
supports its original session-specific commands. Job recovery associates a
caller with saved work; it does not rerun an interrupted calculation. A
manager restart marks previously running work interrupted. Pending jobs remain
queued for the next bounded worker run; opening history does not start work.

## Storage and limits

Job SQLite schema 2 adds workspace ownership. Recognized older schemas migrate
without changing identifiers or results. Legacy hosted rows with no workspace
are attributed only after live authorization validates their immutable
managed site. Local and operator jobs retain null workspace ownership.
Unknown future versions are refused. Backups label new job stores `jobs.v2` and
preserve private result files.

The existing default storage quotas remain 100 jobs per actor and 1,000 jobs
per manager, with bounded worker concurrency and input/output limits. Delete
finished jobs to free their quota. Metadata history does not provide
cross-member administration or a tamper-proof audit log.
