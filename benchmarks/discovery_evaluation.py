"""Measure frozen discovery intents against the production runtime search path.

This is a deterministic, local-only search evaluation. It does not execute
provider tools, contact accounts, or adjust the frozen corpus expectations.
Exact capability-name matches are the relevance signal; labels that cannot be
found in the production catalogue are reported separately from retrieval
misses because the corpus includes category-derived and not-yet-implemented
capability names.

Run from the repository root with the project environment::

    PYTHONPATH=src:. python -m benchmarks.discovery_evaluation \
        --output docs/evidence/discovery-evaluation-development.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from energy_agent_tools.app import build_agent
from energy_agent_tools.models import Session

from .scenarios import DiscoveryIntent, discovery_intents

SCHEMA_VERSION = "discovery-evaluation-v1"
TOP_K = (1, 3, 5, 10)
DEFAULT_SAMPLES = 3
REVIEW_GATE = 200
PRIVATE_FIXTURE_CAPABILITY = "discovery_eval_private_meter"

# These examples were mapped from the production tool descriptions and
# capability metadata. They are hand-derived checks, not independent reviews.
HAND_DERIVED_EXAMPLES: tuple[dict[str, Any], ...] = (
    {
        "id": "hand_consumption",
        "query": "How much electricity did I use yesterday?",
        "expected_capability": "get_energy_consumption",
        "accepted_tools": (
            "octopus_energy.get_consumption",
            "home_assistant.get_history",
            "openenergymonitor.get_feed",
            "WORKBENCH_SUMMARIZE",
        ),
    },
    {
        "id": "hand_carbon_intensity",
        "query": "Which tool returns GB carbon intensity with its source and unit?",
        "expected_capability": "get_carbon_intensity",
        "accepted_tools": (
            "carbon_intensity_gb.get_intensity",
            "electricitymaps.get_signal",
        ),
    },
    {
        "id": "hand_power_flow",
        "query": "Which tool runs an AC power flow simulation and reports voltage and line loading?",
        "expected_capability": "run_power_flow",
        "accepted_tools": (
            "engineering.run_power_flow",
            "pypsa.power_flow",
            "opendss.power_flow",
        ),
    },
)

# Out-of-domain probes intentionally contain no known catalogue vocabulary.
# They check whether token search returns anything for wholly unrelated input;
# they do not measure semantic safety or realistic negative intent precision.
NEGATIVE_PROBES: tuple[dict[str, str], ...] = (
    {"id": "negative_quanta_arc", "query": "quanta arc flux capacitor"},
    {"id": "negative_velocitron", "query": "velocitron picowave hexagonal"},
    {"id": "negative_zyzzyva", "query": "plinth zyzzyva candelabra"},
)


@dataclass(frozen=True, slots=True)
class RankedTool:
    """One public result returned by the production runtime search method."""

    name: str
    toolkit: str
    capabilities: tuple[str, ...]
    connection_available: bool

    def to_json(self, expected_capability: str | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "toolkit": self.toolkit,
            "capabilities": list(self.capabilities),
            "connection_available": self.connection_available,
            "exact_capability_match": (
                expected_capability in self.capabilities
                if expected_capability is not None
                else False
            ),
        }


@dataclass(frozen=True, slots=True)
class IntentResult:
    """A frozen corpus expectation and the measured ranked search response."""

    id: str
    split: str
    origin: str
    category: str
    query: str
    expected_capability: str
    expected_tools: tuple[str, ...]
    expected_terms: tuple[str, ...]
    provider_hints: tuple[str, ...]
    label_status: str
    label_evidence: str
    catalogue_candidates: tuple[str, ...]
    ranked_results: tuple[RankedTool, ...]
    latency_samples_ms: tuple[float, ...]
    query_error: str | None = None

    @property
    def first_relevant_rank(self) -> int | None:
        if self.label_status != "grounded_catalogue_label":
            return None
        return next(
            (
                rank
                for rank, tool in enumerate(self.ranked_results, start=1)
                if self.expected_capability in tool.capabilities
            ),
            None,
        )

    @property
    def relevant_results(self) -> tuple[tuple[int, RankedTool], ...]:
        if self.label_status != "grounded_catalogue_label":
            return ()
        return tuple(
            (rank, tool)
            for rank, tool in enumerate(self.ranked_results, start=1)
            if self.expected_capability in tool.capabilities
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "split": self.split,
            "origin": self.origin,
            "category": self.category,
            "query": self.query,
            "expected_capability": self.expected_capability,
            "expected_tools": list(self.expected_tools),
            "expected_terms": list(self.expected_terms),
            "provider_hints": list(self.provider_hints),
            "label_provenance": {
                "origin": self.origin,
                "category": self.category,
                "capability_assignment": (
                    "Scenario-derived value emitted by discovery_intents(); see the pinned corpus source hash."
                    if self.origin == "scenario"
                    else "Literal capability value from a frozen reviewed_query record."
                ),
                "catalogue_grounding": self.label_evidence,
                "gateway_tool_expectations": "corpus expected_tools; not canonical registry tool names",
                "expected_terms": "frozen corpus values; not used to infer relevance",
            },
            "label_status": self.label_status,
            "catalogue_candidates": list(self.catalogue_candidates),
            "target_tool_ambiguity": (
                "Multiple production tools expose this exact capability; any is an accepted exact-label match."
                if len(self.catalogue_candidates) > 1
                else None
            ),
            "first_relevant_rank_at_10": self.first_relevant_rank,
            "relevant_results": [
                {"rank": rank, **tool.to_json(self.expected_capability)}
                for rank, tool in self.relevant_results
            ],
            "ranked_results": [
                tool.to_json(self.expected_capability) for tool in self.ranked_results
            ],
            "latency_samples_ms": list(self.latency_samples_ms),
            "latency_median_ms": (
                statistics.median(self.latency_samples_ms) if self.latency_samples_ms else None
            ),
            "query_error": self.query_error,
        }


def aggregate_metrics(
    results: Sequence[IntentResult], top_ks: Sequence[int] = TOP_K
) -> dict[str, Any]:
    """Aggregate exact-label retrieval metrics from already measured results."""

    total = len(results)
    grounded = [result for result in results if result.label_status == "grounded_catalogue_label"]
    distinct_labels = {result.expected_capability for result in results}
    grounded_labels = {result.expected_capability for result in grounded}
    hits = {
        k: sum(
            result.first_relevant_rank is not None and result.first_relevant_rank <= k
            for result in grounded
        )
        for k in top_ks
    }
    latency = [sample for result in results for sample in result.latency_samples_ms]
    returned = [tool for result in results for tool in result.ranked_results]
    relevant = [tool for result in grounded for _, tool in result.relevant_results]
    status_counts = Counter(result.label_status for result in results)
    return {
        "intent_count": total,
        "grounded_intent_count": len(grounded),
        "unique_expected_capability_count": len(distinct_labels),
        "unique_grounded_capability_count": len(grounded_labels),
        "intent_label_coverage": _ratio(len(grounded), total),
        "unique_label_coverage": _ratio(len(grounded_labels), len(distinct_labels)),
        "label_status_counts": dict(sorted(status_counts.items())),
        "recall_at_k": {
            str(k): {
                "hits": hits[k],
                "denominator": len(grounded),
                "rate": _ratio(hits[k], len(grounded)),
            }
            for k in top_ks
        },
        "mean_reciprocal_rank_at_10": _ratio(
            sum(
                1 / result.first_relevant_rank
                for result in grounded
                if result.first_relevant_rank is not None and result.first_relevant_rank <= 10
            ),
            len(grounded),
        ),
        "end_to_end_exact_label_hit_rate_at_k": {
            str(k): {"hits": hits[k], "denominator": total, "rate": _ratio(hits[k], total)}
            for k in top_ks
        },
        "search_result_connection_availability": {
            "available_results": sum(tool.connection_available for tool in returned),
            "returned_results": len(returned),
            "rate": _ratio(sum(tool.connection_available for tool in returned), len(returned)),
        },
        "relevant_result_connection_availability": {
            "available_relevant_results": sum(tool.connection_available for tool in relevant),
            "returned_relevant_results": len(relevant),
            "rate": _ratio(sum(tool.connection_available for tool in relevant), len(relevant)),
        },
        "latency_ms": _latency_summary(latency),
    }


def evaluate_discovery(
    repo_root: Path | None = None,
    *,
    samples: int = DEFAULT_SAMPLES,
) -> dict[str, Any]:
    """Run all 221 corpus queries and deterministic local scope fixtures."""

    if samples < 1:
        raise ValueError("samples must be positive")
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    intents = discovery_intents()
    scope_config = _scope_fixture_config()
    with TemporaryDirectory(prefix="discovery-evaluation-") as directory:
        agent = build_agent(Path(directory), scope_config)
        try:
            audit_session = Session(user_id="discovery-audit")
            capability_tools = _visible_capability_tools(agent, audit_session)
            results = tuple(
                _measure_intent(agent, audit_session, intent, capability_tools, samples)
                for intent in intents
            )
            hand_examples = _run_hand_examples(agent, audit_session)
            negatives = _run_negative_probes(agent, audit_session)
            account_visibility = _run_account_visibility(agent)
            catalogue = _catalogue_identity(agent)
        finally:
            asyncio.run(agent.close())

    grouped: dict[str, list[IntentResult]] = defaultdict(list)
    for result in results:
        grouped[result.split].append(result)
    all_metrics = aggregate_metrics(results)
    source_labels = Counter(intent.origin for intent in intents)
    absent_label_findings = _absent_label_findings(results)
    retrieval_misses = [
        _failure_record(result, "search_implementation_miss")
        for result in results
        if result.label_status == "grounded_catalogue_label" and result.first_relevant_rank is None
    ]
    capability_gaps = [
        _failure_record(result, "catalogue_capability_gap")
        for result in results
        if result.label_status == "explicit_capability_absent_from_catalogue"
    ]
    negative_failures = [probe for probe in negatives if probe["false_positive"]]
    hand_failures = [example for example in hand_examples if not example["passes"]]
    scope_failures = [case for case in account_visibility["cases"] if not case["passes"]]
    query_errors = [
        {"id": result.id, "query": result.query, "error": result.query_error}
        for result in results
        if result.query_error
    ]
    failures = {
        "search_implementation_misses": retrieval_misses,
        "explicit_capability_coverage_gaps": capability_gaps,
        "hand_derived_example_misses": hand_failures,
        "account_visibility_misses": scope_failures,
        "negative_probe_false_positives": negative_failures,
        "query_errors": query_errors,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "source_identity": _source_identity(root, intents, catalogue),
        "gate": {
            "threshold_independently_reviewed_intents": REVIEW_GATE,
            "source_intent_count": len(intents),
            "origin_counts": dict(sorted(source_labels.items())),
            "verified_independently_reviewed_intent_count": 0,
            "meets_gate": False,
            "reason": (
                "The corpus records intent origins but contains no reviewer identities,"
                " independent review records, or adjudication evidence."
            ),
        },
        "method": {
            "runtime_path": "EnergyAgent.search -> Registry.search",
            "result_limit": 10,
            "reported_k": list(TOP_K),
            "warmups_per_intent": 1,
            "measured_samples_per_intent": samples,
            "relevance_rule": "Exact match of expected capability to returned tool capabilities.",
            "execution_scope": "Local registry and deterministic fixture objects; no tool execution or network calls.",
            "heldout_status": (
                "Search-only diagnostics are reported by corpus split; this is not held-out scenario completion or qualification."
            ),
        },
        "runtime": {
            "python": platform.python_version(),
            "catalogue_tool_count": catalogue["tool_count"],
            "catalogue_toolkit_count": catalogue["toolkit_count"],
            "catalogue_capability_count": len(catalogue["capability_tools"]),
            "catalogue_identity_sha256": catalogue["sha256"],
            "dependencies": catalogue["dependencies"],
        },
        "counts": {
            "intent_count": len(intents),
            "by_split": dict(sorted(Counter(intent.split for intent in intents).items())),
            "by_origin": dict(sorted(source_labels.items())),
            "expected_label_count": len({intent.capability for intent in intents}),
            "catalogue_grounded_intent_count": all_metrics["grounded_intent_count"],
            "absent_derived_label_intent_count": sum(
                result.label_status == "derived_category_label_absent_from_catalogue"
                for result in results
            ),
            "explicit_capability_gap_intent_count": len(capability_gaps),
            "verified_independent_review_count": 0,
        },
        "metrics": all_metrics,
        "split_metrics": {
            split: aggregate_metrics(split_results)
            for split, split_results in sorted(grouped.items())
        },
        "label_findings": absent_label_findings,
        "hand_derived_examples": hand_examples,
        "negative_probes": {
            "count": len(negatives),
            "no_result_count": sum(not probe["returned_results"] for probe in negatives),
            "false_positive_rate": _ratio(len(negative_failures), len(negatives)),
            "probes": negatives,
            "interpretation": "Artificial lexical out-of-domain probes; not semantic safety negatives.",
        },
        "account_visibility": account_visibility,
        "failures": failures,
        "failure_counts": {name: len(items) for name, items in failures.items()},
        "limitations": [
            "The 221 corpus entries are not 221 verified independent reviews. The corpus origin field is source metadata, not proof of reviewer identity or independence.",
            "Scenario-derived expected capabilities come from a category-to-capability heuristic in benchmarks/scenarios.py; absent exact labels are reported as ambiguity and excluded from retrieval recall.",
            "An explicit corpus capability absent from the catalogue is reported as a capability coverage gap. Expected terms and abstract gateway tool names are not used to invent relevance judgments.",
            "Exact capability-name matching is conservative and cannot recognize valid synonyms or settle ambiguous intent semantics.",
            "Search-only held-out split measurements do not establish completion, scenario correctness, or a held-out release gate.",
            "Connection availability reflects the local runtime, installed optional dependencies, and credential-free deterministic fixtures; no real accounts or providers were used.",
            "Negative probes are synthetic out-of-domain strings and do not estimate precision for realistic unsupported or unsafe requests.",
            "Latency varies with local hardware and process load; each query is warmed once and measured repeatedly, with source and environment identity recorded.",
        ],
        "intent_results": [result.to_json() for result in results],
    }


def _measure_intent(
    agent: Any,
    session: Session,
    intent: DiscoveryIntent,
    capability_tools: Mapping[str, Sequence[str]],
    samples: int,
) -> IntentResult:
    candidates = tuple(sorted(capability_tools.get(intent.capability, ())))
    if candidates:
        label_status = "grounded_catalogue_label"
        label_evidence = (
            "Exact capability appears on a production tool or visible reviewed binding."
        )
    elif intent.origin == "scenario":
        label_status = "derived_category_label_absent_from_catalogue"
        label_evidence = (
            "Scenario label is category-derived; exact name is absent from this catalogue."
        )
    else:
        label_status = "explicit_capability_absent_from_catalogue"
        label_evidence = "Corpus supplies an explicit capability name absent from this catalogue."

    last_results: tuple[RankedTool, ...] = ()
    error = None
    try:
        agent.search(session, intent.query, 10)
        timings = []
        for _ in range(samples):
            start = time.perf_counter()
            public_results = agent.search(session, intent.query, 10)
            timings.append((time.perf_counter() - start) * 1000)
        last_results = tuple(
            RankedTool(
                name=item["name"],
                toolkit=item["toolkit"],
                capabilities=tuple(item.get("capabilities", ())),
                connection_available=bool(item.get("connection_available", False)),
            )
            for item in public_results
        )
    except Exception as exc:  # retain unexpected failures in the evidence
        timings = []
        error = f"{type(exc).__name__}: {exc}"
    return IntentResult(
        id=intent.id,
        split=intent.split,
        origin=intent.origin,
        category=intent.category,
        query=intent.query,
        expected_capability=intent.capability,
        expected_tools=tuple(intent.expected_tools),
        expected_terms=tuple(intent.expected_terms),
        provider_hints=tuple(intent.provider_hints),
        label_status=label_status,
        label_evidence=label_evidence,
        catalogue_candidates=candidates,
        ranked_results=last_results,
        latency_samples_ms=tuple(round(sample_ms, 6) for sample_ms in timings),
        query_error=error,
    )


def _visible_capability_tools(agent: Any, session: Session) -> dict[str, tuple[str, ...]]:
    mapping: dict[str, set[str]] = defaultdict(set)
    for tool in agent.registry.tools.values():
        for capability in tool.capabilities:
            mapping[capability].add(tool.name)
    for name, capabilities in agent.resolver.scoped_capabilities(session).items():
        for capability in capabilities:
            mapping[capability].add(name)
    return {capability: tuple(sorted(names)) for capability, names in mapping.items()}


def _run_hand_examples(agent: Any, session: Session) -> list[dict[str, Any]]:
    output = []
    for example in HAND_DERIVED_EXAMPLES:
        results = agent.search(session, example["query"], 10)
        ranked = [
            RankedTool(
                name=item["name"],
                toolkit=item["toolkit"],
                capabilities=tuple(item.get("capabilities", ())),
                connection_available=bool(item.get("connection_available", False)),
            )
            for item in results
        ]
        matching_tools = [
            (rank, tool)
            for rank, tool in enumerate(ranked, start=1)
            if tool.name in example["accepted_tools"]
            and example["expected_capability"] in tool.capabilities
        ]
        output.append(
            {
                "id": example["id"],
                "query": example["query"],
                "expected_capability": example["expected_capability"],
                "accepted_tools": list(example["accepted_tools"]),
                "passes": bool(matching_tools),
                "matching_results": [
                    {"rank": rank, **tool.to_json(example["expected_capability"])}
                    for rank, tool in matching_tools
                ],
                "ranked_results": [tool.to_json(example["expected_capability"]) for tool in ranked],
            }
        )
    return output


def _run_negative_probes(agent: Any, session: Session) -> list[dict[str, Any]]:
    output = []
    for probe in NEGATIVE_PROBES:
        results = agent.search(session, probe["query"], 10)
        names = [item["name"] for item in results]
        output.append(
            {
                **probe,
                "returned_results": names,
                "false_positive": bool(names),
            }
        )
    return output


def _run_account_visibility(agent: Any) -> dict[str, Any]:
    query = PRIVATE_FIXTURE_CAPABILITY
    cases = (
        {
            "id": "authorized_owner_site_account",
            "session": Session(
                user_id="scope-owner",
                site_id="scope-home",
                account_ids={"octopus-energy-account": "scope-owner-account"},
            ),
            "should_see": True,
        },
        {
            "id": "owner_selected_other_account",
            "session": Session(
                user_id="scope-owner",
                site_id="scope-home",
                account_ids={"octopus-energy-account": "scope-second-account"},
            ),
            "should_see": False,
        },
        {
            "id": "owner_at_other_site",
            "session": Session(user_id="scope-owner", site_id="scope-annex"),
            "should_see": False,
        },
        {
            "id": "different_user_and_site",
            "session": Session(user_id="scope-other-user", site_id="scope-other-site"),
            "should_see": False,
        },
    )
    observations = []
    for case in cases:
        matches = agent.search(case["session"], query, 10)
        visible_tools = [
            item["name"]
            for item in matches
            if PRIVATE_FIXTURE_CAPABILITY in item.get("capabilities", ())
        ]
        visible = bool(visible_tools)
        observations.append(
            {
                "id": case["id"],
                "expected_visible": case["should_see"],
                "observed_visible": visible,
                "visible_tools": visible_tools,
                "passes": visible == case["should_see"],
            }
        )
    return {
        "fixture": "Synthetic local account and site bindings on the production runtime; auth scheme none; no provider execution.",
        "marker_capability": PRIVATE_FIXTURE_CAPABILITY,
        "case_count": len(observations),
        "passed": sum(case["passes"] for case in observations),
        "cases": observations,
    }


def _scope_fixture_config() -> dict[str, Any]:
    return {
        "sites": [
            {
                "id": "scope-home",
                "user_id": "scope-owner",
                "name": "Home",
                "timezone": "Europe/London",
            },
            {
                "id": "scope-annex",
                "user_id": "scope-owner",
                "name": "Annex",
                "timezone": "Europe/London",
            },
            {
                "id": "scope-other-site",
                "user_id": "scope-other-user",
                "name": "Other",
                "timezone": "Europe/London",
            },
        ],
        "accounts": [
            _fixture_account("scope-owner-account", "scope-owner", "scope-home"),
            _fixture_account("scope-second-account", "scope-owner", "scope-home"),
            _fixture_account("scope-other-account", "scope-other-user", "scope-other-site"),
        ],
        "assets": [
            {
                "id": "scope-owner-meter",
                "site_id": "scope-home",
                "kind": "meter",
                "name": "Fixture meter",
                "account_ids": ["scope-owner-account"],
            }
        ],
        "bindings": [
            {
                "capability": PRIVATE_FIXTURE_CAPABILITY,
                "tool": "octopus_energy.get_consumption",
                "account_id": "scope-owner-account",
                "asset_id": "scope-owner-meter",
                "reviewed": True,
            }
        ],
    }


def _fixture_account(account_id: str, user_id: str, site_id: str) -> dict[str, Any]:
    return {
        "id": account_id,
        "user_id": user_id,
        "toolkit": "octopus-energy-account",
        "site_id": site_id,
        "auth": {"scheme": "none"},
    }


def _absent_label_findings(results: Sequence[IntentResult]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for result in results:
        if result.label_status != "grounded_catalogue_label":
            grouped[(result.expected_capability, result.label_status)].append(result.id)
    return [
        {
            "capability": capability,
            "classification": label_status,
            "intent_count": len(intent_ids),
            "intent_ids": sorted(intent_ids),
        }
        for (capability, label_status), intent_ids in sorted(grouped.items())
    ]


def _failure_record(result: IntentResult, failure_type: str) -> dict[str, Any]:
    return {
        "type": failure_type,
        "intent_id": result.id,
        "split": result.split,
        "origin": result.origin,
        "expected_capability": result.expected_capability,
        "candidate_tools": list(result.catalogue_candidates),
        "first_relevant_rank_at_10": result.first_relevant_rank,
        "query_error": result.query_error,
    }


def _catalogue_identity(agent: Any) -> dict[str, Any]:
    tools = sorted(agent.registry.tools.values(), key=lambda tool: tool.name)
    descriptions = [
        {
            "name": tool.name,
            "toolkit": tool.toolkit,
            "description": tool.description,
            "capabilities": sorted(tool.capabilities),
            "actions": sorted(action.value for action in tool.actions),
            "dependencies": sorted(tool.dependencies),
        }
        for tool in tools
    ]
    capability_tools: dict[str, set[str]] = defaultdict(set)
    for tool in tools:
        for capability in tool.capabilities:
            capability_tools[capability].add(tool.name)
    for name, capabilities in agent.resolver.scoped_capabilities(
        Session(user_id="discovery-audit")
    ).items():
        for capability in capabilities:
            capability_tools[capability].add(name)
    dependencies = {
        dependency: _dependency_present(dependency)
        for tool in tools
        for dependency in tool.dependencies
    }
    identity = {
        "tools": descriptions,
        "toolkits": sorted(agent.registry.toolkits),
        "capability_tools": {
            capability: sorted(names) for capability, names in sorted(capability_tools.items())
        },
        "dependencies": dependencies,
    }
    return {
        "tool_count": len(tools),
        "toolkit_count": len(agent.registry.toolkits),
        "capability_tools": identity["capability_tools"],
        "dependencies": dependencies,
        "sha256": _sha256_bytes(_canonical_json(identity).encode("utf-8")),
    }


def _dependency_present(dependency: str) -> bool:
    from importlib.util import find_spec

    return find_spec(dependency) is not None


def _source_identity(
    repo_root: Path,
    intents: Sequence[DiscoveryIntent],
    catalogue: Mapping[str, Any],
) -> dict[str, Any]:
    source_paths = (
        "benchmarks/scenarios.py",
        "src/energy_agent_tools/app.py",
        "src/energy_agent_tools/capabilities.py",
        "src/energy_agent_tools/registry.py",
        "src/energy_agent_tools/runtime.py",
    )
    source_hashes = {
        relative: _hash_file(repo_root / relative)
        for relative in source_paths
        if (repo_root / relative).is_file()
    }
    corpus_value = [intent.to_json() for intent in intents]
    head = _git_head(repo_root)
    evaluator_path = Path(__file__).resolve()
    return {
        "git_head_revision": head,
        "production_source_sha256": source_hashes,
        "scenario_corpus_source_sha256": source_hashes.get("benchmarks/scenarios.py"),
        "serialized_discovery_intents_sha256": _sha256_bytes(
            _canonical_json(corpus_value).encode("utf-8")
        ),
        "evaluator_source_sha256": _hash_file(evaluator_path),
        "catalogue_identity_sha256": catalogue["sha256"],
    }


def _git_head(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def _hash_file(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _latency_summary(samples_ms: Sequence[float]) -> dict[str, float | int | None]:
    if not samples_ms:
        return {"sample_count": 0, "median": None, "p95": None, "max": None}
    ordered = sorted(samples_ms)
    p95_index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return {
        "sample_count": len(ordered),
        "median": round(statistics.median(ordered), 6),
        "p95": round(ordered[p95_index], 6),
        "max": round(ordered[-1], 6),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="Write JSON evidence to this path; default is stdout."
    )
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    args = parser.parse_args(argv)
    report = evaluate_discovery(samples=args.samples)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
