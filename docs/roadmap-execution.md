# Roadmap execution

Execution contract: [project roadmap](project-roadmap.md). Baseline is v0.2.0,
commit `5354d32`. Completed implementation is distinct from live qualification.

## Current work

- [x] Read the orchestration principles and preserve the existing release evidence.
- [x] Frame the run against the six milestone gates.
- [x] Separate independent implementation into exclusive worktrees.
- [x] Reproduce artifact-window and requested-coverage defects before changing code.
- [x] Integrate and verify provider substitution fixtures.
- [x] Implement local connection onboarding and reviewed mappings; private access qualification remains open.
- [ ] Expand and execute real-agent evaluation without weakening scoring.
- [x] Qualify OpenDSS and EnergyPlus with real engines and reference inputs.
- [x] Integrate bounded jobs, deployment and recovery checks; upgrade and soak gates remain open.
- [x] Publish verified improvements and update the evidence matrix; later batches retain separate checks.
- [ ] Review the complete supported experience against the roadmap gates.

The primary owns integration contracts, shared runtime, CLI and final review.
Workers own new fixture, onboarding, evaluation, engine, deployment and job files
in isolated worktrees. Their commits require primary review and acceptance tests.

## Original acceptance criteria

| Criterion | Existing executable evidence | Remaining qualification |
|---|---|---|
| One heterogeneous gateway | `tests/test_workflows.py`, `tests/test_sdk_workflows.py` | Complete reference journeys across additional telemetry and engines |
| Dynamic discovery | `tests/test_mcp_endpoint.py`, `scripts/discovery_benchmark.py` | Broader held-out intent set and relevance results |
| Generic multi-provider capabilities | `tests/test_capabilities.py`, `tests/test_provider_substitution.py` | Reviewed telemetry transformations and real provider substitution |
| Accounts/sites/assets | `tests/test_platform_security.py`, `tests/test_provider_substitution.py` | Broader multi-site real-agent scenarios |
| Credentials outside context | `tests/test_auth.py`, `tests/test_review_regressions.py` | No-config onboarding and provider qualification |
| Common REST/Python/local/MCP execution | `tests/test_hosting.py`, `tests/test_executable.py`, `tests/test_mcp_bridge.py`, `tests/test_sdk_workflows.py` | Full journey examples with authentication and imported semantics |
| Bounded large results | `tests/test_workbench.py` | Chunked processing and representative large-site workloads |
| Physical semantics and provenance | `tests/test_timeseries.py`, `tests/test_workflow_windows.py` | Complete source-to-answer coverage and counter mapping |
| Broad real-agent success | `docs/evidence/agent-benchmark-v020.json` | 100+ distinct cases, held-out tests and two available model families |
| Real private/site workflow when accessible | Provider fixtures in `tests/test_http_connectors.py` | Access acquisition and actual installed-provider workflow |
| Connector SDK without runtime edits | `src/energy_agent_tools/connector_sdk.py`, `tests/test_extended.py` | Independently contributed integration |
| Practical self-hosting | `docs/self-hosting.md`, `tests/test_hosting.py` | Container, install, restart, upgrade and restore checks |
| Green engineering/release checks | Python 3.11/3.12/3.13 CI at v0.2.0 | Repeat for integrated changes and release artifact installation |
| Honest qualification catalogue | `docs/connectors.md`, `docs/limitations.md` | New engine/provider qualification records |
| Extensible platform experience | Registry, capability resolver, plugins, MCP and SDK | Reference projects and sustained operational evidence |

A test path is a reproducible check, not proof that a new run passed. Results are
recorded separately in release evidence and the append-only decision log.

## External access investigation

On 1 October the browser inventory contained no provider account sessions.
The Emoncms hosted registration form was inspected through its home page. It
requires username, password and email, and asks users to read its usage/pricing
information. No identity, password or billing information was invented or
submitted. Local provider deployments remain an independent qualification path.

Octopus customer API keys come from the customer's Developer settings, and meter
access requires the associated account. Home Assistant authentication requires
an authorized instance or a project-controlled installation. ENTSO-E requires
registration and an API-access request to its helpdesk. Electricity Maps offers
personal/free access and trial routes whose eligibility and registration must be
checked before requesting credentials. No private-provider access is claimed yet.

Sources: [Octopus access](https://octopus.energy/help-and-faqs/articles/how-do-i-access-the-octopus-api/),
[Home Assistant authentication](https://developers.home-assistant.io/docs/auth_api/),
[Emoncms API](https://www.emoncms.org/site/api),
[ENTSO-E token management](https://transparency.entsoe.eu/content/static_content/download?path=%2FStatic+content%2FAPI-Token-Management.pdf),
[Electricity Maps access](https://help.electricitymaps.com/en/articles/13335550-how-can-i-access-the-electricity-maps-api-and-are-there-any-restrictions).

## Integrated run evidence

- 243 tests passed on Python 3.12; Ruff, formatting and mypy passed.
- Three provider paths use the same consumption/cost workflow with independent
  numerical truths. These are protocol fixtures, not live private meters.
- Requested windows preserve source kind and report edge/interior gaps and DST.
- The connection journey loads a generated local key/profile without environment
  edits. Current power requires observation freshness; counters cannot be silently
  promoted to interval energy.
- Numerical jobs enforce the gateway policy and survive host restart. A complete
  profile/vault/job backup was restored and used after deleting the original state.
- A real Codex access probe completed with a partial score (0.9167); it is preserved
  separately from a release benchmark. The 141-case corpus (58 held-out) and
  221 discovery intents are contracts, not 141 executed cases. An interrupted
  environment draft was not imported because its fixtures did not match several
  independent scenario truths.
- The Home Assistant official image was downloaded and started. Its API did not
  reach readiness during checks. The shared Colima VM had 2908/2970 MB in use and
  severe load; the project-created Home Assistant container was stopped successfully.
  No private credentials or live physical readings were obtained. Other services
  in that VM were left running. Container build/host qualification moves to CI.
- Only Codex was present among the checked model CLIs. The checked Anthropic,
  Gemini and OpenRouter credential environment references were absent. Claude
  API access requires an owner-created Console account/key and available credits;
  no owner identity or billing acceptance was fabricated. Electricity Maps'
  documented portal was attempted in the browser, whose tab attachment timed out.

Access references: [Claude API start](https://platform.claude.com/docs/en/get-started),
[Electricity Maps key management](https://help.electricitymaps.com/en/articles/13160917-where-do-i-find-my-home-assistant-api-key).

The remaining gates include qualified scenario environments and 100+ actual runs,
held-out results, a second legitimately accessible model family, complete telemetry
transformations and workflows, private installed-provider qualification, container
restart/load/upgrade checks, contributor qualification, and sustained soak evidence.

## 2 October checks

At `c2ea0aa`, 255 tests passed locally and CI passed on Python 3.11, 3.12 and
3.13. Four new development environments ran through actual Codex calls in an
immutable checkout. The source identity stayed unchanged throughout the run.
Two cases passed and two were partial, with a mean heuristic score of 0.9105.
The daily CSV total was 42.5 kWh. The fresh Home Assistant fixture returned
12.75 kW. The interval-gap answer correctly disclosed 42/48 intervals, 87.5%
coverage and unknown missing energy, but used 15 calls against a budget of 10.
The two-account answer asked for clarification; it missed required language,
structured boundary-error and provenance evidence. Scoring was not weakened.

[Full scores and transcript digest](evidence/agent-benchmark-qualified-oct02-summary.json)
and [compressed complete traces](evidence/agent-benchmark-qualified-oct02.json.gz)
preserve the results. These runs add development evidence, not held-out or
physical-meter evidence. The separate 19-case run in the same immutable checkout
finished with the quota interruption recorded below. The earlier 11-pass/8-partial run remains exploratory because
source changes continued while it was running.

Container host qualification passed in CI at `9c20850`: bearer enforcement,
non-root UID 10001, 30 concurrent requests, and job/artifact recovery after a
restart. The measured median was 0.06784 seconds and p95 was 0.07705 seconds.
The test rediscovers the host port after restart because Docker can reassign it.
This is a short load check, not sustained soak or upgrade evidence.

Further fixes reject duplicate/overlapping energy intervals and apply current
toolkit/site scope to saved jobs. Reviewed account/asset capability roles are
now included in scoped search without mutating the shared catalogue. Their
focused acceptance checks passed; final combined verification remains required.

At `ae79406`, CI passed 266 tests on each Python version and the real Home
Assistant container qualification. This validates development authentication,
encrypted connection storage, profile reopen, scoped current-power execution
and freshness against an installed provider. The synthetic state remains
estimated; the physical-meter gate is still open.

The immutable 19-case run completed with 6 passes, 5 partials and 8 inconclusive
results. The model CLI reported a usage limit during tariff comparison and
subsequent cases could not complete. The report and
[complete traces](evidence/agent-benchmark-frozen-oct02.json.gz) are preserved in
[the summary](evidence/agent-benchmark-frozen-oct02-summary.json). Its mean
heuristic score of 0.7887 includes inconclusive cases and is not a success claim.
A new runner guard stops on that infrastructure error and records unattempted
case IDs. No quota resets, credit purchases or model substitutions were used.


CI at `2e3eccd` passed 283 tests on Python 3.11, 3.12 and 3.13, container
qualification, actual Home Assistant stale-read and foreign-user denial checks,
and the installed published-v0.2.0 upgrade/backup/restore check. Evidence is in
[the provider check](evidence/home-assistant-negative-checks-oct02.json) and
[the upgrade check](evidence/state-upgrade-ci-oct02.json).

Nine additional independently checked development environments now cover five
telemetry and four engineering cases. Together with the first four, the harness
can select 13 qualified scenario environments. These environment checks are
not additional model passes. The 19 original agent cases remain the default;
select the new scenarios explicitly with `--case`. Three candidate cases stay
excluded because reviewed CSV counter semantics, capacity-constrained PyPSA
dispatch, and a bounded PyPSA job operation are missing. The frozen scenario
truths and all held-out cases remain unchanged.

Counter conversion now emits explicit interval starts and ends, preserving the
last interval when selecting a day. Tests reproduce the previous boundary
loss with 49 counter observations and independently require 48 intervals and
50 kWh. Quantity shape survives filtering, normalization and missing-row
expansion, and raw counters cannot enter cost/carbon multiplication.

## Orchestrated implementation on 2 October

Workers use GPT-6 Luna with maximum reasoning effort. The primary retains
shared contracts, integration, acceptance checks and release review.

The current batch adds caller-declared CSV interval/counter/power semantics,
explicit consumption transformations before date filtering, three runnable
reference projects with scoped source artifacts, real PyPSA linear dispatch,
and bounded PyPSA jobs with atomic migration of the prior job store.
The integrated pre-billing suite passed 322 tests. Billing adds explicit
standing charges and caller-defined tax treatment, preserving complete
interval boundaries and refusing incomplete coverage; its combined focused
checks passed 78 tests. These test counts are implementation checks, not
real-agent task passes.

The [221-intent discovery measurement](discovery-evaluation.md) preserves
ambiguous labels and capability gaps. The [actual 13-case agent diagnosis](agent-run-diagnosis-oct02.md)
records 5 passes and 8 partial results from unchanged source `ad769dc`.
No scoring thresholds or frozen scenario truths were changed. The diagnosed
cold numerical import delay was reproduced and fixed after that run; the
archived scores remain unchanged.

The project goal remains the open-source, self-hostable energy-agent gateway.
Unfinished gates include complete examples and failure evidence for all twelve
workflows across provider combinations; real owner-authorized site data;
100 or more actual tasks and held-out qualification with two available model
families; independent relevance review of at least 200 intents; a representative
30-day soak; and an independently authored connector. The three reference
projects use synthetic inputs and real numerical solvers. They do not clear
the real-site gate. v0.2.0 remains the published release.
