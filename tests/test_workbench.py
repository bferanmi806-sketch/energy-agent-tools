from __future__ import annotations

import pytest

from energy_agent_tools.models import DataKind, EnergyError, EnergyResult, Session
from energy_agent_tools.workbench import Workbench


def test_large_output_is_private_artifact_with_provenance(tmp_path):
    bench = Workbench(tmp_path, inline_bytes=1000)
    session = Session(user_id="u")
    result = EnergyResult(
        data=[{"timestamp": "2026-09-29T00:00:00Z", "kwh": i} for i in range(10000)],
        kind=DataKind.METERED,
        unit="kWh",
        source="fixture",
        provenance=[{"meter": "test-meter"}],
    )
    compact = bench.compact(session, result)
    assert compact["data"]["rows"] == 10000
    artifact = compact["data"]["artifact_id"]
    assert bench.read(session, artifact).provenance == [{"meter": "test-meter"}]
    summary = bench.summarize(session, artifact, "kwh")
    assert summary.data["sum"] == 49995000
    assert summary.kind == DataKind.CALCULATED
    assert summary.provenance[0]["input_kind"] == "metered"
    with pytest.raises(EnergyError):
        bench.read(Session(user_id="other"), artifact)
    with pytest.raises(EnergyError):
        bench.read(Session(user_id="u"), artifact)


def test_resample_handles_dst_and_missing_bins(tmp_path):
    bench = Workbench(tmp_path)
    sess = Session(user_id="u")
    result = EnergyResult(
        data=[
            {"time": "2026-10-25T01:00:00+01:00", "kwh": 1},
            {"time": "2026-10-25T01:00:00+00:00", "kwh": 2},
        ],
        kind=DataKind.METERED,
        unit="kWh",
        source="fixture",
        timezone="Europe/London",
    )
    artifact = bench.persist(sess, result)["artifact_id"]
    out = bench.resample(sess, artifact, "time", "kwh", "1h", "sum")
    assert [r["kwh"] for r in out.data] == [1, 2]
    assert out.data[0]["timestamp"] != out.data[1]["timestamp"]
    daily = bench.resample(sess, artifact, "time", "kwh", "1D", "sum")
    assert daily.data[0]["kwh"] == 3
    assert daily.kind == DataKind.CALCULATED


def test_join_units_kinds_and_duplicates(tmp_path):
    bench = Workbench(tmp_path)
    sess = Session(user_id="u")
    left = EnergyResult(
        data=[{"time": "2026-09-30T01:00:00+01:00", "energy": 1}],
        kind=DataKind.METERED,
        unit="kWh",
        source="meter",
    )
    right = EnergyResult(
        data=[{"time": "2026-09-30T00:00:00Z", "price": 10}],
        kind=DataKind.FORECAST,
        unit="GBP_pence/kWh",
        source="tariff",
    )
    a = bench.persist(sess, left)["artifact_id"]
    b = bench.persist(sess, right)["artifact_id"]
    result = bench.join(sess, a, b, "time")
    assert result.data[0]["energy"] == 1
    assert result.data[0]["price"] == 10
    assert result.unit == "mixed"
    assert [p["input_kind"] for p in result.provenance[:2]] == ["metered", "forecast"]
    right.data *= 2
    duplicate = bench.persist(sess, right)["artifact_id"]
    with pytest.raises(EnergyError, match="duplicated"):
        bench.join(sess, a, duplicate, "time")


def test_trusted_python_and_naive_timestamp_rejected(tmp_path):
    bench = Workbench(tmp_path)
    sess = Session(user_id="u")
    result = EnergyResult(
        data=[{"time": "2026-09-30T00:00:00", "energy": 1}],
        kind=DataKind.SIMULATED,
        unit="kWh",
        source="simulation",
    )
    a = bench.persist(sess, result)["artifact_id"]
    calc = bench.calculate(sess, a, lambda frame: float(frame.energy.sum()) * 2, "kWh")
    assert calc.data == 2
    assert calc.provenance[0]["input_kind"] == "simulated"
    with pytest.raises(EnergyError):
        bench.resample(sess, a, "time", "energy", "1h", "sum")


def test_empty_bins_remain_null(tmp_path):
    bench = Workbench(tmp_path)
    sess = Session(user_id="u")
    r = EnergyResult(
        data=[
            {"time": "2026-09-30T00:00:00Z", "energy": 1},
            {"time": "2026-09-30T02:00:00Z", "energy": 2},
        ],
        kind=DataKind.METERED,
        unit="kWh",
        source="test",
    )
    a = bench.persist(sess, r)["artifact_id"]
    assert bench.resample(sess, a, "time", "energy", "1h", "sum").data[1]["energy"] is None


def test_weather_pivot_preserves_units_and_input_kind(tmp_path):
    bench = Workbench(tmp_path)
    sess = Session(user_id="u")
    result = EnergyResult(
        data=[
            {
                "timestamp": "2026-09-30T00:00:00Z",
                "variable": "temperature",
                "value": 10,
                "unit": "C",
            },
            {
                "timestamp": "2026-09-30T00:00:00Z",
                "variable": "radiation",
                "value": 0,
                "unit": "W/m2",
            },
        ],
        kind=DataKind.FORECAST,
        unit="mixed",
        source="weather",
    )
    a = bench.persist(sess, result)["artifact_id"]
    pivot = bench.pivot(sess, a, "timestamp", "variable", "value")
    assert pivot.data[0]["temperature"] == 10
    assert pivot.provenance[0]["input_kind"] == "forecast"
    assert pivot.provenance[1]["column_units"]["temperature"] == ["C"]


def test_large_metadata_stays_in_artifact(tmp_path):
    import json

    bench = Workbench(tmp_path, inline_bytes=2000)
    sess = Session(user_id="u")
    r = EnergyResult(
        data=[{"value": 1}],
        kind=DataKind.SIMULATED,
        unit="kWh",
        source="test",
        provenance=[{"large_model": "x" * 20000}],
        warnings=["y" * 20000],
    )
    out = bench.compact(sess, r)
    assert len(json.dumps(out).encode()) < 2000
    stored = bench.read(sess, out["data"]["artifact_id"])
    assert len(stored.provenance[0]["large_model"]) == 20000
