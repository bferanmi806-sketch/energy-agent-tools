# Verification

Release verification on 1 October 2026, Europe/London. Development ran on macOS
with CPython 3.12.14. CI additionally runs Linux on Python 3.11, 3.12 and 3.13.
Remote CI status is separate from the local results below.

## Local checks

| Command/check | Result |
|---|---|
| `uv run pytest -q` | 60 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | All files formatted |
| `uv run mypy` | Success, no issues in 17 source files |
| `uv build` | Wheel and source distribution built |
| `uv run python examples/sdk.py` | CSV import, provider aliases and 1.2 kWh summary succeeded |
| `uv run python examples/reference_agent.py` | Real stdio MCP discovery/import/summary succeeded |
| Isolated base-wheel install | Seven MCP helpers available; missing pvlib returns `dependency_unavailable` |
| `git diff --check` | No whitespace errors |

These checks ran against the actual implementation and package, not a mock of the
runtime. Local tests use real pandapower, pvlib and SciPy. Transport tests use the
official MCP client, real subprocesses and a local HTTP MCP server. Public network
requests do not run in the offline test suite.

## Six workflows through one endpoint

`tests/test_workflows.py` keeps one official MCP ClientSession connected to the
gateway and drives a deterministic reference agent. Private/provider data uses
HTTP contract fixtures. Every workflow first checks discovery and bounded schema
selection, then executes through the common MCP runtime.

| Workflow | Assertions |
|---|---|
| Yesterday electricity use | 48 metered intervals sum to 16.75 kWh; total is calculated and links the metered input |
| Building consumption spike | One 5 kWh interval is identified; screening reports that correlation cannot establish cause |
| Cheapest/cleanest battery charging | Real constrained MILP reaches 4 kWh final SOC; cost optimum costs 0.20 currency units; clean schedule emits 240 g in the fixture |
| Tomorrow solar estimate | Explicit 1 October forecast window, weather pivot and real pvlib PVWatts; estimated output retains forecast-input provenance |
| Power flow | Caller-supplied two-bus network converges; balance error is below 1e-6 MW; result stays simulated |
| Consumption versus weather/tariff | Matching windows/resolutions join into 24 rows; mixed units and input kinds remain in provenance |

The complete six-workflow scenario uses MCP JSON-RPC over the official in-memory
transport. A separate stdio process test proves the packaged CLI endpoint. A
separate streamable HTTP test proves remote MCP ingestion, including bearer-auth
schema discovery and execution. This is protocol integration evidence, not an
autonomous LLM benchmark. No OpenAI/Anthropic API credentials were used.

## Public live probes

[Raw summaries](live-probe.json) record five successful, nonempty checks at
23:03 UTC on 30 September, which was 00:03 on 1 October in London.

| Live provider action | Result |
|---|---|
| GB national carbon | 1 calculated actual interval |
| GB regional carbon forward query | 48 forecast intervals |
| Open-Meteo | 96 hourly variable rows |
| Octopus Agile public rates | 46 rate intervals |
| Elexon FUELHH generation | 960 metered grid rows |

The live probes use the same execution runtime and local artifact persistence.
Their output contains summaries and public product/location parameters, not
private credentials or meter data. Use `uv run python examples/live_probe.py` to
repeat them. Counts change as new provider data is published.

Octopus private consumption, Home Assistant and Emoncms use realistic contract
fixtures and credential-injection assertions. They were not tested against real
private installations. PowerMCP, ENTSO-E, Electricity Maps and deferred simulation
engines are not represented as tested integrations.

## Failures found and corrected

The integration/review loop corrected search ranking for specific local queries,
public-versus-private Octopus account scope, carbon actual measurement semantics,
regional carbon response shapes, start-only forecast validation, bounded default
tariff windows, battery offset timezone handling, provider function aliases,
source provenance across solver input translation, atomic MCP imports, default
executable permissions, blocked-stdin subprocess timeout coverage, and oversized
metadata compaction. Regression tests cover those executable cases.

Security checks cover cross-user/site account rejection, ambiguous accounts,
credential redaction before persistence, artifact session isolation, policy
revalidation after hooks, duplicate timestamps, DST 23/25-hour days, null bins,
MCP annotations that try to grant permissions, remote bearer authentication,
restricted subprocess environments, timeout and output limits.
