from datetime import UTC, datetime, timedelta

import pytest

from energy_agent_tools import EnergyAgentTools
from energy_agent_tools.maintenance import create_backup, restore_backup
from energy_agent_tools.models import DataKind, EnergyError, EnergyResult, Session
from energy_agent_tools.workbench import Workbench


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
async def test_large_dataset_through_gateway_and_backup(tmp_path, interface):
    data = tmp_path / "data"
    data.mkdir()
    start = datetime(2020, 1, 1, tzinfo=UTC)
    with (data / "large.csv").open("w") as stream:
        stream.write("timestamp,end,value,physical_meter\n")
        for i in range(100_001):
            left = start + timedelta(minutes=30 * i)
            stream.write(
                f"{left.isoformat()},{(left + timedelta(minutes=30)).isoformat()},2,false\n"
            )
    config = {
        "sites": [{"id": "home", "user_id": "owner", "name": "Synthetic", "timezone": "UTC"}],
        "assets": [{"id": "meter", "site_id": "home", "kind": "meter", "name": "Synthetic"}],
    }
    state = tmp_path / "state"
    async with EnergyAgentTools(state, config, data_root=data) as energy:
        session = energy.session("owner", "home")

        async def call(tool, arguments, **options):
            if interface == "sdk":
                return await session.execute(tool, arguments, **options)
            response = await session.dispatch(
                "ENERGY_MULTI_EXECUTE_TOOL",
                {"calls": [{"tool": tool, "arguments": arguments, **options}]},
            )
            return response["results"][0]

        args = {
            "file": "large.csv",
            "kind": "metered",
            "unit": "kWh",
            "timezone": "UTC",
            "quantity_shape": "interval",
            "resolution": "30min",
        }
        old = await call("CSV_READ_TIMESERIES", args)
        assert not old["ok"] and old["error"]["code"] == "input_too_large"
        imported = await call("DATASET_IMPORT_CSV", args, asset_id="meter")
        assert imported["ok"], imported
        ref = imported["result"]["data"]["dataset_id"]
        stored = energy.agent.workbench.read(session.context, ref)
        assert (
            stored.kind == DataKind.METERED
            and stored.site_id == "home"
            and stored.asset_id == "meter"
        )
        assert stored.data["rows"] == 100_001
        assert ref in {
            item["artifact_id"] for item in energy.agent.workbench.list_artifacts(session.context)
        }
        first_page = await call("DATASET_PAGE", {"artifact_id": ref})
        first_data = first_page["result"]["data"]
        assert first_data["rows"] and first_data["returned_rows"] < 1000
        assert first_data["next_offset"] == first_data["returned_rows"]
        projected = await call("DATASET_PAGE", {"artifact_id": ref, "columns": ["value"]})
        assert projected["ok"] and all(
            set(row) == {"value"} for row in projected["result"]["data"]["rows"]
        )
        denied_asset = await call("DATASET_IMPORT_CSV", args, asset_id="unknown-asset")
        assert not denied_asset["ok"] and denied_asset["error"]["code"] == "asset_forbidden"
        summary = await call(
            "DATASET_SUMMARIZE", {"artifact_id": ref, "column": "value"}, input_artifacts=[ref]
        )
        assert summary["ok"], summary
        assert summary["result"]["kind"] == "calculated"
        assert summary["result"]["data"]["sum"] == 200_002
        assert summary["result"]["data"]["count"] == 100_001
        assert summary["result"]["data"]["mean"] == pytest.approx(2)
        assert ref in str(summary["result"]["provenance"])
        page = await call("DATASET_PAGE", {"artifact_id": ref, "offset": 99_999, "limit": 2})
        assert page["ok"] and len(page["result"]["data"]["rows"]) == 2
        assert page["result"]["data"]["next_offset"] is None
        window = await call(
            "WORKBENCH_WINDOW",
            {
                "artifact_id": ref,
                "start": start.isoformat(),
                "end": (start + timedelta(hours=2)).isoformat(),
                "timestamp": "timestamp",
            },
            input_artifacts=[ref],
        )
        assert window["ok"], window
        assert window["result"]["kind"] == "metered"
        assert len(window["result"]["data"]) == 4
        foreign = await energy.session("other").execute("DATASET_PAGE", {"artifact_id": ref})
        assert not foreign["ok"] and foreign["error"]["code"] == "artifact_not_found"
        other_session = await energy.session("owner", "home").execute(
            "DATASET_SUMMARIZE", {"artifact_id": ref, "column": "value"}
        )
        assert not other_session["ok"] and other_session["error"]["code"] == "artifact_not_found"
        invalid = await call("DATASET_PAGE", {"artifact_id": ref, "limit": 10001})
        assert not invalid["ok"] and invalid["error"]["code"] == "invalid_arguments"
        archive = tmp_path / "backup.tar.gz"
        manifest = create_backup(state, archive)
        assert {f.path for f in manifest.files} == {"artifacts.sqlite3"}
        restored = tmp_path / "restored"
        restore_backup(archive, restored)
        restarted = Workbench(restored)
        assert (
            restarted.partitioned.read_page(session.context, ref, offset=99_999, limit=2)[
                "total_rows"
            ]
            == 100_001
        )
        restored_result = restarted.read(session.context, ref)
        assert restored_result.asset_id == "meter" and restored_result.site_id == "home"
        restarted.delete(session.context, ref)
        with pytest.raises(EnergyError, match="does not exist"):
            restarted.read(session.context, ref)


def test_small_artifacts_and_datasets_share_quota_both_directions(tmp_path):
    session = Session(user_id="owner")
    bench = Workbench(tmp_path / "a", user_quota_bytes=4000, global_quota_bytes=4000)
    metadata = EnergyResult(data=[], kind=DataKind.METERED, unit="kWh", source="fixture")
    plain = metadata.model_copy(update={"data": [{"text": "x" * 2800}]})
    bench.persist(session, plain)
    with pytest.raises(EnergyError, match="operator quota"):
        bench.partitioned.ingest(session, metadata, ({"text": "x" * 800} for _ in range(2)))
    with bench.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
    reverse = Workbench(tmp_path / "b", user_quota_bytes=4000, global_quota_bytes=4000)
    reverse.partitioned.ingest(session, metadata, [{"text": "x" * 1600}])
    with pytest.raises(EnergyError, match="operator quota"):
        reverse.persist(session, plain)


@pytest.mark.parametrize("shape,unit", [("counter", "kWh"), ("instantaneous", "kW")])
async def test_dataset_summary_does_not_sum_power_or_counters(tmp_path, shape, unit):
    data = tmp_path / "data"
    data.mkdir()
    (data / "values.csv").write_text("value\n1\n2\n3\n")
    async with EnergyAgentTools(tmp_path / "state", data_root=data) as energy:
        session = energy.session("owner")
        saved = await session.execute(
            "DATASET_IMPORT_CSV",
            {
                "file": "values.csv",
                "kind": "metered",
                "unit": unit,
                "timezone": "UTC",
                "quantity_shape": shape,
            },
        )
        ref = saved["result"]["data"]["dataset_id"]
        result = await session.execute("DATASET_SUMMARIZE", {"artifact_id": ref, "column": "value"})
        assert result["ok"], result
        assert result["result"]["data"]["sum"] is None
        assert result["result"]["data"]["mean"] == pytest.approx(2)


async def test_streamed_summary_keeps_small_energy_terms_in_large_net_flows(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "net.csv").write_text("value\n10000000000000000\n1\n1\n-10000000000000000\n")
    async with EnergyAgentTools(tmp_path / "state", data_root=data) as energy:
        session = energy.session("owner")
        imported = await session.execute(
            "DATASET_IMPORT_CSV",
            {
                "file": "net.csv",
                "kind": "metered",
                "unit": "Wh",
                "timezone": "UTC",
                "quantity_shape": "interval",
            },
        )
        ref = imported["result"]["data"]["dataset_id"]
        result = await session.execute("DATASET_SUMMARIZE", {"artifact_id": ref, "column": "value"})
        assert result["ok"], result
        assert result["result"]["data"]["sum"] == 2
        assert result["result"]["data"]["mean"] == 0.5
