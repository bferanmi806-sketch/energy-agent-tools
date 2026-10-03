# Energy Agent Tools web app

This Next.js application reads the authenticated Python gateway through the TypeScript SDK. The catalogue comes from toolkit metadata; it does not have a separate provider list. Users start by browsing systems, then select setup requirements and map data to sites as the control plane is completed.

This first implementation uses operator-provisioned gateway access keys. It supports an Octopus API-key form for an existing owned site when the gateway has encrypted AuthStore storage enabled. Account registration, other provider forms, shared ACLs and custom MCP onboarding remain under development. It requires matching current gateway source with `GET /me` and scoped `connection-setup` routes; the published Python v0.3.0 wheel predates that route.

## Run locally

From the repository root:

```sh
npm --prefix packages/typescript ci --ignore-scripts
npm --prefix packages/typescript run build
npm --prefix apps/web ci --ignore-scripts
```

Copy `.env.example` to `.env.local` in this directory. Set the fixed gateway URL, exact web origin and independently generated 32-byte session encryption key. The Python gateway must be running with a valid principal and allowed sites. Start the app:

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
The new connection is immediately usable by existing REST and hosted MCP sessions.
Retrying the same meter for the same user and site updates one connection. A
rejected replacement does not overwrite its working credential. Accounts created
previously by other provisioning methods keep their IDs; this form does not merge
those records automatically.

The current site is chosen from operator-provisioned sites. A disabled form
explains when encrypted storage or an owned site is unavailable. Other toolkits
still show setup metadata and documentation. Dynamic site provisioning, asset
mapping and other provider forms remain separate work.

The HTTP acceptance runs the explicit `--octopus-fixture` transport with fictional
credentials. It never qualifies a private Octopus account or physical meter.

Octopus connection records now offer **Check connection** and **Disconnect**.
A check reports current probe health and its time. A failed check preserves the
last successful account verification and saved key. This action result is not a
persisted health history. Disconnect requires inline confirmation, removes the
saved credential and retains a revoked record. Existing REST and MCP sessions
cannot execute the revoked account on their next request. Connect the same meter
again with a valid key to restore access.
