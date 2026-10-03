# Energy Agent Tools web app

This Next.js application reads the authenticated Python gateway through the TypeScript SDK. The catalogue comes from toolkit metadata; it does not have a separate provider list. Users start by browsing systems, then select setup requirements and map data to sites as the control plane is completed.

This first implementation uses operator-provisioned gateway access keys. Account registration, provider connection forms, shared ACLs and custom MCP onboarding remain under development. It requires current gateway source with `GET /me`; the published Python v0.3.0 wheel predates that route.

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
