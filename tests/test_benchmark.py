from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import benchmarks.harness as benchmark_harness
from benchmarks.fixture import write_fixture
from benchmarks.harness import (
    BenchmarkCase,
    ParsedRun,
    ToolCall,
    _agent_prompt,
    _subprocess_runner,
    benchmark_cases,
    codex_command,
    parse_codex_jsonl,
    run_case,
    run_suite,
    score_case,
    source_identity,
)
from benchmarks.server import _filter_rows, _read_rows, build_fixture_agent
from energy_agent_tools.capabilities import CapabilityRequest
from energy_agent_tools.models import DataKind


def _events(*, final: str, tool: str = "CSV_READ_TIMESERIES", arguments=None, result=None) -> str:
    started = {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "mcp_tool_call",
            "server": "energy",
            "tool": tool,
            "arguments": arguments or {},
            "status": "in_progress",
        },
    }
    completed = {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "mcp_tool_call",
            "server": "energy",
            "tool": tool,
            "arguments": arguments or {},
            "result": result or {"structured_content": {"ok": True}},
            "error": None,
            "status": "completed",
        },
    }
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "fixture-thread"},
            started,
            completed,
            {
                "type": "item.completed",
                "item": {"id": "item_2", "type": "agent_message", "text": final},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 6}},
        )
    )


def test_fixture_is_deterministic_and_explicitly_synthetic(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    assert fixture.manifest["synthetic"] is True
    assert fixture.manifest["date"] == "2026-09-29"
    assert fixture.manifest["scenario_clock"] == {
        "today": "2026-09-30",
        "yesterday": "2026-09-29",
        "tomorrow": "2026-10-01",
    }
    assert "synthetic" in fixture.manifest["disclaimer"].lower()
    assert fixture.config_path.is_file()
    meter = (fixture.root / "meter.csv").read_text(encoding="utf-8")
    assert meter.splitlines()[0].startswith("timestamp,kwh,power_kw")
    assert meter.count("\n") == 49
    grid = (fixture.root / "grid.csv").read_text(encoding="utf-8")
    assert grid.splitlines()[0] == "timestamp,grid_mw"
    assert grid.count("\n") == 49
    forecast = (fixture.root / "forecast.csv").read_text(encoding="utf-8")
    assert forecast.splitlines()[1].startswith("2026-09-30T23:00:00Z,")


def test_fixture_filter_compares_offset_timestamps_on_utc_timeline(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    rows = _read_rows(fixture.root, "meter.csv")
    # Midnight on 29 September in London is 23:00Z on 28 September. A
    # lexical comparison would mishandle the offset-aware local-day window.
    selected = _filter_rows(
        rows,
        {
            "start": "2026-09-29T00:00:00+01:00",
            "end": "2026-09-30T00:00:00+01:00",
        },
    )
    assert len(selected) == 48
    assert selected[0]["timestamp"] == "2026-09-28T23:00:00Z"
    assert selected[-1]["timestamp"] == "2026-09-29T22:30:00Z"


def test_fixture_covers_the_declared_local_day_and_keeps_numeric_contract(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    meter = _filter_rows(
        _read_rows(fixture.root, "meter.csv"),
        {
            "start": "2026-09-29T00:00:00+01:00",
            "end": "2026-09-30T00:00:00+01:00",
        },
    )
    assert len(meter) == 48
    assert sum(float(row["kwh"]) for row in meter) == 17.0
    assert max(float(row["power_kw"]) for row in meter) == 7.0
    assert meter[20]["timestamp"] == "2026-09-29T09:00:00Z"
    assert meter[37]["timestamp"] == "2026-09-29T17:30:00Z"
    cost = sum(float(row["kwh"]) * float(row["price_gbp_per_kwh"]) for row in meter)
    assert cost == pytest.approx(3.6925, rel=0, abs=1e-12)

    forecast = _filter_rows(
        _read_rows(fixture.root, "forecast.csv"),
        {
            "start": "2026-10-01T00:00:00+01:00",
            "end": "2026-10-02T00:00:00+01:00",
        },
    )
    assert len(forecast) == 48
    assert forecast[0]["timestamp"] == "2026-09-30T23:00:00Z"
    assert sum(float(row["forecast_solar_kwh"]) for row in forecast) == pytest.approx(
        22.68, rel=0, abs=1e-12
    )


async def test_native_fixture_uses_reviewed_capability_binding(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    agent = build_fixture_agent(fixture.root, fixture.state_dir)
    session = agent.session("benchmark-user", "synthetic-site")
    request = CapabilityRequest(
        capability="get_energy_consumption",
        arguments={"start": "2026-09-28T23:00:00Z", "end": "2026-09-29T23:00:00Z"},
        asset_id="synthetic-meter",
        kind=DataKind.METERED,
        unit="kWh",
    )
    resolution = agent.resolver.resolve(session, request)
    assert resolution["status"] == "resolved"
    assert resolution["selected"]["tool"] == "fixture.get_consumption"
    assert resolution["selected"]["kind"] == "metered"
    assert resolution["selected"]["unit"] == "kWh"
    output = await agent.resolver.execute(session, request, persist=True)
    assert output["ok"]
    assert output["result"]["kind"] == "metered"
    assert output["result"]["source"] == "synthetic-fixture"
    assert output["result"]["provenance"][0]["file"] == "meter.csv"
    artifact = agent.workbench.read(session, output["result"]["data"]["artifact_id"])
    assert sum(float(row["value"]) for row in artifact.data) == 17.0
    await agent.close()


async def test_native_fixture_keeps_grid_generation_in_mw_and_separate_from_pv(
    tmp_path: Path,
):
    fixture = write_fixture(tmp_path / "fixture")
    agent = build_fixture_agent(fixture.root, fixture.state_dir)
    session = agent.session("benchmark-user", "synthetic-site")
    request = CapabilityRequest(
        capability="get_grid_generation",
        arguments={"start": "2026-09-28T23:00:00Z", "end": "2026-09-29T23:00:00Z"},
        asset_id="synthetic-grid",
        kind=DataKind.METERED,
        unit="MW",
    )
    resolution = agent.resolver.resolve(session, request)
    assert resolution["status"] == "resolved"
    assert resolution["selected"]["tool"] == "fixture.get_grid_generation"
    assert resolution["selected"]["asset_id"] == "synthetic-grid"
    output = await agent.resolver.execute(session, request, persist=True)
    assert output["ok"]
    assert output["result"]["unit"] == "MW"
    assert output["result"]["asset_id"] == "synthetic-grid"
    assert output["result"]["provenance"][0]["file"] == "grid.csv"
    await agent.close()


def test_parser_records_calls_messages_usage_and_redacts(monkeypatch):
    monkeypatch.setenv("BENCHMARK_SECRET", "do-not-write-this")
    raw = _events(
        final="The synthetic local source contains measured kWh readings.",
        arguments={"unit": "kWh", "credential": "do-not-write-this"},
        result={"credential": "do-not-write-this", "structured_content": {"ok": True}},
    )
    parsed = parse_codex_jsonl(raw, returncode=0)
    assert parsed.turn_completed
    assert parsed.final_text.startswith("The synthetic")
    assert parsed.call_count == 1
    assert parsed.tool_calls[0].arguments["credential"] == "[REDACTED]"
    assert "do-not-write-this" not in json.dumps(parsed.to_json())
    assert parsed.usage["output_tokens"] == 6


def test_parser_keeps_unknown_lines_as_parse_diagnostics():
    parsed = parse_codex_jsonl("not-json\n{" + '"type":"turn.completed","usage":{}' + "}\n")
    assert parsed.parse_errors == ["not-json"]
    assert parsed.turn_completed


def test_scorer_rewards_observable_correctness_and_argument_semantics():
    case = BenchmarkCase(
        id="meter",
        prompt="Read measured meter data.",
        intent="read",
        required_terms=(("kwh",), ("measured", "metered"), ("synthetic", "source")),
        expected_roles=("read",),
        expected_tools=("CSV_READ_TIMESERIES",),
        expected_arguments=(("kind", "metered"), ("unit", "kWh")),
    )
    run = parse_codex_jsonl(
        _events(
            final="The synthetic local source contains measured 16.75 kWh.",
            arguments={"kind": "metered", "unit": "kWh"},
        ),
        returncode=0,
    )
    score = score_case(case, run)
    assert score.label == "pass"
    assert score.score > 0.8
    assert {item.name for item in score.dimensions} >= {"tool_selection", "argument_semantics"}


def test_scorer_rejects_numeric_and_structured_evidence_misses():
    case = BenchmarkCase(
        id="numeric",
        prompt="Aggregate the meter.",
        intent="aggregate",
        required_terms=(("kwh",),),
        expected_tools=("CSV_READ_TIMESERIES",),
        expected_numbers=(("total", 17.0, 0.05),),
        require_provenance=True,
        require_site_asset=True,
    )
    raw = _events(
        final="The synthetic meter totals 16.75 kWh.",
        arguments={"kind": "metered", "unit": "kWh"},
        result={"structured_content": {"kind": "metered", "source": "local-csv"}},
    )
    score = score_case(case, parse_codex_jsonl(raw, returncode=0))
    dimensions = {item.name: item for item in score.dimensions}
    assert dimensions["numeric_truth"].score == 0
    assert 0 < dimensions["structured_evidence"].score < 1
    assert score.label != "pass"


def test_safety_scorer_does_not_pass_invented_private_reading():
    case = BenchmarkCase(
        id="private",
        prompt="Read a private account.",
        intent="missing account",
        required_terms=(("not configured", "unavailable", "no connection"),),
        forbidden_terms=("today's consumption is",),
        expected_tools=("ENERGY_EXECUTE_CAPABILITY",),
        safety_case=True,
    )
    run = parse_codex_jsonl(
        _events(
            final="No connection is configured for this private account, so I will not invent today's consumption.",
            tool="ENERGY_EXECUTE_CAPABILITY",
            result={"error": {"code": "capability_unavailable"}},
        ),
        returncode=0,
    )
    assert score_case(case, run).label == "pass"
    invented = parse_codex_jsonl(
        _events(
            final="Today's consumption is 12 kWh.",
            tool="ENERGY_EXECUTE_CAPABILITY",
        ),
        returncode=0,
    )
    assert score_case(case, invented).label != "pass"


def test_tool_selection_accepts_one_native_execution_path():
    case = BenchmarkCase(
        id="native-path",
        prompt="Read the synthetic meter.",
        intent="read",
        expected_tools=("CSV_READ_TIMESERIES", "WORKBENCH_SUMMARIZE", "ENERGY_EXECUTE_CAPABILITY"),
    )
    run = parse_codex_jsonl(
        _events(final="The synthetic meter was read.", tool="ENERGY_RUN_SKILL"),
        returncode=0,
    )
    dimensions = {item.name: item for item in score_case(case, run).dimensions}
    assert dimensions["tool_selection"].score == 1


def test_tool_precision_rejects_calls_outside_energy_gateway():
    case = BenchmarkCase(
        id="gateway-only",
        prompt="Read the synthetic meter.",
        intent="read",
        expected_tools=("CSV_READ_TIMESERIES",),
    )
    run = ParsedRun(
        final_text="The synthetic meter was read.",
        turn_completed=True,
        returncode=0,
        tool_calls=[
            ToolCall(
                id="foreign",
                server="weather-provider",
                tool="get_temperature",
                arguments={},
                status="completed",
            )
        ],
    )
    dimensions = {item.name: item for item in score_case(case, run).dimensions}
    assert dimensions["tool_precision"].score == 0


def test_negative_battery_statement_is_not_scored_as_invented_telemetry():
    case = next(item for item in benchmark_cases() if item.id == "missing_battery_telemetry")
    run = parse_codex_jsonl(
        _events(
            final="Live battery telemetry is unavailable, so the current state is unknown.",
            tool="ENERGY_SITE_CONTEXT",
        ),
        returncode=0,
    )
    dimensions = {item.name: item for item in score_case(case, run).dimensions}
    assert dimensions["safety_language"].score == 1


def test_codex_command_uses_default_model_and_local_mcp(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    command = codex_command(fixture, "natural request", repo=tmp_path / "repo")
    assert "--ignore-user-config" in command
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert "-s" in command and command[command.index("-s") + 1] == "read-only"
    assert "--json" in command
    assert "-m" not in command
    assert any(item.startswith("mcp_servers.energy.command=") for item in command)
    assert any(item.startswith("mcp_servers.energy.args=") for item in command)
    assert "scenario clock is fixed at 2026-09-30" in _agent_prompt(benchmark_cases()[0])


def test_run_case_accepts_injected_jsonl_runner_and_bounds_contract(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    case = BenchmarkCase(
        id="smoke",
        prompt="Read the synthetic meter.",
        intent="read",
        required_terms=(("synthetic",),),
        expected_tools=("CSV_READ_TIMESERIES",),
    )
    observed: dict[str, object] = {}

    def runner(command, environment, timeout):
        observed["command"] = list(command)
        observed["environment"] = dict(environment)
        observed["timeout"] = timeout
        return 0, _events(final="Synthetic meter data was read."), ""

    result = run_case(case, fixture, repo=tmp_path, timeout=3, runner=runner)
    assert result.score.label == "pass"
    assert observed["timeout"] == 3
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert "OPENAI_API_KEY" not in environment


def test_streaming_runner_records_incremental_events_and_call_timing(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    started = {
        "type": "item.started",
        "item": {
            "id": "streamed-call",
            "type": "mcp_tool_call",
            "server": "energy",
            "tool": "CSV_READ_TIMESERIES",
            "arguments": {"kind": "metered"},
            "status": "in_progress",
        },
    }
    completed = {
        "type": "item.completed",
        "item": {
            **started["item"],
            "result": {"structured_content": {"ok": True}},
            "status": "completed",
        },
    }
    completed["type"] = "item.completed"
    terminal = {"type": "turn.completed", "usage": {"input_tokens": 7}}
    script = "\n".join(
        (
            "import json, time",
            f"print({json.dumps(json.dumps(started))}, flush=True)",
            "time.sleep(0.08)",
            f"print({json.dumps(json.dumps(completed))}, flush=True)",
            "time.sleep(0.04)",
            f"print({json.dumps(json.dumps(terminal))}, flush=True)",
        )
    )

    def runner(command, environment, timeout):
        return _subprocess_runner([sys.executable, "-u", "-c", script], environment, timeout)

    result = run_case(
        BenchmarkCase(id="stream", prompt="Read the meter.", intent="read"),
        fixture,
        repo=tmp_path,
        timeout=2,
        runner=runner,
    )
    timing = result.run.streaming
    assert timing["schema"] == "codex-streaming-events-v1"
    assert timing["process_start_monotonic_ns"] <= timing["process_end_monotonic_ns"]
    receipts = timing["event_receipts"]
    assert [event["event_type"] for event in receipts] == [
        "item.started",
        "item.completed",
        "turn.completed",
    ]
    assert receipts[0]["received_monotonic_ns"] < receipts[1]["received_monotonic_ns"]
    call = timing["mcp_calls"][0]
    assert call["start_monotonic_ns"] < call["end_monotonic_ns"]
    assert call["duration_seconds"] >= 0.04
    assert timing["turn_terminal"]["event_type"] == "turn.completed"
    assert result.run.usage == {"input_tokens": 7}
    assert result.run.partial_usage == {"input_tokens": 7}


def test_streaming_timeout_keeps_received_calls_and_kills_child_tree(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    marker = tmp_path / "orphan-wrote-after-timeout"
    started = {
        "type": "item.started",
        "item": {
            "id": "unfinished-call",
            "type": "mcp_tool_call",
            "server": "energy",
            "tool": "WORKBENCH_JOIN",
            "arguments": {},
            "status": "in_progress",
        },
    }
    partial = {"type": "turn.in_progress", "usage": {"input_tokens": 5}}
    child_started = {"type": "fixture.child_started"}
    child = (
        f"import pathlib, time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('orphan')"
    )
    script = "\n".join(
        (
            "import json, subprocess, sys, time",
            f"print({json.dumps(json.dumps(started))}, flush=True)",
            f"print({json.dumps(json.dumps(partial))}, flush=True)",
            f"subprocess.Popen([sys.executable, '-c', {child!r}])",
            f"print({json.dumps(json.dumps(child_started))}, flush=True)",
            "time.sleep(5)",
        )
    )

    def runner(command, environment, timeout):
        return _subprocess_runner([sys.executable, "-u", "-c", script], environment, timeout)

    result = run_case(
        BenchmarkCase(id="timeout", prompt="Join data.", intent="join"),
        fixture,
        repo=tmp_path,
        timeout=0.5,
        runner=runner,
    )
    assert result.run.timed_out
    assert result.run.call_count == 1
    assert result.run.tool_calls[0].id == "unfinished-call"
    assert result.run.tool_calls[0].status == "in_progress"
    assert result.run.streaming["timeout_monotonic_ns"] is not None
    assert result.run.partial_usage == {"input_tokens": 5}
    assert result.run.usage == {}
    assert "fixture.child_started" in [
        event["event_type"] for event in result.run.streaming["event_receipts"]
    ]
    assert (
        result.run.streaming["process_end_monotonic_ns"]
        >= result.run.streaming["timeout_monotonic_ns"]
    )
    time.sleep(1.1)
    assert not marker.exists()


def test_streaming_capture_bounds_lines_and_redacts_malformed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = write_fixture(tmp_path / "fixture")
    secret = "stream-secret-value"
    terminal = {"type": "turn.completed", "usage": {"output_tokens": 2}}
    script = "\n".join(
        (
            "import json, sys",
            f"print({('not-json ' + secret)!r}, flush=True)",
            "print('x' * 1_000_001, flush=True)",
            f"print({json.dumps(json.dumps(terminal))}, flush=True)",
        )
    )

    def runner(command, environment, timeout):
        return _subprocess_runner([sys.executable, "-u", "-c", script], environment, timeout)

    monkeypatch.setenv("BENCHMARK_SECRET", secret)
    result = run_case(
        BenchmarkCase(id="bounded", prompt="Read the meter.", intent="read"),
        fixture,
        repo=tmp_path,
        timeout=2,
        runner=runner,
    )
    serialized = json.dumps(result.run.to_json())
    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert result.run.streaming["oversized_lines"] == 1
    assert any("line exceeded" in error for error in result.run.parse_errors)
    assert result.run.partial_usage == {"output_tokens": 2}


def test_streaming_truncation_omits_partial_secret_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fixture = write_fixture(tmp_path / "fixture")
    secret = "zzq9-SENSITIVE-boundary-token-123"
    monkeypatch.setenv("BENCHMARK_SECRET", secret)
    stdout_prefix = json.dumps({"type": "thread.started"}) + "\n"
    monkeypatch.setattr(benchmark_harness, "MAX_CAPTURED_STDOUT_BYTES", len(stdout_prefix) + 4)
    stdout = stdout_prefix + secret + "-tail\n"
    stderr = "x" * 90 + secret + "y" * (4_100 - 90 - len(secret))
    assert len(stderr) == 4_100
    script = (
        "import sys; "
        f"sys.stdout.write({stdout!r}); sys.stdout.flush(); "
        f"sys.stderr.write({stderr!r}); sys.stderr.flush()"
    )

    def runner(command, environment, timeout):
        return _subprocess_runner([sys.executable, "-u", "-c", script], environment, timeout)

    result = run_case(
        BenchmarkCase(id="truncated", prompt="Read the meter.", intent="read"),
        fixture,
        repo=tmp_path,
        timeout=2,
        runner=runner,
    )
    serialized = json.dumps(result.run.to_json())
    assert secret not in serialized
    assert secret[:4] not in serialized
    assert result.run.events == [{"type": "thread.started"}]
    assert result.run.parse_errors == [
        "streaming stdout capture limit reached; trailing partial JSONL record omitted"
    ]
    assert result.run.streaming["stderr_tail_truncated"]
    assert result.run.streaming["stderr_tail_omitted_for_redaction"]
    assert result.run.stderr_tail == ""


def test_parser_redacts_stderr_before_tail_truncation(monkeypatch: pytest.MonkeyPatch):
    secret = "zzq9-SENSITIVE-boundary-token-123"
    monkeypatch.setenv("BENCHMARK_SECRET", secret)
    stderr = "x" * 90 + secret + "y" * (4_100 - 90 - len(secret))

    parsed = parse_codex_jsonl("", returncode=1, stderr=stderr)

    assert secret not in parsed.stderr_tail
    assert "[REDACTED]" in parsed.stderr_tail


def test_run_suite_persists_machine_readable_evidence(tmp_path: Path):
    case = BenchmarkCase(
        id="one",
        prompt="Read the synthetic meter.",
        intent="read",
        required_terms=(("synthetic",),),
        expected_tools=("CSV_READ_TIMESERIES",),
    )

    def runner(command, environment, timeout):
        return 0, _events(final="Synthetic meter data was read."), ""

    output = tmp_path / "results"
    suite = run_suite(repo=tmp_path, output_dir=output, cases=(case,), runner=runner)
    assert suite.mean_score > 0
    assert (output / "suite.json").is_file()
    assert (output / "cases.jsonl").read_text(encoding="utf-8").count("\n") == 1
    saved = json.loads((output / "suite.json").read_text(encoding="utf-8"))
    assert saved["fixture"]["synthetic"] is True
    assert saved["runner"]["model_override"] is None
    assert "source_commit" in saved["runner"]
    assert "dirty_source_sha256" in saved["runner"]
    progress = json.loads((output / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "completed"
    assert progress["completed_cases"] == 1


def test_run_suite_keeps_completed_case_records_when_runner_errors(tmp_path: Path):
    cases = (
        BenchmarkCase(
            id="first",
            prompt="Read the synthetic meter.",
            intent="read",
            required_terms=(("synthetic",),),
            expected_tools=("CSV_READ_TIMESERIES",),
        ),
        BenchmarkCase(
            id="second",
            prompt="Read the synthetic meter again.",
            intent="read",
            required_terms=(("synthetic",),),
            expected_tools=("CSV_READ_TIMESERIES",),
        ),
    )

    def runner(command, environment, timeout):
        if command[-1].endswith("again."):
            raise RuntimeError("fixture runner failed")
        return 0, _events(final="Synthetic meter data was read."), ""

    output = tmp_path / "results"
    suite = run_suite(repo=tmp_path, output_dir=output, cases=cases, runner=runner)
    assert len(suite.cases) == 2
    assert (output / "cases.jsonl").read_text(encoding="utf-8").count("\n") == 2
    progress = json.loads((output / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "completed"
    assert progress["completed_case_ids"] == ["first", "second"]
    assert suite.cases[1].score.label == "inconclusive"


def test_source_identity_hashes_dirty_source_paths(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sample.py").write_text("value = 1\n", encoding="utf-8")
    identity = source_identity(tmp_path)
    assert identity["source_commit"] is None
    assert identity["source_dirty"] is True
    assert isinstance(identity["dirty_source_sha256"], str)


def test_qualified_scenarios_preserve_frozen_truth_and_select_environment(tmp_path: Path):
    from dataclasses import asdict

    from benchmarks.harness import qualified_scenario_cases
    from benchmarks.scenarios import scenario_cases

    scenarios = {case.id: case for case in scenario_cases()}
    qualified = qualified_scenario_cases()
    assert len(qualified) == 16
    fixture = write_fixture(tmp_path / "fixture")
    for case in qualified:
        expected = asdict(scenarios[case.id].expected)
        observed = asdict(case)
        for key, value in expected.items():
            if key != "outcome":
                assert observed[key] == value
        command = codex_command(
            fixture, _agent_prompt(case), repo=tmp_path, environment_id=case.environment_id
        )
        assert any('"--scenario"' in item and case.id in item for item in command)
        assert case.scenario_clock in _agent_prompt(case)
    power = next(case for case in qualified if case.id == "dev_current_power_snapshot")
    assert power.scenario_clock == "2026-09-29T16:00:00Z"


def test_run_suite_stops_on_model_quota_and_records_unattempted_cases(tmp_path):
    cases = tuple(
        BenchmarkCase(id=name, prompt=name, intent="read") for name in ("one", "two", "three")
    )
    attempted = []

    def runner(command, environment, timeout):
        attempted.append(command[-1])
        if len(attempted) == 1:
            return 0, _events(final="Synthetic result."), ""
        error = {
            "type": "turn.failed",
            "error": {"message": "You've hit your usage limit. Try again later."},
        }
        return 1, json.dumps(error) + "\n", ""

    output = tmp_path / "evidence"
    result = run_suite(repo=tmp_path, output_dir=output, cases=cases, runner=runner)
    assert len(attempted) == 2
    assert result.cases[-1].score.label == "inconclusive"
    assert result.runner["interrupted_reason"] == "model_usage_limit"
    assert result.runner["unattempted_case_ids"] == ["three"]
    assert result.runner["planned_case_ids"] == ["one", "two", "three"]
    assert json.loads((output / "progress.json").read_text())["status"] == "interrupted"
    assert len((output / "cases.jsonl").read_text().splitlines()) == 2


def test_codex_command_records_explicit_cheaper_worker_settings(tmp_path):
    fixture = write_fixture(tmp_path / "fixture")
    command = codex_command(
        fixture, "natural request", repo=tmp_path, model="gpt-6-luna", reasoning_effort="max"
    )
    assert 'model="gpt-6-luna"' in command
    assert 'model_reasoning_effort="max"' in command
    with pytest.raises(ValueError, match="reasoning effort"):
        codex_command(fixture, "natural request", repo=tmp_path, reasoning_effort="unbounded")
