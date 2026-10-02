# Product execution and completion audit

The [full objective](product-objective.md) is the completion contract. This
record extends the existing backend roadmap; it does not replace unfinished
gates with test counts. Baseline is main `cb3ac05`, with 419 local tests and
[passing CI](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/37032700732).

## Current audit

| Requirement | Current evidence | Status and next proof |
| --- | --- | --- |
| One gateway, Python SDK, scoped MCP, sites/assets/accounts | Runtime, capability resolver, SDK/MCP acceptance and twelve references | Implemented; broader live qualification remains open. |
| Consumption forecasting and forecast bill composition | No consumption forecast tool or composed recipe exists | Missing; implement scoped historical/context inputs, honest forecast uncertainty and tariff/billing lineage. |
| Supported spike explanations | Anomaly screening only | Missing; use available weather/equipment evidence and explicit missing evidence without claiming causation. |
| Combined grid analysis | Generation and carbon are fetched separately | Missing; align quantities, quantify useful conditions and explain limits without conflating grid/local readings. |
| Large-site processing | Bounded whole-artifact operations | Partial; representative partitioned processing, pagination, memory/load measurements and recovery needed. |
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
optionally use explicitly supplied past/future contextual data, predict a
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

Temporal selection still scans chunks. Timestamp indexing, concurrent-load and
sustained-operation qualification remain open. The fixture is synthetic and does not establish physical meter access.
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
