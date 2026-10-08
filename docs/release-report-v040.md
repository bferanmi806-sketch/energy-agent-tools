# Energy Agent Tools 0.4.0 release readiness

This release aligns the Python gateway, TypeScript SDK and self-hosted web
application at 0.4.0. It improves the path from a user's own provider account to
an existing agent. The gateway remains integration infrastructure.

## Completed

- Reviewed single-gateway discovery, canonical execution, composed workflows,
  account permissions and restart recovery. No new backend logic defect was
  verified. MCP descriptions now clarify which execution entry point to use.
- Added a public MCP cost-workflow journey over Octopus, OpenEnergyMonitor and
  CSV, including denial of cross-user account access before provider I/O.
- Made account connections the initial catalogue view. Data and engineering
  tools have a separate catalogue; temporarily blocked provider setup remains
  visible with its actual operator requirements.
- Moved approved Home Assistant authorization into its selected provider detail.
  Octopus keeps the user's API-key flow, clears its secret after verification,
  and provides the next site-mapping action. No OAuth support was invented.
- Grouped navigation into Connect, Workspace and Advanced. Added managed Account
  & settings, including real identity/permissions, gateway URL copy feedback,
  key expiry/status, agent-key controls and sign-out.
- Added shared Codex CLI, Claude Code and HTTP MCP setup instructions. The
  ChatGPT static-bearer authentication limitation is explicit. Agent keys remain
  site-scoped, shown once, and revocable.
- Improved operational typography, responsive forms, labelled provider marks,
  pending/error feedback and mobile section layout. The official Home Assistant
  identification asset has a third-party notice; other provider marks use
  semantic icons rather than invented logos.
- Added `scripts/check_release_versions.py` to existing CI. It checks Python,
  SDK/web manifests and locks, including the SDK's MCP client identity.

The [connection experience review](connection-experience-review.md) records
current Composio sources, the directly observed public screens, and the
sign-in boundary that prevented access to its private dashboard. The
[backend audit](release-backend-audit.md) records contracts and limits.

## Validation

- Full Python regression suite: **978 passed** on Python 3.12.
- SDK acceptance: **21 passed**; after changing the MCP client version, its
  three MCP transport tests passed again.
- Production web build and TypeScript checks passed. Web acceptance: **18 passed**,
  including managed Octopus onboarding, session-bound Home Assistant callbacks,
  custom MCP mapping/recovery, read-only agent boundaries and secure cookies.
  The final full Python run rebuilt and exercised these packages serially.
- Ruff check, formatting and mypy passed. Gateway, SDK, web and lock versions agree.
- Browser checks at 1440×1000 and 390×844: platform sign-in, failed provider key,
  successful retry, cleared secret, site creation/mapping, active connection,
  one-time scoped key, Settings and revocation. A real MCP client used that
  browser-issued key to discover and execute a fixture provider; after UI
  revocation, the endpoint returned HTTP 401.
- Installed Python wheel: offline artifact backup/restore and encrypted
  credential recovery smoke passed in a separate environment.

Browser evidence and package hashes accompany the prepared release artifacts.
Tests use fictional accounts and provider responses. They do not qualify a
physical meter or privately operated provider deployment.

## Release and remaining boundaries

The GitHub release is prepared as a **draft**, with a matching wheel, source
archive and SDK tarball. The web application is built from the same tagged source;
follow its README for the two self-hosted processes, HTTPS, fixed origins and
separate session encryption key. No deployment, PyPI publication or npm registry
publication is claimed.

Generic custom MCP OAuth and automatic MCP OAuth discovery remain unavailable.
ChatGPT custom MCP apps cannot directly use this gateway's static bearer key.
The retained evidence gaps include live private-provider journeys, an independently
authored connector, broader held-out agent evaluation and sustained deployment
soak/load evidence. These remain explicit future qualification work, not release
claims or grounds for rewriting the working gateway.
