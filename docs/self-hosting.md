# Run an authenticated self-hosted host

Use the `host` command when several operators or applications need the same local
Energy Agent Tools process. The host accepts operator-provisioned bearer tokens,
derives the user identity from the token digest, and creates scoped REST or MCP
sessions. It listens on loopback so a reverse proxy can provide the public TLS
endpoint.

The host does not create users, issue bearer tokens, or accept a user ID from a
request. Provision principals in the host configuration and give each operator a
token through your existing secret-management process.

## Define sites and principals

Every principal must own the sites it can select. `token_digest` is the hexadecimal
SHA-256 digest of the bearer token and must contain exactly 64 hexadecimal
characters. The raw token is never written to this configuration.

```json
{
  "sites": [
    {
      "id": "alice-home",
      "user_id": "alice",
      "name": "Alice home",
      "timezone": "Europe/London"
    },
    {
      "id": "bob-lab",
      "user_id": "bob",
      "name": "Bob lab",
      "timezone": "UTC"
    }
  ],
  "hosting": {
    "principals": [
      {
        "user_id": "alice",
        "allowed_site_ids": ["alice-home"],
        "token_digest": "0000000000000000000000000000000000000000000000000000000000000000",
        "expires_at": "2027-01-01T00:00:00+00:00",
        "token_id": "alice-token-2026-01"
      },
      {
        "user_id": "bob",
        "allowed_site_ids": ["bob-lab"],
        "token_digest": "1111111111111111111111111111111111111111111111111111111111111111",
        "token_id": "bob-token-2026-01"
      }
    ],
    "max_requests_per_minute": 60,
    "max_body_bytes": 1000000,
    "max_sessions_per_user": 10,
    "session_idle_timeout": 1800,
    "max_sessions_global": 1000
  }
}
```

The digest values above are valid-shaped placeholders. Replace them before
starting the host. Generate a digest inside your provisioning process, where the
raw token comes from a secret manager, then save only the returned digest in an
ignored configuration file:

```python
import os

from energy_agent_tools.hosting import token_digest

print(token_digest(os.environ["ENERGY_HOST_TOKEN"]))
```

`token_digest()` accepts a non-empty UTF-8 token up to 4096 bytes and returns its
SHA-256 digest. Keep the raw value in the secret manager and send it only as an
HTTPS bearer header at request time.

## Start the host

Save the configuration at a path readable by the host process and start it with:

```sh
uv run energy-agent host \
  --config host-config.json \
  --state-dir .energy-agent \
  --port 8765
```

The CLI binds Uvicorn to `127.0.0.1`. The state directory also contains the local
vault when the configuration enables `vault`; see [Manage local connections and
OAuth](authentication.md) for encrypted credentials and OAuth setup. Keep the
state directory private and back it up only together with the operator-managed
master key.

Send one bearer header with every request. The host computes its SHA-256 digest,
compares it with the configured principal using a constant-time comparison, and
rejects missing, duplicated, malformed, expired, or revoked credentials.

```http
Authorization: Bearer OPERATOR_TOKEN_FROM_SECRET_MANAGER
Content-Type: application/json
```

Create a session by selecting one of the authenticated principal's sites:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer ${ENERGY_HOST_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"site_id":"alice-home"}' \
  http://127.0.0.1:8765/sessions
```

The [TypeScript SDK](../packages/typescript/README.md) wraps these authenticated
REST routes and the site-scoped MCP endpoint.

The response contains a server-generated `session_id`. Use it with the REST routes
for search, execution, capabilities, skills, connections, and artifacts:

```text
POST   /sessions/{session_id}/search
POST   /sessions/{session_id}/execute
POST   /sessions/{session_id}/resolve
POST   /sessions/{session_id}/capability
GET    /sessions/{session_id}/skills
POST   /sessions/{session_id}/skills
POST   /sessions/{session_id}/skills/run
POST   /sessions/{session_id}/jobs
GET    /sessions/{session_id}/toolkits
GET    /sessions/{session_id}/connections
GET    /sessions/{session_id}/artifacts
DELETE /sessions/{session_id}/artifacts/{artifact_id}
DELETE /sessions/{session_id}
```

The `DELETE /sessions/{session_id}` route also deletes the artifacts owned by that
session. MCP clients can use `/mcp/{site_id}` when a site is explicit, or `/mcp`
when the authenticated principal has exactly one available site. MCP session
admission uses the same per-user and global limits as REST sessions.

## Rotate, expire, and revoke access

Set `expires_at` to an ISO 8601 timestamp with a timezone offset. An expired
principal is rejected before a REST or MCP request reaches the agent. Set
`revoked` to `true` when the token must stop working immediately. `token_id` is
operator metadata and must be unique when present; use it to identify a rotation
in an audit record.

For a restart-based rotation, edit the ignored configuration and restart the
`host` process. For an embedded host, `rotate_principals()` applies digest,
expiry, revocation, and token metadata changes atomically:

```python
import os
from datetime import UTC, datetime

from energy_agent_tools.hosting import Principal, token_digest

replacement = Principal(
    user_id="alice",
    allowed_site_ids={"alice-home"},
    token_digest=token_digest(os.environ["ENERGY_HOST_TOKEN_NEXT"]),
    expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    token_id="alice-token-2027-01",
)
unchanged_bob = Principal(
    user_id="bob",
    allowed_site_ids={"bob-lab"},
    token_digest=token_digest(os.environ["ENERGY_HOST_TOKEN_BOB"]),
    token_id="bob-token-2026-01",
)
host.rotate_principals({"alice": replacement, "bob": unchanged_bob})
```

The replacement mapping must preserve the current user/site mount topology. Restart
the host when adding or removing a principal's sites. When a principal becomes
inactive, its REST sessions and MCP sessions are removed. A new token must be
provided through the secret manager before the client sends the replacement
bearer header.

## Keep user, site, session, and account scopes separate

The request identity comes only from the bearer principal. A client cannot select a
different `user_id` in JSON. The host then applies these checks in order:

1. The principal must be active and must own the requested site.
2. The session is stored under that principal's user ID and selected site.
3. Every session route checks that the session belongs to the bearer principal.
4. Runtime account lookup is scoped by user, site, toolkit, and account state.
5. Asset and account links are checked before a connector receives a request.
6. Artifact reads and deletes require both the user and the session that created
   the artifact.
7. MCP sessions are admitted under the same user/site pair and cannot be reused by
   another principal.

Keep site IDs stable and unique. If one user has several sites, require the client
to send `site_id` when creating a session. If the principal has no site or includes
a site belonging to another user, host creation fails or the request is rejected.

## Set quotas and clean up state

The default host limits are:

| Resource | Default | Enforcement |
| --- | ---: | --- |
| Requests per user | 60 per rolling minute | REST and MCP admission |
| Request body | 1,000,000 bytes | Before route parsing |
| REST sessions per user | 10 | Session creation |
| MCP sessions per user | 10 | MCP admission |
| Sessions across the host | 1,000 | REST and MCP admission |
| REST idle session TTL | 1,800 seconds | Cleanup before create/use |

Set these values under `hosting` in the configuration. The body limit should also
be enforced at the reverse proxy. A maintenance task may call
`host.cleanup_sessions()` in an embedded deployment; the host already calls it
before session creation and use.

Workbench artifacts have separate defaults: 16,000 bytes for inline results,
20,000,000 bytes per stored artifact, 100,000,000 bytes per user,
1,000,000,000 bytes globally, and seven days of retention. Expired artifacts are
ignored by reads and listings and are deleted during a later persist operation.
Use `DELETE /sessions/{session_id}/artifacts/{artifact_id}` for immediate cleanup.
Expiring an idle session does not run artifact cleanup automatically, so use the
artifact route or the session delete route when you need immediate deletion.

## Put the loopback host behind a reverse proxy

Terminate TLS at a reverse proxy and keep the Python host bound to loopback. Pass
the original bearer header through explicitly, set a body limit at least as large
as the host limit, and set a read timeout appropriate for connector calls. A
minimal Nginx location is:

```nginx
server {
    listen 443 ssl;
    server_name energy.example;

    # Configure certificate and private key through your normal deployment system.
    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host 127.0.0.1:8765;
        proxy_buffering off;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
    }
}
```

Do not expose port 8765 directly. Do not use `X-Forwarded-User`, an arbitrary
query parameter, or a JSON field as the authenticated identity. The host must
continue to validate the bearer token after the proxy forwards it. Restrict the
proxy's upstream access to the loopback listener and apply your normal TLS,
firewall, and access-log redaction policy.

## Know the deployment limits

This host is an operator-managed gateway. It does not provide public registration,
hosted self-service OAuth, dynamic OAuth client registration, a central secret
service, or hardware-backed verification. OAuth authorization is an operator
workflow using the local loopback callback described in
[Manage local connections and OAuth](authentication.md). A provider grant may
also need to be revoked in the provider's own console.

Bearer principal metadata and the encrypted credential vault are local process
configuration. This implementation does not coordinate a shared vault or
principal database across multiple host replicas. If you run more than one host,
provision and rotate each instance deliberately, and keep its state and master key
permissions separate.

The MCP transport validates its upstream host against its loopback allowlist.
The example therefore sends the loopback Host header. Server MCP clients normally
omit Origin. Browser-origin access requires an explicitly reviewed origin policy;
do not strip or rewrite arbitrary Origin headers to bypass that check.

## Web identity and persisted workspace keys

Current source adds authenticated `GET /me`, returning only the principal's
allowed sites and their assets. It sends `Cache-Control: no-store`. The
TypeScript SDK exposes this as `identity()`. This route postdates v0.3.0.

`ControlStore` persists owner-private users, workspaces, site/asset mappings
and API-key metadata in `control/control.sqlite3`. It stores SHA-256 digests
of independently generated 32-byte keys, returns a raw key only at issuance,
and checks revocation/expiry on each authentication. Schema version 2 migrates
version 1 keys transactionally to `legacy-agent` access while preserving their
digests, expiry and revocation. A newer version is refused. Shared membership and ACLs are
not implemented in this first store.

An operator can pass a borrowed store to `create_host(control_store=store)`,
or enable `hosting.persistent_keys: true` for the CLI host. The operator owns
provisioning and closure of the store. Workspace keys are narrowed to the
intersection of workspace sites, configured principal allowances and the
runtime ownership map. They cannot create new mounts, widen an operator's
site policy or use a site-free shared mount. Existing sessions are still
checked against the key's current permitted sites on every request. The
`eat_` prefix is reserved for persisted keys while a store is enabled; adding
a key digest to static principal configuration does not bypass its revocation.

New keys require explicit access. `AgentKeyAccess(site_ids=[...])` grants one or
more existing sites in the key's workspace; execution also obeys the operator's
policy and runtime ownership map. Agent and migrated legacy keys cannot add or
disconnect provider accounts. They can check connection health. A
`ManageKeyAccess()` key can manage connections within the same site intersection.
`GET /me` reports `can_manage_connections`, and connection setup metadata reports
`management_key_required` for execution keys. An explicit key role never receives
a site-free session, even when its effective site grant is empty.

These roles do not create a managed multi-tenant deployment. The current host
still requires explicitly admitted operator principals; automatic workspace
registration, shared ACLs and dynamic topology remain unimplemented.

Current source gives hosted REST and MCP sessions a server-controlled resource
policy. Shared CSV/SQLite/model readers, configured executable adapters and all
imported MCP tools are operator resources and are hidden and denied in those
sessions. Unclassified extensions fail closed too. Public APIs, owned provider
connections and authorized artifacts/calculations remain usable. Direct local
Python and CLI sessions retain operator access. To use file data through the
host, it must first have an authorized tenant ingestion/connection contract;
setting `data_root` or reviewing a capability binding does not grant access.

Jobs persist the session's `local` or `hosted` access mode. Recovery requires
the same mode before restoring a session ID, so hosted recovery cannot inherit
artifacts imported through local operator access. Jobs written before this
field existed migrate conservatively to `local`; their results remain available
to local recovery, while hosted recovery is denied. Newly submitted hosted jobs
retain hosted recovery across restarts.

Example operator-side provisioning, with the existing `home` site model:

```python
from pathlib import Path
from energy_agent_tools.control_contracts import AgentKeyAccess
from energy_agent_tools.control_store import ControlStore
from energy_agent_tools.models import Site

store = ControlStore(Path(".energy-agent/control"))
store.create_user("alice", "Alice")
workspace = store.create_workspace("alice", "Home energy")
store.put_site(
    "alice", workspace.id, Site(id="home", user_id="alice", name="Home", timezone="Europe/London")
)
issued = store.create_key(
    "alice", workspace.id, "Agent access", access=AgentKeyAccess(site_ids=["home"])
)
# Deliver issued.token privately once. Public metadata is issued.key.
# Revoke with store.revoke_key("alice", workspace.id, issued.key.id).
store.close()
```

This is a persistence and authentication foundation. Normal-user registration,
workspace management, dynamic site creation and other provider connection forms
still need the control-plane API and web flow. The web app's current source
and run instructions are in [apps/web](../apps/web/README.md).

Backup and restore include the control database when it is under the private
state root's `control/` directory. A restored instance retains key revocations,
workspace scopes and site/asset mappings. Stop the host before deployment-wide
backup, as required for the other state databases. Backups containing control
records require the new source to restore; earlier v0.3.0 tooling does not
recognize the added database kind. Older archives remain readable.
