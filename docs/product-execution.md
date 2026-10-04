# Product execution and completion audit

The [full objective](product-objective.md) is the completion contract. This
record extends the existing backend roadmap; it does not replace unfinished
gates with test counts. Baseline is main `cb3ac05`, with 419 local tests and
[passing CI](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37032700732).

## Managed no-auth storage and MCP response limits, October 4

Managed connections that need no authentication now stage an encrypted null
credential, reopen and activate through the existing revision-checked lifecycle.
They retain workspace, site and grant checks. The actual managed gateway was
tested against an owned HTTP MCP server in both bearer and no-auth modes; the
gateway management credential was not forwarded to the no-auth provider.

Approved HTTP responses now have a fixed cumulative 32 MiB limit. Declared and
streamed oversized responses are refused and closed, unsupported content encodings
are rejected before decoding, and later safe requests still work. Primary review
added regression coverage for arbitrarily long declared length strings.

Two Luna/max workers implemented the independent storage and transport changes.
Primary owned combined gateway acceptance and final review. All 915 Python tests passed, including the 98 affected checks.
Static checks, contract export and package builds also passed. An installed wheel also
passed encrypted restart/activation and real pinned HTTP response rejection.
The [qualification record](evidence/custom-mcp-lifecycle-bounds-oct04.json)
tracks the full suite and exact published-source CI separately. Source `9af38234`
passed all six [CI jobs](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37236506645),
with 915 tests on each of Python 3.11, 3.12 and 3.13.

Managed inspection/review routes, persistent definitions and recovery, private
endpoint workspace policy, health and the web journey remain open. This closes
two prerequisites, rather than the complete Add Custom MCP product requirement.

## Connection-owned MCP foundation, October 4

Trusted integrations can now import explicitly selected and reviewed MCP HTTP
tools into one managed connection. Ownership filters schemas, search, catalogue,
capability resolution and execution. Current stored lifecycle and grants apply
before publication or provider IO, and another account preference cannot retarget
a private tool. An approved transport pins DNS addresses, preserves TLS hostname,
blocks foreign origins/Host overrides and disables environment proxies/redirects.
Legacy imports remain operator-scoped.

Three Luna/max workers owned the bridge, network transport and managed-host
acceptance. Primary review owned shared contracts, integration and the actual
managed-gateway-to-MCP acceptance. The full suite passed 898 tests before the
final deprecated IPv6 site-local denial regression. All 59 affected checks passed
with that final fix, as did 18 SDK tests, 11 web tests, static checks and package
builds. The [qualification record](evidence/custom-mcp-foundation-oct04.json)
preserves the distinction. Published source `78dcb748` passed all six
[CI jobs](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37234773751),
including 899 tests on each of Python 3.11, 3.12 and 3.13.

The [MCP guide](mcp-review.md) documents the trusted integration contract. This
foundation does not complete Add Custom MCP. Managed inspection/review/staging,
persisted definitions and recovery,
private endpoint workspace policy, health and the normal web journey remain open.
Complete account/settings, generic OAuth/events, actual held-out agent evaluation,
physical-site evidence, external contribution, sustained operations and a coherent
platform release remain governed by the full objective.

## Persistent job history follow-up, October 4

Jobs now carry durable workspace ownership. Authenticated discovery spans
originating sessions and filters current actor, workspace, site grants and
hosted mode. Scoped recovery reads results and controls numerical work without
overwriting or closing the original REST session. Both web consoles provide
status filters, pagination, result preparation, cancellation and terminal
cleanup; Python and TypeScript SDKs use the same access rules.

The [job guide](jobs-product.md) records lifecycle and quotas. The
[qualification record](evidence/job-product-oct04.json) preserves all 828 passing
Python tests, 17 SDK tests, 11 production web tests and actual browser checks
using private numerical workers at synthetic sites. The browser verified
result readiness and a save link, but no saved-file download event was captured.
Pending work remains queued for a bounded worker run, and interrupted work is
not silently rerun. Full MCP job discovery and an explicit queue restart journey
remain follow-up work alongside custom MCP onboarding, complete settings,
broader actual agent evaluation, physical-site evidence, external contribution,
sustained operations and a coherent release.

## Durable execution activity follow-up, October 4

The gateway now persists a bounded allowlist of execution metadata, including
success, failure and cancellation outcomes. `POST /activity` derives current
actor, workspace, hosted mode, site and shared connection filters from the
bearer principal. The TypeScript SDK and both web consoles expose history and
exclusive older-page navigation. SQLite state survives restart and normal
backup/restore. Recording failure preserves the execution result and exposes
incomplete-history status. The [activity guide](execution-activity.md) documents
its limits; it is operational history rather than a tamper-proof audit service.

The [qualification record](evidence/execution-activity-oct04.json) preserves
35 focused Python checks, the corrected dataset backup cases, 16 SDK tests and
11 production web tests, plus desktop/mobile pagination and real storage-failure
checks. The first full Python run passed 813 tests and failed the two stale
backup assertions; the corrected cases passed separately. [CI for published activity source 2b0ca6f](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37214046465)
passed all six jobs, including all Python versions, container, upgrade and
installed Home Assistant qualification. The bound Python SDK also exposes
`session.activity()`; its follow-up passed nine focused activity/SDK checks. Custom MCP onboarding, jobs, complete account
and settings flows, broad actual agent evaluation, physical-site evidence,
external connector contribution, sustained operations and a coherent release
remain open.

## Skills catalogue follow-up, October 4

The managed workspace now browses gateway workflow metadata before creating a
site. `GET /workspace/skills` and TypeScript `workspace.skills()` require a
management key; agent and operator clients retain scoped session interfaces.
The web app shares a searchable catalogue across both consoles. It shows actual
intent, sequence, capabilities, supporting tools, pitfalls and optional
execution, parameter and evidence metadata. On narrow screens a compact workflow
selector keeps the selected details close to the search control. No provider
availability or execution success is inferred from a catalogue entry.

The [Skills qualification record](evidence/skills-catalogue-oct04.json) preserves
21 focused Python checks, 15 SDK tests, the production build/typecheck and 11
web tests, plus desktop/mobile browser search, selection and no-result checks.
The zero-site browser session made no provider requests.

This implements workflow discovery in the managed product. Execution remains
through the connected agent and the existing `runSkill` gateway interface.
Custom MCP onboarding, logs, jobs, complete account/settings flows, broad agent
evaluation, physical-site qualification, outside contribution, sustained
operations and a coherent release remain open.

## Workspace sharing follow-up, October 3

Managed workspaces now enroll existing users and grant selected sites and
active connections explicitly. The gateway separates member actor identity
from the owner of the resource. New membership has no access. Members use their
own agent keys; removing a member revokes those keys transactionally. Live key,
membership and workspace revision checks deny captured REST/MCP sessions before
credentials, refresh or provider IO. Separate keys cannot reuse each other's
MCP sessions. Artifacts, datasets and jobs remain private to their actor.

The SDK and web app expose enrollment, grants, member keys and removal. Primary
integration passed all 798 Python tests, 15 SDK tests and the production web build, typecheck and 11
web tests. Desktop and 390-pixel mobile browser acceptance used the real
gateway and a synthetic Octopus transport. A member logged in with the UI-issued
key and saw the shared connection without owner controls; UI removal made that
exact key fail authentication with HTTP 401. Browser inspection also led to a
local heading-layout correction. The [qualification record](evidence/workspace-sharing-oct03.json)
preserves the first full-suite failure and final validation state.
[CI for published source d9d02dd](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37160148256)
passed all six jobs, including Python 3.11/3.12/3.13, container, upgrade and
installed Home Assistant qualification.

Enrollment is by public user ID for existing instance users. It does not send
email invitations or provision an identity provider. Any membership or grant
change invalidates captured workspace sessions; REST clients open a new session
and MCP clients reconnect. A provider request dispatched before revocation may
finish. These synthetic sharing checks do not qualify physical-site telemetry
or close the broad evaluation, contribution, sustained-operation and release
gates. The remaining web work includes custom MCP, logs, jobs, complete account and
settings flows.

## Installed Home Assistant authorization follow-up, October 3

Source `8d25feb` makes installed native authorization part of the default Home
Assistant qualification. [CI attempt 2](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37156401053)
passed all jobs. Attempt 1 failed while fetching the dependency installer release
from GitHub, before qualification ran. The successful pinned
[authorization report](evidence/home-assistant-authorization-ci-oct03.json)
preserves both the existing provider-read checks and the added existing-user
login, code exchange, token refresh, API reads, revocation and rejection of
revoked access/refresh tokens. Owned Docker resources were verified absent.

This supersedes the installed-protocol uncertainty in the earlier
[managed Home Assistant record](evidence/managed-home-assistant-oct03.json).
Its local startup failure remains historical evidence. The CI run uses an
ephemeral development owner and synthetic entity. It does not establish browser
consent against installed Home Assistant, an installed-provider journey through
the managed gateway, physical telemetry or an independent final cleanup review.

Source `4f11e98` adds SDK acceptance for failed verification and two provider
revocation failures. No account is published; the encrypted cleanup remains
pending, agent/foreign workspace access is denied, and an owner retry clears it.
All 14 SDK tests passed in the primary's integration run. The disconnected web
row now retains its safe display name; production build and TypeScript check
passed. Shared workspace membership and connection ACLs remain the next control
plane implementation unit.

## Current audit

Updated through the Jobs and MCP recovery follow-ups; implementation evidence below
does not close the separate live, external-review or sustained-operation gates.

| Requirement | Current evidence | Status and next proof |
| --- | --- | --- |
| One gateway, Python SDK, scoped MCP, sites/assets/accounts | Runtime, capability resolver, SDK/MCP acceptance and twelve references | Implemented; broader live qualification remains open. |
| Consumption forecasting and forecast bill composition | Native forecast and forecast-bill SDK/MCP workflows; chronological model selection/calibration; synthetic three-month and high-resolution evidence | Implemented; automatic weather and cutoff acceptance. Physical-site forecast accuracy remains open. |
| Supported spike explanations | Scoped weather/equipment associations with matching windows and explicit missing evidence | Implemented; real-site qualification remains open. Associations do not establish causation. |
| Combined grid analysis | Aligned generation power and carbon intensity with known-value acceptance | Implemented; broader live qualification remains open. |
| Large-site processing | Million-row partitioned SDK qualification, bounded paging/streamed summaries, shared quotas/backup, high-resolution observed aggregation | Partial; timestamp indexing, gateway load and sustained operations remain open; a separate local store concurrency check is recorded below. |
| Complete workflows and provider substitution | Twelve offline success/failure references; three consumption provider contracts | Partial; contextual workflows, longer histories and deeper provider combinations needed. |
| Real connection lifecycle and provider qualification | Encrypted vault, onboarding, refresh/revoke, installed Home Assistant development qualification | Partial; physical-site access and remaining provider verification required. |
| Legitimate credential/application acquisition | Public providers qualified; private permissions not available | Open; use permitted applications/access, never fabricate identity, entitlement or hardware. |
| 100+ actual tasks, held-out and two model families | Preserved smaller actual runs with partial/inconclusive results | Open; unchanged scoring and verified model identity required; unavailable access is not a pass. |
| Independently reviewed discovery | 221 development/held-out intents, no independent human reviews | Open; independent relevance review and published measurements needed. |
| External connector authoring | Connector SDK and first-party examples | Partial; an outside authored connector must qualify through documented interfaces. |
| Deployment/recovery/load/soak and resilience | CI container, restart, state upgrade and backup/restore checks | Partial; representative sustained deployment and 30-day observations remain open. |
| Coherent new release with reproducible evidence | Published Python v0.3.0 and SDK archive; later web/control-plane source is CI-qualified | Partial; package a matching gateway, SDK and web revision after the next stable product boundary. |
| First-class TypeScript SDK | REST and official MCP transports, generated contracts, workspace management APIs and 15 acceptance tests | Implemented for current routes; new control-plane APIs need matching SDK coverage and distribution. |
| Connect Apps web product | Metadata catalogue, operator console, managed system-first Octopus flow, sites/assets, scoped agent setup and health; production HTTP and desktop/mobile checks | Partial; explicit shared connections are implemented; Skills discovery and durable execution activity are implemented; durable scoped Jobs history and MCP recovery are implemented; custom MCP, complete account management and settings remain open. |
| Hosted connection/control plane | Private managed workspaces, bootstrap identities, management/agent keys, encrypted scoped accounts, durable sites/assets, dynamic MCP, migrations and recovery acceptance | Partial; membership, explicit connection ACLs and approved Home Assistant OAuth are implemented; generic custom OAuth, events and full observability remain open. |
| Ecosystem journeys | Existing public, telemetry and engineering connectors with differing qualification | Partial; meters, PV/storage/EV, heat/BMS, industrial/files, engineering engines and reviewed MCP must qualify honestly according to accessible supported scope. |
| Accurate documentation and evidence | Append-only decisions, archived failures and qualification records | Ongoing; every completion claim needs current source and runnable evidence. |

## Phase sequence

1. Finish the energy-native core. Implement forecasting/bill composition,
   supported spike explanations, grid analysis and scalable time-series work.
   Continue provider, evaluation, external-contribution and operational
   qualification independently of unavailable external access. Publish a
   coherent release only after the backend contracts and gates justify it.
2. Keep the Python core and add the TypeScript SDK and developer web product.
   Catalogue entries come from toolkit metadata. Verify actual rendered
   connection-to-agent journeys, not just component compilation.
3. Complete the connection and hosted control plane, retaining first-class
   self-hosting. Verify workspace/user/key/connection ACL boundaries, stored
   state, OAuth refresh, dynamic MCP sessions, logs, events and migrations.
4. Expand energy journeys with qualified integrations and engineering engines.
   Record access and engine limitations separately from implementation status.

## First implementation contracts

The primary owns gateway contracts, orchestration, adapters, integration tests,
release evidence and final review. Independent workers own pure forecasting,
spike and grid analysis modules and their focused tests in isolated worktrees.
Workers use GPT-6 Luna/max. Shared runtime files have one writer.

The forecasting path must retrieve historical metered interval energy,
use supplied or resolved past/future contextual data when available, predict a
future interval series with uncertainty and model-validation diagnostics, and
resolve tariffs for that same future window. Historical data remains metered;
predicted energy is forecast; monetary estimates are calculated with explicit
forecast basis. Unknown tariffs, missing intervals or inaccessible sources
produce visible refusals. Backtesting uses chronological splits; future data
cannot enter model fitting or error calibration.

Two model structures were considered: a repeated weekly calendar profile and
a context-conditioned regression. The implementation retains an offline
calendar baseline and compares a context-conditioned candidate against it
using held-back chronological errors. Relevant context can improve predictions
only when both historical observations and future covariates are supplied.
Empirical residual bands are model diagnostics, not guaranteed real-world
coverage. No external forecasting service is required.

Completion is unproven until every requirement in the full objective has direct
current evidence. External access, independent review and sustained observation
remain open when unavailable; independent work continues.

## Implemented core wave, October 2

Consumption forecasts and forecast billing now have native capabilities and
composed SDK/MCP workflows. Their offline acceptance retrieves three months
in bounded calls, preserves metered history, forecasts eight days and checks
a 192 kWh/42.72 GBP synthetic fixture. The runnable reference also exercises
an actual missing-tariff-coverage refusal. Model selection and calibration use
separate chronological weeks, followed by a final all-history fit. Explicit
past/future temperature artifacts can condition the model; automatic context
fetching and real-site forecast accuracy remain unqualified.

Building spike analysis now reports supported equipment/weather associations
and missing evidence. SDK/MCP acceptance covers distinct context assets,
matching explicit endpoints and foreign-artifact refusal. Grid analysis now
compares aligned generation power and carbon intensity, with independent
known-value assertions and preserved carbon species. These changes address
implementation gaps in the baseline table; they do not clear live-provider,
independent-review, agent-evaluation or sustained-operation gates.

Partitioned persistent time-series processing is now integrated in commit
`1bf4fcf4e035adbbcd8fbecc931dbd73c3a92a33`. Native dataset tools import and read scoped persistent chunks,
page within gateway response limits, stream energy summaries, and select
bounded windows for existing forecast workflows. Small artifacts and datasets
share quota accounting, retention and the same SQLite backup/restore boundary.
SDK and MCP acceptance cover provider substitution and user/session isolation.
The full suite passes 507 tests; Ruff and mypy also pass.

The reproducible million-row SDK qualification is recorded in
[evidence/large-timeseries-gateway-oct02.json](evidence/large-timeseries-gateway-oct02.json).
It checks an independently calculated total, a 1,440-row day window, inline
paging, foreign-scope rejection and restored dataset access. Peak traced Python
allocation is 2,336,690 bytes. This excludes native SQLite memory and is not an
RSS measurement. Timing includes allocation-tracing overhead.

Temporal selection still scans chunks. Timestamp indexing, gateway-load and
sustained-operation qualification remain open. A later local store concurrency
check is recorded separately below. The fixture is synthetic and does not establish physical meter access.
The TypeScript SDK, Connect Apps application and full connection control plane
remain in the completion scope.

## Complete observed aggregation, October 2

Commit `3c84cefb8a39aa02c3a39b2d31b384f2aa32172b` adds streamed observed interval-energy
aggregation and uses it before forecast model fitting. Complete one-minute meter
history can now become 15/30/60-minute model input without loading all source rows.
Measured interval amounts remain metered with explicit aggregation lineage;
gaps, overlaps and target boundaries requiring a split are refused.
The SDK/MCP qualification covers both a preloaded 132,480-row dataset and a
resolved dataset provider. Its independently expected eight-day forecast and
bill are 576 kWh and 123.36 GBP. These values describe a synthetic fixture.

The full 546-test suite, Ruff and mypy pass. The pinned record is
[evidence/high-resolution-forecast-oct02.json](evidence/high-resolution-forecast-oct02.json).
Automatic contextual weather retrieval is the next integration unit. Physical
meter access, real forecast accuracy, independent agent evaluation, outside
connector contributions and sustained operation remain qualification gates.

## Automatic weather and historical cutoff, October 2

Commit `5b0c5b5ae20e9c8e37e204724123e880e424ac8e` integrates automatic historical
and future temperature retrieval, bounded alignment, provider substitution and
visible fallback/refusal evidence. Historical weather remains estimated analysis
and future weather remains forecast. Alignment assumptions reach the consumption
forecast. The default historical cutoff is today's local midnight; predictions
begin tomorrow. The model reports the gap and trains only through that cutoff.
SDK/MCP acceptance checks a frozen midday clock and forbids future meter requests.

The local full run passed 594 tests in 547.74 seconds. Six subsequently added
cases passed separately; the final collection contains 600 tests. Ruff and mypy
pass. The pinned record is [forecast weather evidence](evidence/forecast-weather-oct02.json).
A registered gateway request returned 24 finite hourly estimated temperatures
for one public Berlin reference day. The [live record](evidence/historical-weather-live-oct02.json)
and `scripts/qualify_historical_weather.py` preserve its actual response and
reproduction path. This does not qualify physical meters or forecast accuracy.
Automatic weather still uses one historical request, limited to 366 days.

## Local storage concurrency and recovery, October 2

Commit `79732b3790c8aed38d77ad834a7eaa5192f884f3` adds a reproducible local store
qualification. Four separate store instances import 100,000 generated rows
while another store pages and streams a preloaded dataset during an open write
transaction. SQLite serializes writes. All 25 foreign-scope checks and both
shared-quota probes pass. One backup restores all five datasets; every row and
independently calculated total is checked before and after restore, for 102,000
rows. The [pinned record](evidence/dataset-concurrency-oct02.json) includes timings
and `scripts/qualify_dataset_concurrency.py` reproduces the assertions.

This is five threads in one process on local SQLite. Gateway load, multi-host
operation, timestamp indexing and a 30-day soak remain open. Independent agent
evaluation, outside connector contribution and private physical-site evidence
remain separate gates. The TypeScript SDK, Connect Apps product and persistent
connection control plane remain required parts of the active goal.


## Core release candidate, October 2

Historical weather chunking in `9a94835331fa0e9d99aff8d20c32199d9b992cd1`
supersedes the earlier one-request limit. SDK/MCP fixtures cover 18 months,
19 historical requests, 26,304 aligned half-hour rows and visible failure of
a middle chunk. The published CI revision `53a5884da5ebaa8797ed42de837f593dc18dcf0e`
passes 624 tests on Python 3.11, 3.12 and 3.13 plus container, upgrade and
Home Assistant checks. See [chunked weather evidence](evidence/chunked-weather-oct02.json).

Indexed retrieval in `1982dd5234e3aad5c41839cae010ceda80cdb1cb` supersedes the earlier timestamp-index gap.
Conservative chunk extents prune compatible payloads while preserving uncertain
and legacy chunks, alternate mapping fallback and exact boundary validation.
The million-row synthetic SDK run selects 1,440 day rows after decoding 2,000
candidate rows in two chunks, including after backup/restore. The actual query
plan uses the range index and retains a metadata scan for uncertain chunks.
Peak traced Python allocation is 2,340,701 bytes. The complete local regression
suite passes 632 tests in 568.44 seconds; Ruff and mypy pass. See
[indexed time-series evidence](evidence/indexed-timeseries-oct02.json).

The [v0.3.0 core release](https://github.com/bferanmi806-sketch/energy-agent-tools/releases/tag/v0.3.0)
is published at `5d4628173f84a531dea7010cb1009d60c33a630c`. All 632 tests pass
on Python 3.11, 3.12 and 3.13. Container, upgrade and Home Assistant CI checks
pass. The isolated base-wheel passes the forecast/bill reference, recovery,
configuration validation and a real stdio MCP child. Exact hashes and limits
are in [release evidence](evidence/release-v030.json). Phase 2 now proceeds
with the TypeScript SDK and Connect Apps experience. Physical
site evidence, broad actual agent evaluation, independent review, outside
connector contribution and sustained operation remain open and continue
alongside the developer product. The full goal remains active.

## TypeScript SDK foundation, October 2

Phase 2 now has a TypeScript package in `packages/typescript`. It generates
request and result schemas from the Python models, validates gateway payloads,
and exposes authenticated REST sessions and an official Streamable HTTP MCP
client. Sessions support discovery, capability resolution/execution, connections,
artifacts, skills, workflow execution and jobs. Toolkit catalogue metadata comes
from the scoped Python registry through a typed REST route, giving Connect Apps
a source without hard-coded entries. Bearer tokens stay in transport
headers. HTTP calls have bounded responses, cancellation and timeouts, and do
not automatically replay requests. MCP calls preserve JSON helper results.

The gateway adds `POST /sessions/{session_id}/skills/run` over the existing
scoped workflow engine. A real REST fixture composes a synthetic 192 kWh,
eight-day forecast and GBP 42.72 bill estimate. Meter history, forecast energy
and calculated forecast cost retain their semantics. Skill search now honors
its requested result limit.

Local verification passes 11 Node tests and 15 Python integration tests, strict
TypeScript checking, generated-contract drift checking, Ruff and mypy. A packed
SDK installs into a separate consumer project, runs the real authenticated REST
acceptance and checks its public declarations. Node acceptance is also invoked
by Python verification when Node 20 or newer and npm are available; a missing
runtime is reported as a skip rather than a passing SDK check.

This is the SDK foundation, not completion of Phase 2 or the full goal. The
Connect Apps application, persistent workspace/connection control plane and
remaining real-world qualification gates are still required. The SDK is not
published to npm; the current source and local tarball are reviewable.

The SDK implementation revision `bd9cbb3547ee24d9d338f458f4a36d4e5e8240ba`
passes [published CI](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37074460742):
637 tests on each of Python 3.11, 3.12 and 3.13, with no skipped Node verifier.
Container, upgrade and Home Assistant checks also pass. The
[SDK evidence](evidence/typescript-sdk-oct02.json) pins the source and package
qualification. These checks qualify software contracts, not physical sites or
the remaining independent evaluation and deployment gates.

## Web and persistent identity foundation, October 3

The first Next.js application starts with Connect Apps, following the confirmed
system-first journey. It reads structured toolkit metadata, connection health,
owned sites/assets and skills from the authenticated Python gateway. It uses an
operator-fixed gateway URL and encrypted, expiring HttpOnly session cookies.
Browser sign-in, site switching and logout have been exercised against the
production build and a local gateway with explicitly synthetic sites.

The gateway adds a typed `GET /me` response. It excludes arbitrary asset metadata
and filters linked account and parent references to the caller's visible scope.
The TypeScript SDK exposes this identity contract in current source. The earlier
0.1.0 SDK tarball and v0.3.0 core release do not contain these new routes.

An owner-private SQLite store now persists users, workspaces, hashed API keys,
sites and assets. Optional host integration checks persisted key revocation on
every REST and MCP request and intersects workspace scope with operator policy.
Backup/restore includes the control database. These records are a foundation:
operator runtime configuration still supplies topology and policy. Workspace
membership, shared connection ACLs and normal user-facing provisioning remain
open.

The original catalogue foundation opened setup requirements. The Octopus
connection increment below now adds encrypted creation, verification, mapping
to an existing owned site and runtime refresh. Custom MCP,
job management and activity views explicitly disclose unavailable management
operations. Generic MCP configuration is shown for the selected site; individual
agent products have not been independently connected through this web interface.

Targeted verification passes 18 public identity/contract checks, 17 combined
control-store, hosted-key and recovery checks, and seven production web HTTP and
security tests. Full mypy passes 52 source files. The fresh full Python run passes 647 tests in 1,066.87 seconds. The production
web build test passes separately in 183.16 seconds, for 648 total checks. The
final reviewed web build also passes strict TypeScript and seven HTTP/security
tests. The earlier interrupted run is not used as qualification evidence. Real physical/private provider evidence,
held-out agent evaluation, independent contribution/review and sustained operation
remain open. This milestone does not complete Phase 2 or the full project goal.

The visual reviewer returned `ship` after scoring all five listed catalogue
fixes resolved. This verdict covers the catalogue foundation and supplied
direction. The initial QUALITY BAR card was absent; full onboarding was not
approved by this review. The independent boundary review found no concrete
exploitable flaw in its assigned control, key, identity, recovery and web paths.

Reproducible scope and log hashes are in [web/control evidence](evidence/web-control-oct03.json).

## Octopus connection flow, October 3

Current source supports Connect Apps → Octopus Energy Account → enter key and
meter details → verify → encrypted storage → Connections. Gateway metadata
defines the form fields. The gateway derives user and site from the session,
uses the fixed provider origin, and publishes only verified accounts. The form
clears the entered key after submission and exposes fixed failure messages.

New accounts refresh capability bindings in the running process. Existing REST
and hosted MCP sessions can retrieve metered kWh immediately. Operator bindings
remain in the resolver. Repeated meter submissions reuse one scoped account;
failed replacements preserve the working credential.

Acceptance passes 57 targeted Python tests, 11 SDK tests, seven production web
HTTP/security tests, strict TypeScript, generated contract drift, Ruff and mypy
across 54 source files. Browser failure and success journeys were exercised at
mobile and desktop widths. These use a fictional Octopus transport, not a real
private meter. The earlier foundation's published CI passes 648 tests on each
of Python 3.11–3.13, plus container, upgrade and Home Assistant checks.

This increment still requires an operator-provisioned owned site and gateway
key. Site/asset creation, workspace membership, sharing, managed OAuth and
connection lifecycle controls remain open, along with the external qualification
gates. Evidence is in [connection onboarding](evidence/connection-onboarding-oct03.json).

## Octopus lifecycle controls, October 3

Connections now offers a fresh read-only provider check and explicit disconnect.
Checks return current health and its time; failed checks preserve the prior
successful verification and credential. Disconnect removes the credential,
retains a revoked record and prevents existing REST/MCP sessions from executing
that account. A verification racing revocation cannot reactivate it. Reconnect
requires a supplied key and a new successful probe.

The browser journey exercises checking, cancelling disconnection, confirmed
disconnection and reconnecting at mobile and desktop widths. Acceptance passes
65 targeted Python tests, 11 SDK tests, seven production web HTTP/security tests,
strict TypeScript, generated contract drift, Ruff and mypy across 55 source
files. An independent read-only lifecycle review found no concrete flaw. The
fictional HTTP fixture permits 300 requests/minute for its scripted burst; the
production default remains 60/minute and existing host tests verify enforcement.

Published onboarding CI at revision `00f1de3` passes 662 tests on each Python
3.11–3.13 plus container, upgrade and Home Assistant checks. Lifecycle full CI
will be recorded separately after the run completes. This still does not
qualify a real private provider or finish the wider product/control-plane goal.
See [lifecycle evidence](evidence/connection-lifecycle-oct03.json).


## Managed workspace integration, October 3

The current implementation adds opt-in managed workspaces to the existing host.
Bootstrap issues a management key for a private workspace. Its owner can browse
the structured catalogue with no sites, verify an Octopus connection, create a
site, map the connection and issue a scoped agent key through the web app or
TypeScript SDK. Dynamic hosted MCP endpoints accept sites created after startup.
Existing operator workspaces retain their policy restrictions.

ControlStore persists immutable site ownership and key grants. AuthStore encrypts
workspace-scoped pending and active connections and uses revision checks for
publication. The host rejects an encryption key that cannot decrypt existing
state. Tests cover same-user workspace isolation, revocation on existing MCP
sessions, operator-key migration, restart, backup/restore and owner-task shutdown
cleanup. Production web acceptance passes nine tests; TypeScript SDK acceptance
passes twelve tests. Browser checks cover desktop and mobile system-first
onboarding and immediate site/key metadata refresh. Provider responses in these
checks are synthetic.

This slice does not complete Phase 2 or Phase 3. Shared membership and connection
ACLs, managed OAuth configuration, additional provider forms, custom MCP
onboarding, triggers and complete managed logs/jobs/settings journeys remain
open. Physical-site evidence, held-out agent evaluation, independent discovery
review, outside connector authoring and sustained operations also remain open.
See [managed workspace setup](managed-workspaces.md) for runnable instructions.


Managed workspace source `c31f5430ea0dab4c41b85f417c3683f112dfb21b` is now
[CI-qualified](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37135243730).
Python 3.11, 3.12 and 3.13 each pass 716 tests. Repository-wide lint, formatting,
types and package builds pass, as do container, published-baseline upgrade and
Home Assistant development protocol checks. The [qualification record](evidence/managed-workspaces-oct03.json)
preserves the earlier in-progress failure separately from final revision results.
These checks do not establish private-provider access, physical meter data,
shared connection ACLs, managed OAuth or sustained operations.

## Managed Home Assistant authorization, October 3

Revision `af84cd2e45b84ede0b3a4cfc8cccb0dcd268c66e` adds native Home Assistant
authorization through deployment-approved instance configurations. The web app
starts authorization, verifies the session-bound return, maps a verified sensor
to an owned site and supports health checks and disconnect. The TypeScript SDK
uses the same production gateway routes. Entity restrictions apply before token
refresh or provider requests.

AuthStore schema 2 keeps failed remote revocations in an encrypted durable
queue. Local disconnect removes execution access before provider I/O. Cleanup
retries require the current approved provider profile and are bounded; backup
and restore preserve pending cleanup. A final primary review reproduced old
profile replay and an unbounded disconnect loop, both fixed with regression
coverage. The final independent cleanup review did not finish because its worker
hit the account usage limit.

A frozen-source full suite passed 777 tests. A subsequent browser check exposed
an authorization form that did not navigate; the final asynchronous submission
change was separately rebuilt, type-checked and accepted by all 11 production
web tests. Browser checks then completed authorization, site creation, mapping,
health and disconnect against disposable synthetic provider fixtures, including
desktop and mobile layouts and rejection of a mismatched unit. These are
synthetic connection proofs, not real provider consent.

Installed Home Assistant authorization remains unverified after a startup
timeout. Exact task-owned Docker resources were removed in follow-up cleanup.
Generic custom OAuth, MCP authorization discovery, shared membership/ACLs,
complete managed logs/jobs/settings, broader evaluation, private-site evidence
and sustained operations remain open. See the
[qualification record](evidence/managed-home-assistant-oct03.json),
[desktop view](evidence/managed-home-assistant-desktop.jpg) and
[mobile view](evidence/managed-home-assistant-mobile.jpg).

## MCP job recovery follow-up, October 4

Fresh MCP and Python sessions now discover numerical jobs from earlier sessions,
page safe metadata and recover results or controls under current authorization.
Toolkit filtering happens before pagination. Recovery preserves the caller's
session identity and checks live workspace access. Corrupt metadata produces a
fixed error without exposing its contents. The [qualification record](evidence/mcp-job-history-oct04.json)
preserves four real FastMCP dispatch checks, 25 gateway integration checks,
17 SDK checks and 11 web checks. The worker owned the acceptance test file;
primary review owned contracts, authorization and integration.

Pending work after restart still needs an explicit authorized execution journey.
Interrupted calculations are not rerun by recovery. Physical-site qualification,
broad actual-agent evaluation, outside contribution, sustained operations and
a coherent platform release remain open.

## Explicit queued-job start follow-up, October 4

Saved pending jobs now have an explicit Start action in the gateway, MCP,
Python and TypeScript SDKs, and web console. Start checks current scope and
calculation permissions, hooks, dependencies and saved arguments before
selecting the exact job. New submissions do not drain older queued work.
Repeated starts do not duplicate work; interrupted and terminal jobs cannot
restart. The existing two-worker bound remains.

The [qualification record](evidence/job-start-oct04.json) preserves 57 focused
Python checks, 18 SDK checks, 11 web checks and production build/typecheck.
The real production browser started one restored job, retrieved its calculated
0.38 kW result and left its neighbor pending. The 390-pixel mobile page had
no horizontal overflow. Three Luna workers owned storage, UI and acceptance;
primary review owned contracts, authorization, integration and browser checks.

These synthetic inputs qualify product behavior and native calculation, not a
physical energy installation. Custom MCP onboarding, complete account/settings
flows, generic OAuth/events, broad actual-agent evaluation, outside contribution,
sustained operations and a coherent platform release remain open.
