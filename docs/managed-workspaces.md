# Set up a managed workspace

Use a managed workspace to connect a system before creating a site. This guide
requires matching current Python, TypeScript SDK and web source. The published
Python v0.3.0 wheel predates managed workspace routes.

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

Shared workspace membership, connection ACLs, managed OAuth configuration and
site deletion remain under development. Current managed workspaces are private
to one owner.
