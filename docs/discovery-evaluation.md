# Discovery relevance evaluation

`benchmarks/discovery_evaluation.py` measures the frozen 221-query discovery
corpus against the production `EnergyAgent.search` path and its registry
ranking. It returns the top ten tools for each intent, records connection
availability and latency, and evaluates exact capability-name relevance at
the top 1, 3, 5, and 10. It does not execute a provider tool or make a network
request.

The evaluator keeps three distinct outcomes. A corpus capability that exists
on a public tool or visible reviewed binding is grounded; a miss within the
top ten is a search implementation miss. A capability name derived from a
scenario category but absent from the catalogue is recorded as an ambiguous
category projection and excluded from retrieval recall. An explicit
capability name from a `reviewed_query` record that is absent from the
catalogue is recorded as a capability coverage gap. This preserves each
expectation as authored and avoids treating an unsupported label as evidence
that the ranker failed.

The corpus carries `origin` metadata, but that metadata does not prove that
anyone independently reviewed an intent. The report therefore records zero
verified independent reviews toward the 200-intent gate, while retaining
counts for the corpus's `scenario` and `reviewed_query` origins. No human
review, held-out scenario completion, or release qualification is claimed.

The evaluator also runs three hand-derived examples against production search,
three fixed out-of-domain negative probes, and four deterministic account and
site visibility cases. The hand-derived examples check consumption, carbon
intensity, and power-flow retrieval against tool capability metadata. The
negative probes use unrelated lexical tokens, so they only check that
out-of-domain strings return no catalogue results. The visibility fixtures
attach a synthetic reviewed capability to a local, credential-free account
and verify that the production resolver exposes it only to the matching user,
site, and selected account. These checks do not use an external provider.

## Run

Use the project virtual environment from the repository root:

```sh
PYTHONPATH="src:." .venv/bin/python -m benchmarks.discovery_evaluation \
  --output docs/evidence/discovery-evaluation-development.json
```

The JSON evidence records source hashes for the scenario corpus, evaluator,
runtime search implementation, production catalogue, and serialized intent
set. Latency depends on local hardware and load. The report includes all 221
ranked responses and every measured failure so results can be inspected
without rewriting corpus expectations.

Run the evaluator contract tests and style checks with:

```sh
.venv/bin/pytest -q tests/test_discovery_evaluation.py
.venv/bin/ruff check benchmarks/discovery_evaluation.py tests/test_discovery_evaluation.py
.venv/bin/ruff format --check benchmarks/discovery_evaluation.py tests/test_discovery_evaluation.py
```

## Limits

Exact capability equality is a conservative relevance rule. It does not
recognize synonyms, judge query semantics, or reconcile a scenario category
with several possible implementation capabilities. The corpus's abstract
`expected_tools` name the gateway surface and are not canonical production
registry tool names; `expected_terms` remain visible in the evidence but are
not used to infer a relevant result.

Search diagnostics include the held-out split so the same deterministic
retrieval path is visible across the corpus. They do not execute held-out
scenarios, validate answers, or establish a held-out release gate. The
credential-free environment reports the actual local dependency and
availability state, which can vary between machines. The synthetic negative
probes do not estimate precision for realistic unsafe or unsupported
requests.
