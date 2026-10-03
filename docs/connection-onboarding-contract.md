# Octopus connection milestone

The first web onboarding provider is Octopus Energy Account. Its API origin is
fixed and the core already has credential validation, a bounded read probe and a
reviewed interval-energy capability. The first implementation maps a connection
to an existing owned site. Dynamic site creation, shared ACLs and arbitrary
provider endpoints follow separately.

The gateway derives the user and site from the authenticated, scoped session.
A connection request supplies the provider key, MPAN and meter serial. It cannot
supply a user, a provider URL or an arbitrary capability binding. Probe before
publishing an active connection; persist successful credentials only in the
existing encrypted AuthStore. Responses expose the public account shape and
fixed health diagnostics. Failure responses and logs cannot echo input keys or
provider response text.

Provider setup requirements must come from structured gateway metadata. The web
form consumes those requirements, submits through a server route with the same
origin checks and bounded input policy as sign-in, and reloads gateway state.
Secrets remain absent from React props, response payloads and browser storage.

After a successful connection, the same running agent must discover and use its
new account binding without rebuilding the process. Preserve explicit operator
bindings while refreshing account-derived bindings. Site ownership and current
session scope still govern capability resolution and execution. New host site
mounts are outside this first increment.

Acceptance uses a local fake provider and fictional credentials: successful
probe, encrypted persistence, reload after restart, use by the same live REST
and MCP agent, failed probe with no active account, foreign-site denial before
any outbound request, bounded secret input and no credential in logs/HTML.
Actual private Octopus access remains an external qualification gate.

The implementation is in current source. Identical user/site/meter submissions
use the same opaque account ID, so a lost response and retry do not create
ambiguous account bindings. Failed verification leaves an existing account and
credential untouched. The web app requires its matching gateway version; a
missing setup endpoint reports the gateway unavailable.

[Qualification evidence](evidence/connection-onboarding-oct03.json) separates
production contract checks from the fictional provider transport.

## Implemented lifecycle increment

Add a read-only provider verification action and explicit disconnect for the
same Octopus account. The session determines user and site, and the route looks
up the account in that exact scope before any provider request. Verification
returns current probe health and the public account. Its timestamp distinguishes
a new probe from the last successful verification stored on the account.
A failed probe must not replace credentials or claim a new successful timestamp.

Disconnect revokes the record and removes its encrypted credential. Existing
REST and MCP sessions must lose executable access on the next request. It does
not delete the public record. Reconnecting the same meter requires supplying and
verifying a key again. Browser actions retain exact Origin and bounded form
checks; responses contain fixed messages and no provider text or credentials.

Current source implements these actions at scoped account `verify` and
`disconnect` POST routes. The SDK exposes both. The Connections view shows
current check feedback, inline cancellation/confirmation and a revoked record
with reconnect guidance. [Lifecycle evidence](evidence/connection-lifecycle-oct03.json)
qualifies the software contracts and fictional provider journey.
