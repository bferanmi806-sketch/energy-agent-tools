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
