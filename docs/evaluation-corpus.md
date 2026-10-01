# Energy Agent Tools evaluation corpus

This directory adds a reviewed scenario corpus for the next evaluation
milestone. It is intentionally separate from `benchmarks/harness.py` and the
existing synthetic fixture so that a larger task set can be reviewed before it
is connected to live-model execution.

The corpus currently contains 141 distinct scenarios:

| Split or status | Count | Meaning |
| --- | ---: | --- |
| Development | 83 | Prompt and scoring contracts used while building integrations and fixtures |
| Held out | 58 | Prompts reserved for evaluation after the corresponding capability work is complete |
| Executable fixture | 19 | The existing 19 cases from `benchmarks.harness.benchmark_cases()` |
| Pending environment | 122 | New cases that need a qualified provider, site, account, or engineering fixture |

It also exports 221 discovery intents: the 141 scenario queries plus 80
separately reviewed capability-search queries. These are categorized by
capability, provider hints, expected gateway tools, and development or
held-out split. The extra queries are explicit tasks, not a Cartesian product
of synonyms.

The 19 executable cases are copied as explicit contracts so the corpus can
refer to the current fixture without silently treating new cases as runnable.
Every pending case has `fixture_case_id=None` and at least one concrete
`environment_requirements` entry. Every held-out case is pending. No model runs
or provider calls are performed by this module.

## Scenario shape

`benchmarks/scenarios.py` defines two frozen dataclasses:

* `ExpectedTruth` contains the observable contract used by the current scorer:
  required and forbidden language, accepted tool paths, roles, error codes,
  argument constraints, numeric tolerances, provenance and site-asset flags,
  call budget, and a safety marker. It also records the expected outcome
  (`answer`, `advisory`, `clarify`, `degrade`, `refuse`, or `simulate`).
* `ScenarioCase` adds the natural-language task, category, development or
  held-out split, provider, site, IANA timezone, account mode, substitution
  group, readiness status, environment requirements, and optional existing
  fixture ID.
* `DiscoveryIntent` is the smaller search benchmark contract. It records a
  capability name, category, provider hints, accepted gateway tools, and
  expected terms. The corpus derives one discovery record from each scenario
  and adds 80 independently reviewed search queries.

`ScenarioCase.harness_kwargs()` projects the case back to the fields accepted
by the current `BenchmarkCase` constructor. The roadmap integration can use
that adapter after the required provider and fixture environments exist.

The substitution group is a semantic family, not a prompt synonym. The corpus
contains cross-split families for provider and site substitution across meter,
solar, carbon, weather, and engineering cases. Prompts remain distinct and
each case has its own expected truth contract.

The cases cover:

* local-day, billing-window, current-power, stale telemetry, counter resets,
  gaps, outages, and 23/25-hour DST days;
* p/kWh, GBP/EUR, W/Wh/kW/kWh/MW, carbon units, field-level units, and
  power-versus-energy duration semantics;
* tariff comparisons, standing charges, forecast cost, PV self-consumption,
  regional-versus-site generation, wind/PV model prerequisites, and
  measured-versus-forecast data kinds;
* battery state, reserves, capacity degradation, efficiency, thermal and
  export limits, EV scheduling, charger ambiguity, and demand response;
* missing credentials, expired OAuth, callback replay, user/site/account
  isolation, same-name assets, permission errors, bounded outage retries, and
  provider availability;
* provider quality flags, duplicate or malformed rows, overlap, coverage,
  provenance, power-flow and hydraulic simulation, solver dependencies, and
  reviewed write-action boundaries.

## Export

From the repository root, export the deterministic JSONL corpus and manifest:

```sh
python benchmarks/scenarios.py \
  --output export
```

The output contains `scenario-corpus.jsonl` and `manifest.json`. The manifest
records the schema version, counts by split/status/category/provider, the exact
19 fixture IDs, all pending IDs, the discovery-intent counts, and the assertion
that held-out cases are pending. `discovery-intents.jsonl` contains the
capability-search records for the discovery benchmark. The export directory is
an intermediate artifact and is not part of the source corpus.

## Validation

Run the independent contract tests without changing the existing benchmark or
fixture:

```sh
outputs/energy-agent-tools/.venv/bin/pytest -q \
  tests/test_scenarios.py
outputs/energy-agent-tools/.venv/bin/ruff check \
  benchmarks/scenarios.py \
  tests/test_scenarios.py
```

The tests check corpus size and prompt uniqueness, exact fixture coverage,
pending/readiness boundaries, cross-split provider and site substitution,
observable truth fields, the current harness adapter shape, deterministic
manifest and discovery counts, JSONL export, and rejection of an unsupported
fixture claim. Discovery validation also checks unique queries, both splits,
the reviewed-query origin, and non-empty capability/category expectations.

## What remains before live evaluation

The status field is a release gate. Before a pending case is sent to a real
agent, the project needs a deterministic fixture or a legitimate provider
probe for the named environment, with scoped accounts, declared units and
data kind, timezone-aware coverage, and a provenance record. Cases involving
private credentials must run only when the operator supplies an authorized
connection. Simulation cases require the named optional engine and bounded
resource policy. Held-out cases should remain untouched while development
fixtures and scoring are refined.

The primary benchmark integration should select cases by status, create the
corresponding fixture per `environment_requirements`, translate
`harness_kwargs()` to `BenchmarkCase`, and retain the corpus metadata beside
the model transcript. A pending case must not be counted as a pass, fail, or
partial result merely because its prompt resembles an executable case.
