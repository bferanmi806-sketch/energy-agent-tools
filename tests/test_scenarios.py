from __future__ import annotations

import importlib.util
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType

import pytest


def _load_module() -> ModuleType:
    path = Path(__file__).parents[1] / "benchmarks" / "scenarios.py"
    spec = importlib.util.spec_from_file_location("roadmap_scenarios", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load scenario module at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scenarios = _load_module()
CASES = scenarios.scenario_cases()


def test_corpus_is_large_distinct_and_split():
    assert len(CASES) >= 100
    assert len({case.id for case in CASES}) == len(CASES)
    assert len({case.prompt for case in CASES}) == len(CASES)
    assert {case.split for case in CASES} == {"development", "heldout"}
    assert sum(case.split == "development" for case in CASES) >= 40
    assert sum(case.split == "heldout" for case in CASES) >= 40


def test_existing_fixture_cases_are_explicit_and_complete():
    fixture_cases = {case.id: case for case in CASES if case.executable}
    assert set(fixture_cases) == scenarios.EXISTING_FIXTURE_CASE_IDS
    assert all(case.fixture_case_id == case.id for case in fixture_cases.values())
    assert all(case.split == "development" for case in fixture_cases.values())
    assert all(case.provider == "synthetic-fixture" for case in fixture_cases.values())
    assert all(case.environment_requirements for case in fixture_cases.values())


def test_pending_cases_cannot_claim_current_fixture_support():
    pending = [case for case in CASES if not case.executable]
    assert len(pending) >= 100
    assert all(case.status == "pending_environment" for case in pending)
    assert all(case.fixture_case_id is None for case in pending)
    assert all(case.environment_requirements for case in pending)
    assert all(case.status == "pending_environment" for case in CASES if case.split == "heldout")


def test_provider_and_site_substitution_crosses_the_split():
    grouped: defaultdict[str, list[scenarios.ScenarioCase]] = defaultdict(list)
    for case in CASES:
        grouped[case.substitution_group].append(case)

    cross_split = []
    for group, cases in grouped.items():
        splits = {case.split for case in cases}
        if splits != {"development", "heldout"}:
            continue
        development_pairs = {
            (case.provider, case.site) for case in cases if case.split == "development"
        }
        heldout_pairs = {(case.provider, case.site) for case in cases if case.split == "heldout"}
        if len({case.provider for case in cases}) > 1 and len({case.site for case in cases}) > 1:
            cross_split.append(group)
            assert development_pairs.isdisjoint(heldout_pairs)
    assert len(cross_split) >= 5


def test_each_case_has_an_observable_expected_truth_contract():
    for case in CASES:
        truth = case.expected
        assert truth.required_terms, case.id
        assert all(group and all(term.strip() for term in group) for group in truth.required_terms)
        assert truth.max_calls > 0
        for label, value, tolerance in truth.expected_numbers:
            assert label.strip()
            assert math.isfinite(value)
            assert math.isfinite(tolerance)
            assert tolerance >= 0
        if truth.safety_case:
            assert truth.forbidden_terms or truth.outcome in {"clarify", "refuse"}, case.id
        assert case.harness_kwargs()["id"] == case.id
        assert case.harness_kwargs()["prompt"] == case.prompt


def test_harness_adapter_preserves_existing_benchmark_field_shape():
    expected_fields = {
        "id",
        "prompt",
        "intent",
        "required_terms",
        "forbidden_terms",
        "expected_roles",
        "expected_tools",
        "required_error_codes",
        "expected_arguments",
        "expected_numbers",
        "require_provenance",
        "require_site_asset",
        "max_calls",
        "safety_case",
    }
    for case in CASES:
        kwargs = case.harness_kwargs()
        assert set(kwargs) == expected_fields
        assert kwargs["id"] == case.id
        assert kwargs["expected_tools"] == case.expected.expected_tools


def test_manifest_is_deterministic_and_matches_cases():
    manifest = scenarios.corpus_manifest()
    assert manifest["schema_version"] == "scenario-corpus-v1"
    assert manifest["total_cases"] == len(CASES)
    assert manifest["split_counts"] == {
        "development": sum(case.split == "development" for case in CASES),
        "heldout": sum(case.split == "heldout" for case in CASES),
    }
    assert manifest["status_counts"]["executable_fixture"] == len(
        [case for case in CASES if case.executable]
    )
    assert manifest["heldout_is_pending"] is True
    assert sorted(manifest["executable_fixture_ids"]) == sorted(scenarios.EXISTING_FIXTURE_CASE_IDS)
    assert manifest["discovery_intent_count"] >= 200
    assert manifest["reviewed_discovery_intent_count"] >= 70


def test_discovery_intents_are_reviewed_and_searchable():
    intents = scenarios.discovery_intents()
    assert len(intents) >= 200
    assert len({intent.id for intent in intents}) == len(intents)
    assert len({intent.query for intent in intents}) == len(intents)
    assert {intent.split for intent in intents} == {"development", "heldout"}
    assert {intent.origin for intent in intents} == {"scenario", "reviewed_query"}
    assert len([intent for intent in intents if intent.origin == "reviewed_query"]) >= 70
    assert all(intent.capability and intent.category for intent in intents)
    assert all(
        intent.provider_hints and intent.expected_tools and intent.expected_terms
        for intent in intents
    )


def test_export_writes_jsonl_and_manifest(tmp_path: Path):
    corpus_path, manifest_path = scenarios.export_corpus(tmp_path)
    discovery_path = tmp_path / "discovery-intents.jsonl"
    records = [json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines()]
    discovery_records = [
        json.loads(line) for line in discovery_path.read_text(encoding="utf-8").splitlines()
    ]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert corpus_path.name == "scenario-corpus.jsonl"
    assert manifest_path.name == "manifest.json"
    assert len(records) == len(CASES)
    assert len(discovery_records) >= 200
    assert {record["id"] for record in records} == {case.id for case in CASES}
    assert manifest["total_cases"] == len(records)
    assert manifest["discovery_intent_count"] == len(discovery_records)
    assert all(record["expected"]["required_terms"] for record in records)


def test_constructor_rejects_unsupported_fixture_claims():
    with pytest.raises(ValueError, match="unknown fixture case"):
        scenarios.ScenarioCase(
            id="bad_fixture_claim",
            prompt="This prompt is intentionally long enough for validation.",
            intent="test an invalid fixture claim",
            category="test",
            split="development",
            provider="unknown",
            site="test-site",
            timezone="UTC",
            account_mode="single",
            substitution_group="test",
            status="executable_fixture",
            expected=scenarios.ExpectedTruth(
                outcome="answer",
                required_terms=(("answer",),),
            ),
            environment_requirements=("test fixture",),
            fixture_case_id="not_in_fixture",
        )
