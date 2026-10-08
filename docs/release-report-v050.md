# Energy Agent Tools 0.5.0 release readiness

Python gateway, TypeScript SDK and self-hosted web source use version 0.5.0.
This change adds read-only native Tesla Energy and Enphase Energy connections
alongside Home Assistant. It extends existing authorization, workspace,
encryption, routing and permission infrastructure.

## Completed

- Separate-tab provider handoff with popup-blocked, cancellation, failure and
  retry feedback. Users authorize their own provider accounts; provider
  passwords and application secrets never enter the connection form.
- Operator-approved cloud application profiles with fixed destinations,
  registered HTTPS callbacks, protected environment secrets and regional Tesla
  audiences. Confidential code exchange preserves existing PKCE and Home
  Assistant protocols. Refresh follows each provider's documented client
  authentication, including Tesla's refresh-token rotation.
- Verification before account publication, disabled pending site mapping,
  repeat verification before activation and workspace-scoped agent execution.
  Failed verification cannot activate a connection. Changing or removing an
  approved application denies existing grant use.
- Tesla site information and live status reads; Enphase system summary reads.
  Targets come from the connected account, not caller-controlled URLs or IDs.
  Results preserve gateway site identity, provider provenance and physical units.
- Immediate local credential removal on disconnect. Neither cloud profile
  configures an undocumented revocation endpoint; users remove provider consent
  separately and the gateway reports `upstream_revoked: null`.
- Matching REST and generated TypeScript contracts, the new
  `workspace.beginProviderAuthorization()` method, setup documentation and
  provider qualification checklists.

## Validation

- Full Python regression suite: **1,062 passed** on Python 3.12, including
  serial production SDK and web verification. SDK acceptance: **22 passed**.

- Installed 0.5.0 Python wheel in a clean environment: **84 focused tests passed**,
  covering provider reads, confidential authorization, configuration validation
  and both managed provider lifecycles. Imports came from the installed wheel.
- Production web build and TypeScript checks passed; **22 web acceptance tests
  passed**, including both cloud providers' callback, cancellation, replay,
  cross-session denial, site mapping and scoped execution journeys.
- Desktop and 390×844 mobile browser checks showed readable provider forms,
  clear operator requirements and no horizontal overflow. Screenshots use
  fictional applications; browser consent to a live provider is not claimed.
- Ruff checks and formatting, mypy, generated-contract checks and version
  agreement passed. Existing host reuse, catalogue scope and bearer-token health
  checks were preserved after full-suite regression findings.

## Deployment boundaries

Live Tesla and Enphase owner consent remains unqualified. Each deployment needs
approved applications, the exact HTTPS callback, eligible owner accounts and
provider region/plan access. Tesla additionally requires domain/public-key
registration in each operating region. Enphase's internal-business-use license
restriction must be checked against the actual deployment before enabling it.
See [configuration](cloud-provider-oauth.md) and
[qualification](cloud-provider-qualification.md).

Users currently supply numeric provider energy-site/system IDs; automatic
provider asset selection is not implemented. These tools expose read operations
only. SMA remains deferred pending its distinct consent protocol and commercial
access. Generic custom MCP OAuth, ChatGPT static-bearer integration limits and
long-term deployment/physical-provider evidence gaps remain as previously
recorded. Synthetic acceptance does not qualify an external provider or device.

No PyPI or npm registry publication or hosted deployment is claimed.
