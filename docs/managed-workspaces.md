# Set up a managed workspace

Use a managed workspace to connect a system before creating a site. This guide
requires matching current Python, TypeScript SDK and web source. The published
Python v0.3.0 wheel predates managed workspace routes. Use the matching 0.4.0 gateway, SDK and web release for this guide.

## Explore supported questions

The web app's **Skills** catalogue is available before you create any sites.
Search by question, workflow ID, capability or supporting tool. Select a workflow
to read its gateway-supplied sequence, pitfalls, parameters and evidence
requirements. The connected agent executes it through a scoped gateway session;
a catalogue entry does not establish provider availability or a completed study.

Management clients use `GET /workspace/skills` or TypeScript
`workspace.skills()`. This metadata route requires a managed workspace management
key and returns no account details or credentials. Operator and member agent
clients retain their existing scoped `session.skills()` and `session.runSkill()`
interfaces.

## Review tool runs

Use **Activity log** to inspect your own recent gateway executions. History
survives restarts and follows current site and shared connection grants. Owners
cannot use it to read another member's private execution history. See the
[activity guide](execution-activity.md) for pagination, retention, backup and
recording-health behavior.

## Create the workspace

Install the gateway from this checkout. From the repository root, run:

```sh
python -m pip install -e .
energy-agent bootstrap --state-dir ./state --name Home --owner-name "Home owner"
```

The command prints an owner, a managed workspace and a management key. Save the
management key when it appears. Its raw token is not stored by the gateway.
Each bootstrap call creates a separate owner and workspace and preserves earlier
keys. It does not recover an earlier raw key.

Bootstrap creates a private `state/vault.key` when no vault key source is
configured. An explicitly configured environment or file source must already
exist. A missing key for an existing encrypted vault, an invalid key, or a key
that cannot decrypt existing state prevents bootstrap from issuing a new key.

Start the gateway using the same state directory:

```sh
energy-agent host --state-dir ./state --managed-workspaces
```

The default address is `http://127.0.0.1:8000`. Managed hosting requires both
ControlStore and encrypted AuthStore storage. The flag enables managed workspace
keys; existing operator workspaces retain their operator policy restrictions.

## Connect and map a system

Start the [web app](../apps/web/README.md) against that gateway and sign in with
the management key. A workspace with no sites can browse Connect Apps and save
a verified connection. It cannot create a site-free execution session.

For an Octopus electricity meter:

1. Select Octopus Energy Account in Connect Apps.
2. Enter the API key, 13-digit MPAN and meter serial number.
3. Connect the system. A successful provider read saves an encrypted,
   disabled `pending_mapping` connection.
4. Create a site or select a site owned by this workspace.
5. Map the connection. The gateway verifies the provider again before activating
   it at that site.
6. Create a scoped agent key for the site and copy its agent configuration.

The provider secret is unavailable to execution while mapping is pending.
Mapping to a different workspace's site is rejected before provider I/O.
Repeating a successful mapping to the same site returns the existing active
connection. Moving an active connection to another site is not supported.

Check connection performs a provider read without replacing the credential.
Disconnect deletes the saved credential and retains a revoked record. Reconnect
the meter to verify and map it again. A failed provider read preserves prior
state. The API-key onboarding form currently supports Octopus; other toolkit
catalogue entries do not imply managed forms or live provider qualification.

## Approve a Home Assistant instance

The deployment operator can register native Home Assistant authorization
configurations in the gateway's hosting configuration. Workspace users choose
an approved configuration ID; they do not supply provider endpoints. For example:

```json
{
  "hosting": {
    "managed_oauth_configurations": [
      {
        "id": "home",
        "name": "Home Assistant",
        "base_url": "https://home.example.org",
        "client_id": "https://energy.example.org/",
        "redirect_uri": "https://energy.example.org/api/workspace/oauth/callback"
      }
    ]
  }
}
```

Start the managed host with `--config gateway.json`. Use an instance origin
without a path prefix. External URLs require HTTPS; loopback development URLs
may use HTTP. The client application and callback must share scheme, host and
port. The web callback must match `ENERGY_WEB_ORIGIN` followed by
`/api/workspace/oauth/callback`. The web form starts authorization through a
same-origin request, shows pending/error feedback and navigates to the validated
provider URL. The callback verifies its session-bound state before exchange and
returns to a clean connection page without the code or state in its URL.

The SDK methods `workspace.authConfigurations()`,
`workspace.beginAuthorization(...)` and `workspace.completeAuthorization(...)`
use the production gateway's workspace scope. Completion exchanges the code
and reads the selected entity before publishing an encrypted, disabled
`pending_mapping` connection. Mapping requires an owned site and a second
provider read. Health checks refresh an expiring active grant before probing.
Disconnect denies local use before attempting refresh-token revocation and
reports `upstream_revoked` as true, false or null when not attempted.

If verification or connection publication fails after a grant is exchanged,
the gateway attempts to revoke that grant. Failed remote revocations remain in
an encrypted, durable cleanup queue. Disconnect also removes local access and
queues the remote grant atomically before contacting the provider. A pending
cleanup never makes a connection usable again.

Disconnect attempts at most one remote revocation, beginning with the newest
queued grant and checking the current approved provider profile. It reports
`upstream_revoked: true` only when the attempt succeeds and no cleanup remains
for that connection. Older pending grants require additional scoped retries.

The configuration catalogue reports `pending_cleanup` for the current
workspace. The web app offers a retry when this count is nonzero; the SDK exposes
`workspace.retryAuthorizationCleanup({ configuration_id })`. Each request
attempts at most one grant and returns attempted, succeeded and pending counts.
Starting another authorization also attempts one cleanup. Retries require the
stored provider configuration to match the currently approved configuration;
changed profiles remain pending and are not contacted automatically. There is
no background retry worker in this version.

Managed Home Assistant connections permit reads only for their selected entity.
Direct state and history calls for another entity are denied before token
refresh or provider requests.

Changing or removing an approved configuration denies existing grant execution,
health and mapping before provider I/O. Disconnect still removes local access.
Restarting does not implicitly approve a stored endpoint. Home Assistant's
native authorization protocol does not provide the generic PKCE guarantee;
this path is distinct from generic configured OAuth and MCP authorization
discovery.

Synthetic gateway/SDK tests cover the lifecycle. The installed Home Assistant
authorization qualifier has not completed: startup timed out. Initial Docker
cleanup failed; follow-up checks confirmed the exact run-owned resources absent. This is not evidence of browser consent,
physical telemetry or a private installation.

## Connect an agent

Use a scoped agent key rather than the management key. Agent keys can execute
within their selected sites and cannot create sites, connect systems or issue
keys. Raw agent keys are returned only when issued; subsequent key listings
contain metadata. Revocation applies to later REST requests and requests on
existing MCP protocol sessions.

The hosted MCP endpoint is `/mcp/{site_id}` with bearer authentication. A site
created after host startup receives its MCP transport dynamically. The endpoint
uses the same gateway tools and capability resolver as the REST API.

The TypeScript SDK exposes management through `gateway.workspace()` and
execution through `gateway.createSession({ site_id })`. Workspace operations
derive the owner and workspace from the authenticated key; requests do not
accept caller-selected owner or workspace IDs.

## Preserve state

Keep the private state directory and vault key together when restarting the
deployment. The host reloads durable workspace topology before loading its
mapped connections. Sites have immutable ownership in this version.

Follow the [backup and restore guide](self-hosting.md) for stopped deployments.
Backups exclude the vault key unless explicitly requested. Preserve the key
separately when it is excluded. Managed hosting refuses an encryption key that
cannot decrypt the restored vault.

AuthStore schema 2 preserves encrypted pending revocations across restart and
backup/restore. Existing schema 1 vaults migrate when opened. Backup manifests
record the vault's actual schema version. Restoring pending cleanup requires
the original vault key and the matching approved provider configuration.

ControlStore schema 4 adds workspace membership and explicit connection grants.
Existing schema 1, 2 and 3 stores migrate when opened. Existing operator workspaces
keep their operator policy and do not gain managed sharing. Backup manifests
record the actual control schema; member keys, grants and revocations survive
restart and restore.

Generic custom OAuth, automatic MCP authorization discovery, owner transfer and
site deletion remain under development.

## Share selected connections

The workspace owner can enroll an existing instance user through **Sharing**.
Use the user's public user ID, never their management key. A teammate can obtain
their user ID from their own bootstrap result using the same instance state
directory. Bootstrap provisions a separate private workspace and prints that
user's initial management key; keep that key private to them. This version does
not send invitations or provide email-based registration.

1. Add the existing user. Membership starts with no site or connection access.
2. Select the sites and active mapped connections to share, then save permissions.
3. Create a member agent key for the saved site grants. Copy its one-time token
   or MCP configuration and provide it to the member through your chosen channel.
4. The member uses that key to connect their agent or sign into the web app.

The key identifies the member as the actor. The connected account and its
encrypted credential remain owned by the original owner. Members can discover
and execute only granted connections at their allowed sites. A newly connected
account remains private. Membership does not grant connection management,
membership management, the owner's artifacts, other members' datasets or jobs.

Removing a connection grant prevents later provider requests through existing
REST and MCP sessions. Permission changes invalidate captured workspace sessions;
create a fresh REST session or reconnect MCP to use the current grant. Requests
already dispatched to an external provider may finish. Removing membership
revokes its keys transactionally. Readding that user does not revive old keys.

Owner management routes are:

| Method | Route | Input |
| --- | --- | --- |
| GET | `/workspace/members` | None |
| POST | `/workspace/members` | `user_id` |
| PATCH | `/workspace/members/{user_id}` | Explicit `site_ids` and `connection_ids` |
| DELETE | `/workspace/members/{user_id}` | None |
| POST | `/workspace/members/{user_id}/keys` | `name` and `site_ids` |

Member grants are limited to 256 sites and 256 connections; a workspace supports
256 members. Connection grants require active accounts mapped to granted sites.
Agent key site grants further restrict membership grants. No wildcard shares
future accounts. The TypeScript workspace client exposes the same operations.
