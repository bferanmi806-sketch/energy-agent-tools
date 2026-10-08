# Cloud provider qualification

Tesla Energy and Enphase Energy have implemented OAuth connection and
read-only provider paths in this repository. This documentation does not claim
that a release has been published or that either provider has completed a fresh
real-account consent test. Fixtures verify request handling; they are not live
provider qualification.

## Current authentication coverage

The local first-run path documents Octopus Energy with HTTP Basic credentials,
Home Assistant with a bearer token, and Emoncms/OpenEnergyMonitor with an API
key. Managed workspace OAuth supports Home Assistant, Tesla Energy, and
Enphase Energy. A provider appearing in the toolkit catalogue does not by
itself mean it has a managed connection form, OAuth flow, or live qualification.
Do not label existing API-key or bearer-token connectors as OAuth integrations.

## Tesla Energy live qualification

Before enabling a Tesla configuration for a deployment, the operator should:

1. Submit an application request in the [Tesla Developer Portal](https://developer.tesla.com/)
   and obtain approved client credentials. Register the exact HTTPS callback.
2. Complete Tesla’s [Fleet API application onboarding](https://developer.tesla.com/docs/fleet-api/getting-started/what-is-fleet-api):
   host the application’s public key at
   `/.well-known/appspecific/com.tesla.3p.public-key.pem` on its registered domain,
   keep the private key outside public hosting, and use a partner token to call
   the register endpoint in each operating region. Application credentials alone
   do not complete Fleet API registration.
3. Select the supported Fleet API region for the account: `na` or `eu`. The managed flow does not support the separate Tesla China application and authorization setup.
4. Confirm that the operator's country supports Fleet API payment and billing
   limits. Tesla's API is pay-per-use; configure a payment method and a billing
   limit or calls can be suspended. Review [regional availability](https://developer.tesla.com/docs/fleet-api/getting-started/regions-countries)
   and [billing and limits](https://developer.tesla.com/docs/fleet-api/billing-and-limits).
5. Use an account that owns or has authorized access to a Powerwall energy
   site. Obtain that numeric site ID and complete a new owner-consent flow with
   only `openid offline_access energy_device_data` requested.
6. With that real grant, verify the selected site's `site_info` probe and live
   status read. Confirm the resulting connection stays pending until it is
   mapped to a workspace-owned site, then verify agent reads through that
   mapping. Confirm write/command operations remain unavailable.
7. Disconnect locally and confirm access is denied immediately. Separately
   revoke consent in Tesla's account consent page; the local result is still
   `upstream_revoked: null` because the integration has no server revocation
   endpoint.

Tesla says third-party authorization lets an application access a person's
vehicle or Powerwall on their behalf. The authorization-code flow and token
exchange are documented in [Third-Party Tokens](https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens).

## Enphase live qualification

Enphase qualification has a deployment-use gate as well as an API setup gate.
The operator must confirm that the actual use falls within the Enphase API v4
license before enabling the app. Enphase states that API v4 is for internal
business purposes and excludes public or consumer-facing applications,
services, and tools, as well as competing products. Do not assume a homeowner
or public consumer deployment is permitted; obtain written guidance from
Enphase where the intended use is unclear. See the official [API v4 license
notice](https://developer-v4.enphase.com/aboutproduct.html) and [license
agreement](https://enphase.com/api-license-agreement-v4).

After confirming license eligibility, the operator should:

1. Create an Enphase Developer Portal application, register the exact HTTPS
   callback, select the Monitoring API access controls, and load the issued
   client ID, client secret, and API key into the gateway's protected
   environment.
2. Check the plan permits the reads the deployment needs. The free Watt plan
   currently lists 1,000 hits per month and 10 hits per minute; paid plans need
   a credit card. The Kilowatt plan is listed at USD 249/month and Megawatt at
   USD 999/month; the paid plan pages list additional access and limits. Review
   [current plan details](https://developer-v4.enphase.com/developer-plans)
   before budgeting. The app's current Enphase tool reads a system summary; it
   does not expose all documented telemetry or commissioning endpoints.
3. Have the owner approve the application from their own Enlighten/Enphase
   account. With fresh consent, use the numeric system ID and verify the
   `/api/v4/systems/{system_id}/summary` read and resulting connection health.
   Check applicable call limits and plan controls using Enphase's [Quick
   Start](https://developer-v4.enphase.com/docs/quickstart.html) and
   [Monitoring API guidance](https://developer-v4.enphase.com/docs/support).
4. Verify the connection remains pending until mapped to a workspace-owned
   site, then verify agent access through that mapping. Disconnect locally and
   confirm access is denied immediately. The owner must separately remove app
   authorization in their provider account; the app reports
   `upstream_revoked: null` because no server revocation endpoint is configured.

The current Enphase flow uses the standard owner-approved authorization-code
flow, not the installer's password grant. Partner plan access is limited to
registered Enphase installers with at least 10 installations; it is not an
assumption operators should make for a developer or consumer app.

## Deferred providers

SMA is not implemented as a managed workspace provider. Its official API
authorization has distinct consent and token handling, calls for logout at
`POST https://auth.smaapis.de/oauth2/logout`, and requires production access
through SMA's contract process. SMA also describes API access as a B2B service
with usage-based charges. It needs its own reviewed backend profile and
commercial qualification before it can be recommended. See SMA's [access
control flow](https://developer.sma.de/api-access-control), [API plans and
FAQ](https://developer.sma.de/faq), and [developer journey](https://developer.sma.de/).

## Completion evidence

Mark a provider as live-qualified for a deployment only after an operator has
recorded all of the following against the exact deployed configuration:

- Provider application approval, production client credentials, and exact
  callback registration.
- Confirmed provider terms, region, payment/plan, and data-access eligibility.
- A fresh owner-consent event and a successful read against the real selected
  Tesla energy-site ID or Enphase system ID.
- Successful workspace mapping to an owned site and an agent read constrained
  to that connection.
- Immediate local disconnect behavior and separate provider-side consent
  removal, with `upstream_revoked: null` recorded honestly.

Until those checks are complete, report the implementation as fixture-tested
or awaiting live qualification, as applicable.
