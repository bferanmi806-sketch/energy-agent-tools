# Energy Agent Tools v0.2.0 report

Repository: [bferanmi806-sketch/energy-agent-tools](https://github.com/bferanmi806-sketch/energy-agent-tools).
Release: [v0.2.0](https://github.com/bferanmi806-sketch/energy-agent-tools/releases/tag/v0.2.0).

This milestone turns the initial gateway into a self-hosted integration platform.
The operator still owns provider permissions, credentials, reviewed bindings and
model qualification. Private-provider access and production reliability are not
inferred from fixture tests.

## Architecture and Composio patterns

The registry stores versioned toolkits, schemas, permissions and handlers. A
common execution runtime validates arguments, enforces user/site/account/asset
scope, obtains credentials at the connector boundary, checks returned contracts,
and persists bounded results. HTTP, Python, local commands and imported MCP use
that path. The default catalogue contains 26 actions in 16 toolkits; configuring
CSV or additional operator adapters expands it.

The adopted Composio patterns are bound sessions, toolkit/account association,
search before schema loading, provider tool formatting, connection lifecycle,
and one execution gateway. The implementation uses local reviewed capability
bindings, an encrypted vault and SQLite artifacts. It does not depend on Composio's
cloud auth, managed execution or hosted connection storage. No Composio source is
copied or bundled. The [audit](composio-audit.md) records upstream evidence and MIT
licensing boundaries.

## Resolution, sites and physical semantics

Reviewed bindings carry exact provider argument maps, fixed semantic selectors,
account/asset links, kind, unit, resolution, declared coverage, quality and source
preference. The resolver checks availability, credentials, policies and exact
schemas. Equal candidates remain ambiguous. Execution rechecks fixed arguments
after hooks and kind/unit/resolution before persistence. Hooks cannot erase
original provenance or relabel bound site/asset scope.

Sites belong to users and have IANA timezones. Asset kinds are extensible; same-site
parent hierarchies reject cycles and asset accounts must agree with the site.
Sessions pin accounts and optionally restrict toolkits and actions. Grid MW
series use `get_grid_generation`; site PV kWh remains a separate reviewed source.
See [sites and assets](sites-and-assets.md) and [architecture](architecture.md).

Results distinguish metered, calculated, estimated, simulated and forecast data.
They carry original units, field units, provider/site/asset metadata, time coverage
where known, assumptions, warnings and nested input lineage. Missing observations
stay missing. Counter differences and power integration are explicit operations.

## Authentication and deployment

Connections support environment references and encrypted local storage. OAuth
includes PKCE, one-time state, a loopback callback, refresh, disable, revoke and
reconnect. Refresh and verification races cannot restore a revoked connection.
Local verification checks credentials; SDK provider probes establish actual
verification. The operator supplies the vault key and OAuth provider registration.
No secret value is exposed in agent schemas or connection lists. Bounded execution
events record IDs, latency, provider/account and safe error codes; an operator
can attach an event sink without logging tool arguments or credentials.

Authenticated hosting provisions bearer-token digests, expiry/revocation and live
rotation. It derives identity before creating scoped REST or MCP sessions. Limits
cover rate, body size, session count, idle lifetime and shared MCP admission.
Artifacts have scoped deletion, expiry and aggregate quotas. TLS and organizational
identity remain deployment infrastructure. See [authentication](authentication.md)
and [self-hosting](self-hosting.md).

## Connectors and evidence

| Category | Implemented coverage | Qualification |
|---|---|---|
| Public energy APIs | GB carbon, Open-Meteo, Octopus rates, Elexon, NESO CKAN/datastore | Live public probes plus contract fixtures |
| Private/entitled services | Octopus meters, Home Assistant, Emoncms, ENTSO-E, Electricity Maps v4 | Auth/parser/semantics fixtures; no legitimate private credentials available |
| Numerical models | pvlib, windpowerlib, pandapower, PyPSA, pandapipes, SciPy battery MILP, heat loss | Real installed solvers and bounded numerical tests; experimental |
| Operator data | CSV, read-only SQLite, reviewed MCP and fixed commands | Path/schema/permission/transport/subprocess tests |
| Building engine | Optional trusted EnergyPlus executable and IDF/EPW adapter | Boundary fixtures only; no real engine/model qualification |
| Deferred engines | OpenDSS, OpenStudio, vendor PowerMCP | Unqualified; no placeholder success claims |

PyPSA tests compare voltage, angle and loss with pandapower and explicitly check
line ohms/per-unit conversion. pandapipes tests solve water and gas hydraulics and
check external-grid mass balance. These are steady-state studies, not protection,
transient, thermal-transport or device-control validation. The
[catalogue](connectors.md) and [upstream research](upstream-research.md) give exact
contracts and dependency requirements.

## MCP, SDK and extensibility

Ten MCP helpers expose bounded discovery, scoped connections, batch execution,
site context, reviewed resolution and executable skills. Imported MCP schemas can
be inspected and fingerprinted before approval. Annotations do not grant action
permissions. Per-call schema drift blocks execution, and configured multi-server
startup commits atomically after all imports and bindings validate.

The bound SDK exports OpenAI Chat, OpenAI Responses and Anthropic function
schemas and dispatches calls through the same helpers. It does not create or
operate a provider client. Configured MCP imports initialize asynchronously.
Plugins are operator-selected; scaffolding and structural validation let a
contributor add a connector without changing the runtime. See [SDK](sdk.md),
[MCP review](mcp-review.md) and [connector development](connector-development.md).

## Workflows and workbench

Twelve recipes execute consumption summaries, anomaly screening, electricity
cost, cheapest/cleanest battery planning, solar comparison and forecast, grid
conditions, power flow, building comparison, tariff comparison and baselines.
They refuse unresolved sources and missing model inputs. Direct solar forecast
energy retains its forecast kind; weather-based estimates retain weather lineage.
Grid and power-flow recipes return source evidence rather than claiming additional
analysis. Anomaly screening does not identify a cause.

The workbench supplies filtering, exact UTC alignment, missing coverage, counter
reset handling, explicit power integration, cost/carbon calculations, rolling
baselines, local calendar comparison, unit conversion, summaries, resampling,
pivots and joins. Existing artifacts are used as supplied; filter them to a
requested range before workflow reuse. See [workflows](workflows.md).

## Verification and evaluation

Local verification passes 161 tests, Ruff, formatting, mypy over 27 source files,
and wheel/source build. [Linux CI](https://github.com/bferanmi806-sketch/energy-agent-tools/actions/runs/36868786447) passes the same 161 tests on Python 3.11, 3.12 and 3.13. An isolated base-wheel installation initializes a real
stdio MCP client, exposes ten helpers and reports unavailable optional solvers.
Public probes returned nonempty carbon, weather, tariff and Elexon results, plus
NESO datastore forecast rows. Private installations were unavailable for live
verification. See [verification](verification.md) and its raw evidence.

The real-model benchmark invokes actual Codex processes using the legitimate
existing login, one fresh MCP server per case and synthetic local data. Shell
tools are disabled and approval is scoped to that fixture server. Numeric truth,
source kinds, provenance, gateway boundary and call budgets are scored. Source
identity and incremental traces make the run auditable. The first exploratory
run had a date mismatch and conservative scoring errors; its evidence is preserved
rather than silently replaced. The frozen release run completed 19 cases with 15 pass and four partial labels,
zero fail and zero inconclusive. Mean heuristic score was 0.9790. Partial scores
reflect an argument-pattern mismatch, call-budget overruns and a safe refusal
without the expected structured error. See [the benchmark report](agent-benchmark.md)
for actual answers, limits and compressed traces. The 10,000-action discovery
measurement returned all eight curated expected actions in the top five, with
68.7 ms median and 127.4 ms p95 across 100 timed queries.

## Remaining gaps and highest-value work

The platform has fewer connectors and much less operational evidence than a
mature Composio-scale service. It lacks managed identity/onboarding, a central KMS,
dynamic tenant provisioning, durable job scheduling, broad production load tests,
multiple-model qualification and large-scale discovery relevance evaluation.
Numerical work runs in the trusted host process. Provider entitlements, exchange
models and physical-control authority require operator review.

The highest-value next work is live private-account qualification, broader model
and site fixtures, explicit telemetry/counter mappings, published numerical
reference networks, isolated asynchronous simulation jobs and deployment load
measurements. Expand validated provider coverage before pursuing connector counts.
The [limitations](limitations.md) remain part of the release contract.
