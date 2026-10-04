# Read execution activity

Open **Activity log** in the web app to review recent gateway tool runs. The
view shows the tool, actual UTC recording time, duration, success or failure,
data kind when available, and optional scope references. **Older activity**
loads the next page; **Latest activity** refreshes the current page.

The gateway stores this metadata in `activity/activity.sqlite3` under its state
root. History survives execution-session deletion and gateway restarts, and is
included in the normal backup and restore commands. Tool arguments, results,
raw error messages, provider credentials and authenticated URLs are not stored.
Unknown tool requests use the fixed `unknown_tool` label.

## Access boundaries

The current bearer key determines the actor, workspace, allowed sites and shared
connection grants on every read. Members see their own executions for currently
granted resources. Workspace owners see their own executions. An owner cannot
use this endpoint to read another member's private history. Narrowing a member's
connection grants immediately hides history for the removed connections;
removing a member revokes their keys. Site-free managed workspaces return empty
history. Hosted clients cannot read local-mode executions.

The endpoint accepts pagination only. Clients cannot supply identity, workspace,
site or connection filters to expand their access.

## Use the TypeScript SDK

Use matching gateway and SDK source; this route was added after Python v0.3.0.

```typescript
const latest = await energy.activity({ limit: 50 });
if (latest.next_before !== null) {
  const older = await energy.activity({
    limit: 50,
    before: latest.next_before,
  });
}
```

The SDK calls authenticated `POST /activity`. Its JSON body accepts `limit`
(default 50, range 1–100) and optional positive `before`. Responses contain
`entries`, `next_before`, `retention_limit` and `recording_status`. Each entry has
an execution ID, monotonically increasing sequence, scope references and a
`success` or `failure` outcome. History reads perform no provider requests.

## Retention and availability

The default store retains up to 2,000 executions per actor, workspace and access
mode, with a shared gateway maximum of 10,000. Oldest records are removed during
append. Pagination uses an exclusive sequence cursor, so new runs do not shift
an older page into duplicates. Retention can remove rows between reads.

A recording failure preserves the tool's execution result. The running gateway
then reports `recording_status: "unavailable"` to indicate that its history may
be incomplete, even if it can still return existing records. This warning stays
set for that process. If the store cannot be read, the endpoint returns a fixed
HTTP 503 error and the web view offers a retry; other dashboard data remains
available. SQLite lock waits are bounded to 100 ms.

This is bounded operational history for `EnergyAgent.execute` tool runs. It is
not a tamper-proof audit service or a log of every HTTP request. Background job
lifecycle controls and history remain separate work.
