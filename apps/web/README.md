# Energy Agent Tools web app

This Next.js application reads the authenticated Python gateway through the TypeScript SDK. The catalogue comes from toolkit metadata; it does not have a separate provider list. Users start by browsing systems, then select setup requirements and map data to sites as the control plane is completed.

Sign in with an operator-provisioned key, managed workspace management key or scoped member agent key. Managed workspace owners can connect an Octopus meter or an approved Home Assistant instance before creating a site, map verified connections, create sites and assets, and issue or revoke agent keys. **Sharing** enrolls existing users with explicit site and connection grants and separate member keys. Members can use their granted connections without managing the owner's workspace. Follow the [managed workspace guide](../../docs/managed-workspaces.md) to bootstrap the gateway and provision users. Other provider forms, generic custom OAuth and custom MCP onboarding remain under development. Use matching current gateway source; the published Python v0.3.0 wheel predates managed workspace routes.

## Run locally

From the repository root:

```sh
npm --prefix packages/typescript ci --ignore-scripts
npm --prefix packages/typescript run build
npm --prefix apps/web ci --ignore-scripts
```

Copy `.env.example` to `.env.local` in this directory. Set the fixed gateway URL, exact web origin and independently generated 32-byte session encryption key. The Python gateway must be running with a valid operator principal or managed workspace key. Managed workspaces can start with no sites. Start the app:

```sh
npm --prefix apps/web run dev
```

Use `http://127.0.0.1:3000` when that is the configured origin. Sign in with the gateway access key. The app verifies the key at the gateway and uses an encrypted HttpOnly cookie. It sends provider operations through the gateway; browser code receives public metadata only. Changing the session encryption key signs out existing sessions.

For production use HTTPS at both external endpoints, the exact external web origin, an independent session key and the production build:

```sh
npm --prefix apps/web run build
npm --prefix apps/web run start
```

Keep the Python gateway, web process and private state under the operator's deployment and backup policy. A reverse proxy may supply HTTPS. Do not put provider or gateway keys into `NEXT_PUBLIC_` variables.

## Verification

```sh
npm --prefix apps/web run verify
```

The source includes a loopback gateway fixture in `scripts/web_reference_host.py`, with synthetic sites and the actual production registry. It does not prove access to physical energy systems.

## Connect an Octopus meter

Select **Octopus Energy Account** in Connect Apps. Enter the Octopus API key,
13-digit electricity MPAN and meter serial number. The gateway probes the fixed
Octopus API before saving an encrypted credential. Success opens Connections.
For a managed workspace, the verified connection waits for site mapping. Create
or select an owned site, then map the connection. The gateway verifies it again
before making it available to scoped REST and MCP sessions. For an operator key,
the form connects directly to an existing allowed site.
Retrying the same operator meter for the same user and site updates one connection. A
rejected replacement does not overwrite its working credential. Accounts created
previously by other provisioning methods keep their IDs; this form does not merge
those records automatically.

Operator keys choose from operator-provisioned sites. Managed workspace managers
can create sites and assets through Sites & Assets. Other toolkits show setup
metadata and documentation rather than an implemented connection form.

The HTTP acceptance runs the explicit `--octopus-fixture` transport with fictional
credentials. It never qualifies a private Octopus account or physical meter.

Octopus connection records now offer **Check connection** and **Disconnect**.
A check reports current probe health and its time. A failed check preserves the
last successful account verification and saved key. This action result is not a
persisted health history. Disconnect requires inline confirmation, removes the
saved credential and retains a revoked record. Existing REST and MCP sessions
cannot execute the revoked account on their next request. Connect the same meter
again with a valid key to restore access.

## Connect an agent from a managed workspace

Open **Connect my agent**, name the key and select its sites. Create the key and
copy the MCP configuration. The raw agent key appears once. The configuration
uses the public gateway URL and the selected site endpoint with bearer
authentication. Configure your client to accept that MCP transport and headers.

Agent keys cannot manage connections, sites, assets or keys. The key list retains
only metadata and supports revocation. A workspace management key has broader
permissions and should remain with the workspace owner.

The production HTTP acceptance in `test/managed-workspace.test.ts` runs a real
managed gateway and web server with synthetic provider responses. It checks
zero-site onboarding, mapping, scope boundaries and agent-key issuance. These
fixtures do not qualify a physical meter or real provider account.
