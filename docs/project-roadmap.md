# Energy Agent Tools: plan to fulfil the project goal

Prepared 1 October 2026 against v0.2.0, commit `5354d32`.

## The outcome

Build a strong open-source, self-hostable integration platform through which an AI agent can discover, authenticate with, and use heterogeneous energy systems. A developer should be able to connect a site's providers, describe its assets, bind an agent session, and ask useful energy questions without writing provider-specific orchestration.

Success includes meters, monitoring systems, public energy APIs, local data, engineering software and reviewed MCP servers. Results must retain physical meaning, account and asset scope, source evidence, and uncertainty throughout a workflow.

Enterprise procurement, billing, sales features and certification programmes are outside this roadmap. Reliable multi-user hosting, credential protection and bounded execution remain necessary because they are part of the original project goal.

This is a proposed implementation plan, not a claim that the milestones have been delivered. Version names are provisional. Publish a release only when its gate passes.

## Where the project stands

The v0.2.0 release establishes a common runtime, reviewed capability bindings, encrypted credentials, scoped REST/MCP hosting, Python integration, twelve workflows and a connector extension mechanism. Its default catalogue contains 26 actions across 16 toolkits. CI passes 161 tests on Python 3.11, 3.12 and 3.13.

The real-agent release evaluation contains 19 synthetic cases: 15 passed and four received partial scores. That is useful early evidence, but it does not establish broad real-world reliability. Private providers have fixture qualification, and EnergyPlus has executable-boundary tests rather than a qualified real-engine workflow.

The most important gaps are complete capability coverage, useful workflows across genuinely different providers, straightforward connection onboarding, stronger engineering qualification, broader agent evaluation and sustained operation on real installations.

## Product contract

The main experience is:

1. Install locally or start a documented self-hosted deployment.
2. Connect providers and verify access.
3. Associate channels with a site and its assets using explicit units and measurement types.
4. Create a scoped agent session through Python or MCP.
5. Discover a small set of relevant tools and execute provider-independent workflows.
6. Inspect the answer's inputs, calculations, missing coverage and provenance.

Maintain three complete reference projects: a home with a meter, PV and battery; a building with consumption and equipment/weather data; and an engineering study using a network model. Each must be runnable from documented sample data. Live variants must state their access requirements and qualification status.

## Milestone 1: make the intended behaviour provable

First establish a coverage matrix for every original acceptance criterion. Link each claim to runnable evidence, distinguish implementation from live qualification, and keep known gaps visible.

Create provider-independent contract scenarios for consumption, current power, generation, export, storage state, tariffs, carbon, weather and engineering studies. Specify required arguments, units, interval meaning, timezone, source kinds, missingness and refusal behaviour. Record whether each capability is implemented, qualified or pending. Do not invent a universal schema that loses provider meaning.

Expand deterministic fixtures beyond the single release site. Include multiple accounts for the same provider, multiple providers for one capability, multiple sites, DST changes, counters, gaps, stale telemetry and mixed measurement kinds. Use independent expected values and preserve earlier benchmark evidence.

Gate: the same consumption question works through three provider adapters on distinct fixtures. Ambiguous accounts require selection; missing sources produce a useful refusal; derived artifacts preserve their source identity. Every original acceptance criterion has an evidence link or an explicit unfinished item.

## Milestone 2: make connecting and resolving sources easy

Build a coherent CLI/API connection journey: configure, authenticate, verify, inspect health, disable, revoke and reconnect. Implement provider-specific verification where it is missing. Expose actionable diagnostics without returning credentials. Finish callback and MCP authentication paths for the providers chosen for qualification.

Add a reviewed telemetry mapping flow. A Home Assistant entity, Emoncms feed or CSV column needs an explicit interpretation before it can supply generic energy capabilities. Support counters versus interval energy, power versus energy, export direction, battery state of charge and sample freshness. Store reviewed mappings with versions so changes can be inspected.

Resolve sources using observed coverage and freshness where providers expose them, alongside declared metadata. Explain which candidate was selected and why alternatives were rejected. Keep ties explicit and never replace absent measured consumption with a forecast or simulation.

Gate: someone following the documentation can connect a supported provider, map a meter, ask about consumption and inspect provenance without editing runtime code. Repeat with a second account and a second site. Demonstrate one real private/site workflow using legitimately available access. If access is unavailable, report that gate as pending; fixture success cannot clear it.

## Milestone 3: complete the core energy workflows

Strengthen the existing workflows before adding more recipes. Automatically validate and explicitly filter reused artifacts to the requested window. Make coverage, freshness and incompatible intervals visible. Add pagination or chunked artifact processing where bounded whole-file handling prevents useful workloads.

Consumption and cost workflows should reconcile meter totals and tariff components. Standing charges, taxes and tariff schedules must be supplied and attributed rather than assumed. Solar comparisons should reconcile compatible generation, import and export data. Battery and EV plans should include explicit capacity, efficiency, power limits, availability and required final charge.

Upgrade spike analysis to compare consumption against available weather and equipment evidence. Present supported possible explanations and missing evidence. An observational correlation does not establish a cause. Turn the grid-condition and power-flow recipes into meaningful analyses with explicit limits, numerical checks and actionable descriptions of model violations.

Gate: each of the twelve workflows has a complete reference example, failure cases and evidence requirements. Applicable workflows run unchanged across two provider combinations. At least one multi-step workflow joins real site data with a public source and returns traceable calculations. Missing intervals and incompatible quantities cannot silently produce a confident answer.

## Milestone 4: expand the ecosystem by completing useful journeys

Select connector work by the workflows it enables, accessibility, maintainability and test evidence. There is no connector-count ceiling.

First complete metering, PV/battery telemetry and charger coverage. Qualify Octopus, Home Assistant and Emoncms where access permits; choose inverter and EV integrations after checking official API access and representative hardware. Prefer an accessible, well-supported integration over a proprietary API with no test environment.

Then qualify building and network workflows. Run EnergyPlus against published reference inputs and a real installed engine. Add DSS-Extensions/OpenDSS with reference networks and independently checked results. Add OpenStudio or vendor MCP integrations only when their actual interfaces and execution environments can be tested. Preserve the existing numerical tools and improve their reference evidence.

Make reviewed MCP import a contributor-friendly alternative to writing native connectors. Provide a repeatable inspect, review, namespace, bind and validate journey. Cover remote authentication, pagination, reconnects and schema drift according to the transports supported by the project.

Gate: each new connector has declared auth, version, action permissions, capability mappings, result contracts, contributor tests and a qualification record. Each stable designation requires documented evidence and a maintenance owner. Imported MCP tools obey the same scope, action and semantics rules as native tools. Qualification and credential requirements are separate catalogue fields.

## Milestone 5: prove that real agents can use the platform

Grow the evaluation to at least 100 task cases spanning the reference projects, provider substitutions, ambiguous sources, outages, stale data and misleading requests. Separate a development set from a held-out set. Use deterministic numerical checks where possible, with human-reviewed rubrics for explanations and refusals.

Run at least two available model families through the same MCP or SDK gateway without revealing the intended tool sequence. Use legitimate access and record unavailable evaluations explicitly. Capture source revision, fixture version, calls, errors, elapsed time and model identity. Measure task completion, numeric correctness, source selection, safe refusal, provenance, call count and latency separately.

Replace the eight-query discovery demonstration with at least 200 reviewed intents, including synonyms, negative queries and account availability. Publish top-k relevance and latency across realistic catalogues. Add optional semantic ranking only if it improves the held-out results enough to justify its dependency and operating cost. Basic discovery must remain usable offline without an LLM.

Proposed release targets: at least 90% complete-task success on the documented supported held-out tasks for each evaluated model family; zero observed cross-scope disclosures or prohibited-action executions; at least 95% correct source selection and provenance on applicable cases. Report sample counts and uncertainty. A clean finite test set does not prove that security failures are impossible.

Gate: publish reproducible results, representative traces and all failures. Demonstrate provider substitution through the same generic workflow. Do not merge partial scores into task success or soften scoring to pass a release.

## Milestone 6: make it maintainable and dependable to self-host

Provide a tested container deployment, persistent-state configuration, backup/restore, upgrades, diagnostics and an offline sample-data mode. Keep local Python installation first-class. Add a small local connection/site inspector only if it materially simplifies onboarding; a dashboard is not a prerequisite for completing the runtime.

Move long simulations into bounded workers with status, cancellation, timeouts and recoverable results. Treat process isolation and sandboxing as different requirements. Define resource and network restrictions for executable jobs before accepting untrusted inputs. Keep the core runtime small and retain one policy-enforcing execution path.

Add provider-specific retry rules, rate limits, circuit breaking and freshness reporting. Retry safe reads within budgets. Require explicit idempotency support for retriable writes. Instrument discovery, resolution, provider calls and artifact operations with credential-safe traces, metrics and logs.

Exercise restart, provider outage, token expiry, quota exhaustion, concurrent users, upgrade and restore scenarios. Define load targets from a published deployment profile rather than claiming arbitrary scale. Run a 30-day soak on representative installations, track incidents and fix recurring failures.

Finish contributor fixtures, a reference connector, compatibility policy, deprecation policy, release checks and maintainer guidance. Ask an outside contributor to add a connector using only the documented extension interface.

Gate: a fresh installation completes the reference projects; restoration recovers usable connections and artifacts; failed jobs leave bounded recoverable state; outages produce diagnosable errors; the soak report and load profile are published; an independently authored connector passes validation without core changes.

## Sequence and execution

Milestone 1 comes first. Milestones 2 and 3 form the main implementation sequence. Connector work proceeds when contracts are settled and each integration has an independent ownership boundary. Evaluation grows throughout the work. Operational checks start early, even though the sustained soak is a final gate.

Release milestones should correspond to completed user journeys: trustworthy source connection and resolution; complete cross-provider workflows; qualified ecosystem coverage; broadly evaluated self-hosted platform. Do not assign 1.0 until the documented supported scope clears its gates. The project remains extensible after that point; fulfilling the goal does not mean implementing every energy provider in existence.

A provisional planning envelope is four to six months for one maintainer with sustained engineering time, agent assistance and access to representative installations. This is an estimate, not a commitment. Re-estimate after the contract audit and first live workflow. External credentials, hardware access, provider approval and independent review can determine the schedule.

The first implementation batch should:

1. Build the acceptance/evidence matrix and capability coverage map.
2. Add independent multi-provider, multi-account and multi-site fixtures.
3. Complete one documented connection-to-answer journey.
4. Fix workflow artifact-window handling and coverage reporting.
5. Demonstrate yesterday's consumption and cost through two provider paths.
6. Re-run the real-agent benchmark on the new scenarios and publish remaining gaps.

## Completion standard

A developer can install the project, connect supported systems, map sites and assets, bind an agent and complete the documented energy tasks through one gateway. Provider changes do not require rewriting those tasks. Contributor integrations use the same contracts. Results retain physical meaning and provenance. Private and numerical integrations have honest qualification records. Self-hosting, failure recovery, evaluation and releases are reproducible.

All fifteen original acceptance criteria must have executable evidence. Any conditional live-access criterion must clearly state whether its condition was met. Broader future integrations remain roadmap items rather than unfinished claims inside the supported release scope.

## References

- [Current release report](https://github.com/bferanmi806-sketch/energy-agent-tools/blob/v0.2.0/docs/release-report-v020.md) supplies the implementation baseline.
- [Composio sessions](https://docs.composio.dev/reference/api-reference/tool-router) describes user-scoped tool, account and execution context. Its [authentication documentation](https://docs.composio.dev/docs/authentication) supplies connection-management comparison points. These are references, not hosted dependencies.
- [OpenTelemetry signals](https://opentelemetry.io/docs/concepts/signals/) supplies the observability vocabulary for traces, metrics and logs.
- [NIST SSDF](https://csrc.nist.gov/Projects/ssdf) is a reference for secure development and release practices. This roadmap does not claim certification or standards compliance.
