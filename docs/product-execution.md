# Product execution and completion audit

The [full objective](product-objective.md) is the completion contract. This
record extends the existing backend roadmap; it does not replace unfinished
gates with test counts. Baseline is main `cb3ac05`, with 419 local tests and
[passing CI](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37032700732).

## Current audit

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
| Coherent new release with reproducible evidence | Published v0.2.0; current main is unreleased | Open; gate on stable contracts and substantial backend qualification. |
| First-class TypeScript SDK | No package | Missing; stable REST/MCP transport and cross-language contract tests required. |
| Connect Apps web product | No application | Missing; structured catalogue, connected apps, shared connections, sites/assets, skills, custom MCP, agent setup, health, accounts, logs, jobs and settings required. |
| Hosted connection/control plane | Operator-provisioned identities and hosted sessions | Partial; workspaces, key/user management, ACL sharing, persistent tenant state, dynamic MCP sessions, OAuth configuration, events and migrations required. |
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
