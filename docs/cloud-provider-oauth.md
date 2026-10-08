# Cloud provider OAuth

Managed workspace OAuth currently supports read-only connections to Tesla Energy
and Enphase Energy. Operators approve the provider application in
`hosting.managed_oauth_configurations`, alongside any Home Assistant OAuth
configuration. Workspace users cannot supply provider endpoints or application
credentials.

## Configure the applications

Register the exact web callback URL with each provider application. It must be
the deployment's `ENERGY_WEB_ORIGIN` followed by
`/api/workspace/oauth/callback`. Use HTTPS and do not add a query string or
fragment. Keep application secrets in the gateway environment; the config
contains environment variable names only.

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
      },
      {
        "id": "tesla-home",
        "name": "Tesla Energy",
        "provider": "tesla",
        "client_id": "TESLA_APPLICATION_CLIENT_ID",
        "client_secret_env": "TESLA_CLIENT_SECRET",
        "redirect_uri": "https://energy.example.org/api/workspace/oauth/callback",
        "region": "eu"
      },
      {
        "id": "enphase-home",
        "name": "Enphase Energy",
        "provider": "enphase",
        "client_id": "ENPHASE_APPLICATION_CLIENT_ID",
        "client_secret_env": "ENPHASE_CLIENT_SECRET",
        "api_key_env": "ENPHASE_API_KEY",
        "redirect_uri": "https://energy.example.org/api/workspace/oauth/callback"
      }
    ]
  }
}
```

`client_id` is the provider-issued application ID, not an environment variable
reference. `client_secret_env` and Enphase's required `api_key_env` are names of
environment variables whose values must be present when the gateway starts.
Do not put secret values in this file, workspace forms, logs, or agent prompts.
Managed Tesla authorization accepts `na` or `eu`; it defaults to `na`. China requires a separate application and authorization setup and is not enabled by this managed flow. Enphase uses its North
American API origin and defaults to `na`.

## Provider authorization details

| Provider | Authorization and token endpoints | Requested access | Provider-side discovery and read APIs |
| --- | --- | --- | --- |
| Tesla Energy | Authorize: `https://auth.tesla.com/oauth2/v3/authorize`<br>Token and refresh: `https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token` | `openid offline_access energy_device_data`. The token exchange sends the client secret in the form body and the selected Fleet API region as `audience`. It does not request `energy_cmds`. | `GET /api/1/products` lists products available to the user. The app asks for the numeric energy-site ID and verifies it with `GET /api/1/energy_sites/{energy_site_id}/site_info`; its read tool also calls `/live_status`. The managed Fleet API regional origin is `na` or `eu`. |
| Enphase Energy | Authorize: `https://api.enphaseenergy.com/oauth/authorize`<br>Token and refresh: `https://api.enphaseenergy.com/oauth/token` | Owner approves the app's selected access controls in Enphase. The authorization request has no OAuth `scope` parameter in this integration. Token requests use HTTP Basic client authentication. Each Monitoring API request also needs the app API key. | `GET /api/v4/systems` lists systems visible to the authorized owner. The app asks for a numeric system ID and verifies it with `GET /api/v4/systems/{system_id}/summary`. The provider also documents `/devices` and production, consumption, battery, import, and export telemetry endpoints; this integration currently exposes the summary read only. |

Tesla's `energy_device_data` scope covers energy live status, site information,
and energy history. The integration exposes only site information and live
status reads. It requests no setting or command scope and does not call Tesla
write endpoints. See Tesla's [third-party token flow](https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens),
[scope definitions](https://developer.tesla.com/docs/fleet-api/authentication/overview),
[energy endpoints](https://developer.tesla.com/docs/fleet-api/endpoints/energy),
and [regional origins](https://developer.tesla.com/docs/fleet-api/getting-started/regions-countries).

Enphase's developer application has a plan and selected access controls. Its
owner-approval OAuth flow returns an authorization code to the configured
callback. The integration exchanges it at `/oauth/token` using the registered
client ID and secret, then calls Monitoring API v4 with both the bearer token
and application API key. Consult Enphase's [Quick Start](https://developer-v4.enphase.com/docs/quickstart.html),
[plan limits](https://developer-v4.enphase.com/developer-plans), and
[telemetry endpoint guidance](https://developer-v4.enphase.com/docs/support).

Tesla operators must also complete Fleet API domain/public-key registration in each operating region before provider reads can work. See the [live qualification checklist](cloud-provider-qualification.md) for this requirement and both providers’ application, region and plan prerequisites.

## User connection flow

The workspace user signs into their own provider account in a new browser tab
and approves the application there. Energy Agent Tools does not ask for the
provider password. Before opening that tab, the user supplies a numeric
`resource_id`: a Tesla energy-site ID or an Enphase system ID. The provider
authorization may cover more than one site, but this connection is verified
against only the selected ID.

After callback verification, the connection is saved as disabled
`pending_mapping`. The user maps it to a site they own in the workspace; a
second provider read verifies the mapping before the connection becomes active.
Until then, agents cannot use it. See [managed workspaces](managed-workspaces.md)
for the workspace lifecycle.

## Disconnect and provider-side consent

Disconnect removes local credential access immediately. Tesla documents a
consent-management page for the user, but neither provider documents a server
OAuth revocation endpoint for this integration. The user must separately remove
the app's access in their provider account or portal. A cloud disconnect
therefore reports `upstream_revoked: null`; it must not be presented as proof
that the provider-side grant was revoked.

Tesla's browser consent-management URL is
`https://auth.tesla.com/user/revoke/consent?revoke_client_id={client_id}&back_url={return_url}`.
It lets an account holder modify or revoke app access; it is not a server token
revocation endpoint. For Enphase, the system owner manages app authorization in
their Enlighten/Enphase account; the [developer Quick Start](https://developer-v4.enphase.com/docs/quickstart.html)
describes the owner approval screen.
