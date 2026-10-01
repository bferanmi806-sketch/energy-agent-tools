# Verification

Verification runs on 1 October 2026, Europe/London. Development uses macOS and
CPython 3.12.14. GitHub CI runs Linux with Python 3.11, 3.12 and 3.13.
Remote CI is a separate release gate from local results.

## Local implementation evidence

The integrated platform suite currently passes 161 tests. Ruff lint and mypy
pass across 27 source files. The release gate also requires the final formatter,
package build, isolated base-wheel installation and remote matrix; final results
are recorded in `evidence/release-v020.json` when verification is complete.

Tests execute real pandapower, pvlib, windpowerlib, PyPSA, pandapipes and SciPy
when the optional extras are installed. The PyPSA two-bus test compares voltage,
angle and line loss against pandapower and checks ohmic/per-unit conversion.
Hydraulic tests cover water, gas and solved external-grid mass balance.
These models are bounded steady-state studies, not operational certification.

MCP tests use real stdio subprocesses, the official client and local HTTP servers.
They cover schema inspection, atomic imports, conservative annotations, review,
bearer credentials and per-call drift rejection. SDK tests exercise all ten
helper schemas in OpenAI Chat, OpenAI Responses and Anthropic dialects and
initialize reviewed imported tools before validating capability bindings.

Capability tests exercise two provider protocols, mapped arguments, fixed
semantic selectors, explicit source choice, account ambiguity, asset scope,
coverage and returned kind/unit/resolution. Workflow tests retain original
source evidence through cost, solar, anomalies, comparisons and baselines.

Security tests cover encrypted secrets, PKCE state/replay, refresh/revocation
races, credential defaults and redaction, malformed/external-reference schemas,
user/site/account/asset isolation, session-scoped artifacts, quota/retention,
expiry/deletion, rate and body bounds, shared MCP admission and token rotation.
Executable tests cover fixed commands, restricted environment, path boundaries,
timeout and output bounds. Operator plugins and executables remain trusted code.

## Discovery

[`evidence/discovery.json`](evidence/discovery.json) records eight curated energy
queries and a generated 10,000-action catalogue. All eight expected actions
appear in the top five. Timing covers 100 queries after index construction;
the raw median and p95 are reported in that file. This measures the indexed
lexical baseline, not general relevance or learned ranking.

## Real-model evaluation

The benchmark invokes actual Codex model processes against a fresh local MCP
gateway. Sources are synthetic and results retain that qualification. The first
19-case exploratory run returned 13 pass and six partial labels. Its numeric
failures, clock mismatch, safe refusals and scoring issues were inspected from
actual calls and answers. See [the audit](agent-benchmark.md).

The release run freezes the scenario dates, records source SHA256 and Git
identity, saves incremental traces, and uses scoped approval for the local
fixture MCP server. Native shell tools are disabled. It does not use a global
sandbox/approval bypass. Heuristic scores and one fixture do not establish
multi-provider or production agent reliability.

## Live public providers

[`evidence/public-live-v020.json`](evidence/public-live-v020.json) records five
successful nonempty actions through the execution runtime on 1 October:

| Action | Returned data |
|---|---|
| GB national carbon | 1 calculated interval |
| GB regional carbon | 48 forecast intervals |
| Open-Meteo | 96 variable rows |
| Octopus Agile tariff | 27 calculated rate intervals |
| Elexon FUELHH | 960 metered grid rows |

[`evidence/neso-live.json`](evidence/neso-live.json) records live CKAN search and
metadata. [`evidence/neso-datastore-v020.json`](evidence/neso-datastore-v020.json)
records three real datastore forecast rows. Their unit stays provider-defined;
a generic dataset query does not establish a compatible meter contract.
Provider counts and publication windows change with time.

Octopus private consumption, Home Assistant, Emoncms, ENTSO-E and Electricity
Maps are contract-fixture qualified. No legitimate private-provider credentials
were available. EnergyPlus has executable boundary fixtures, with no installed
engine/model run. OpenDSS, OpenStudio and vendor PowerMCP remain unqualified.

## Reproduce

```sh
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv build
uv run python examples/bound_sdk.py
uv run python examples/reference_agent.py
uv run python scripts/discovery_benchmark.py
uv run python examples/live_probe.py --output public-probe.json
uv run python -m benchmarks.harness --repo "$PWD" --output benchmark-results
```

Live probes need network access. Real-model evaluation needs an installed Codex
CLI and legitimate login. Neither runs as part of ordinary offline unit CI.
