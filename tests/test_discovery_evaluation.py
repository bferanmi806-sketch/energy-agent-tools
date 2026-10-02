from __future__ import annotations

import asyncio
from pathlib import Path

from benchmarks.discovery_evaluation import (
    HAND_DERIVED_EXAMPLES,
    IntentResult,
    RankedTool,
    _run_account_visibility,
    _run_hand_examples,
    _scope_fixture_config,
    aggregate_metrics,
)
from energy_agent_tools.app import build_agent
from energy_agent_tools.models import Session


def test_aggregate_metrics_separate_grounded_retrieval_from_label_gaps() -> None:
    results = (
        IntentResult(
            id="grounded_hit",
            split="development",
            origin="reviewed_query",
            category="consumption",
            query="read a meter",
            expected_capability="read_meter",
            expected_tools=("ENERGY_RESOLVE_CAPABILITY",),
            expected_terms=("meter",),
            provider_hints=("octopus-energy",),
            label_status="grounded_catalogue_label",
            label_evidence="direct",
            catalogue_candidates=("meter.read",),
            ranked_results=(
                RankedTool("other.tool", "other", ("other",), True),
                RankedTool("meter.read", "meter", ("read_meter",), False),
            ),
            latency_samples_ms=(1.0, 3.0),
        ),
        IntentResult(
            id="grounded_miss",
            split="development",
            origin="reviewed_query",
            category="tariff",
            query="calculate a tariff",
            expected_capability="calculate_tariff",
            expected_tools=("ENERGY_RESOLVE_CAPABILITY",),
            expected_terms=("tariff",),
            provider_hints=("octopus-energy",),
            label_status="grounded_catalogue_label",
            label_evidence="binding",
            catalogue_candidates=("tariff.calculate",),
            ranked_results=(RankedTool("other.tool", "other", ("other",), True),),
            latency_samples_ms=(2.0, 4.0),
        ),
        IntentResult(
            id="ambiguous_absent_label",
            split="heldout",
            origin="scenario",
            category="tariff",
            query="estimate an energy bill",
            expected_capability="estimate_energy_bill",
            expected_tools=("ENERGY_RESOLVE_CAPABILITY",),
            expected_terms=("bill",),
            provider_hints=("octopus-energy",),
            label_status="derived_category_label_absent_from_catalogue",
            label_evidence="heuristic",
            catalogue_candidates=(),
            ranked_results=(),
            latency_samples_ms=(5.0,),
        ),
    )

    metrics = aggregate_metrics(results, top_ks=(1, 2, 5))

    assert metrics["intent_count"] == 3
    assert metrics["grounded_intent_count"] == 2
    assert metrics["intent_label_coverage"] == 0.666667
    assert metrics["unique_label_coverage"] == 0.666667
    assert metrics["recall_at_k"]["1"] == {"hits": 0, "denominator": 2, "rate": 0.0}
    assert metrics["recall_at_k"]["2"] == {"hits": 1, "denominator": 2, "rate": 0.5}
    assert metrics["mean_reciprocal_rank_at_10"] == 0.25
    assert metrics["end_to_end_exact_label_hit_rate_at_k"]["2"]["rate"] == 0.333333
    assert metrics["relevant_result_connection_availability"] == {
        "available_relevant_results": 0,
        "returned_relevant_results": 1,
        "rate": 0.0,
    }
    assert metrics["latency_ms"] == {
        "sample_count": 5,
        "median": 3.0,
        "p95": 5.0,
        "max": 5.0,
    }


def test_hand_derived_examples_match_production_search(tmp_path: Path) -> None:
    agent = build_agent(tmp_path / "hand-example")
    try:
        results = _run_hand_examples(agent, Session(user_id="hand-example-test"))
    finally:
        asyncio.run(agent.close())

    assert len(results) == len(HAND_DERIVED_EXAMPLES)
    assert all(result["passes"] for result in results), results


def test_account_scoped_capability_is_visible_only_in_its_fixture_scope(tmp_path: Path) -> None:
    agent = build_agent(tmp_path / "scope", _scope_fixture_config())
    try:
        result = _run_account_visibility(agent)
    finally:
        asyncio.run(agent.close())

    assert result["case_count"] == 4
    assert result["passed"] == 4
    assert all(case["passes"] for case in result["cases"]), result
