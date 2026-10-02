# Large time-series datasets

Approved CSV files can be imported as private partitioned datasets rather than
whole JSON artifacts. Four tools are available when the operator configures a
`data_root`:

| Tool | Behavior |
| --- | --- |
| `DATASET_IMPORT_CSV` | Streams UTF-8 CSV rows into bounded SQLite chunks. Requires explicit kind, unit, timezone and quantity shape. |
| `DATASET_PAGE` | Returns an inline page bounded by rows and bytes, with `next_offset`. Optional `columns` limits fields. |
| `DATASET_SUMMARIZE` | Streams count, missing count, mean and extrema. Sums only explicitly declared interval energy. |
| `DATASET_WINDOW` | Scans chunks and materializes at most 100,000 rows in an explicit time window. |

`WORKBENCH_WINDOW` also handles dataset references, so existing workflows can
select ordinary bounded interval artifacts from a large source. Rows are
selected without prorating boundary intervals, filling gaps or changing their
source kind. Forecasting and other interval workflows still validate cadence,
endpoints, coverage and units after selection.

## Gateway usage

```python
session = energy.session("owner", "site")
imported = await session.execute(
    "DATASET_IMPORT_CSV",
    {
        "file": "site.csv",
        "kind": "metered",
        "unit": "kWh",
        "timezone": "UTC",
        "quantity_shape": "interval",
        "resolution": "1min",
    },
    asset_id="feeder",
)
dataset_id = imported["result"]["data"]["dataset_id"]
summary = await session.execute(
    "DATASET_SUMMARIZE",
    {"artifact_id": dataset_id, "column": "value"},
    input_artifacts=[dataset_id],
)
```

MCP clients call these discovered tools through `ENERGY_MULTI_EXECUTE_TOOL`,
including for one call. Its calls accept a scoped `asset_id`. A dataset ID can
be linked through the normal `input_artifacts` contract. Metadata, original
source kind and lineage remain available without loading all rows.

`limit` is a maximum page row count. The inline byte budget may return fewer
rows; continue at the returned `next_offset`, rather than incrementing by the
requested limit. A single oversized row is refused with `row_too_large`; select
fewer columns. Missing requested columns are refused. Page reads preserve
insertion order; they do not imply temporal completeness.

A reviewed `get_energy_consumption` binding can point at `DATASET_IMPORT_CSV`
with fixed file and semantic arguments. The importer accepts `start`, `end`
and `timestamp` and filters while streaming. The forecast recipe retrieves
bounded historical chunks and selects their intervals through the same gateway
path. It does not guess a source's unit, cadence, interval shape or physical
qualification.

## Persistence and limits

Datasets and small artifacts share the existing `artifacts.sqlite3` database,
user/global quotas and retention policy. Default quotas are 100,000,000 bytes
per user and 1,000,000,000 bytes globally; retention is seven days. Dataset quota
accounting includes serialized metadata and chunk payloads. Failed generators,
invalid CSV rows or quota failures roll back the complete import. User and
session must both match for reads and deletion. Selected site and asset metadata
come from the gateway's scope validation.

SQLite snapshots used by the existing backup/restore tools include dataset
metadata and chunks together with ordinary artifacts. Schema additions are
initialized when dataset storage is first used. Old ordinary artifacts remain
readable. A restarted gateway can read datasets using the restored session
identity; they are not automatically shared with a newly created session.

Chunks are bounded by row count and four MiB encoded bytes. Pages have a smaller
inline budget in the gateway. Window selection and summaries scan stored chunks;
there is no timestamp index or distributed query engine. A window over 100,000
rows must be narrowed. Whole-history high-resolution resampling and concurrent
bulk-write load qualification remain open. Bulk imports use a worker thread,
but SQLite still has a single writer and an import commits atomically.

Run the reproducible offline qualification from the repository root:

```sh
PYTHONPATH="$PWD/src:$PWD" python scripts/qualify_large_timeseries.py
```

It generates one million one-minute interval rows and checks streamed totals
against an independent closed-form expectation, a day window, inline paging,
foreign-scope rejection and backup/restore. Python allocation tracing is bounded
below 32 MiB; that threshold is not an RSS or native SQLite memory claim. All
meter values are synthetic. This qualification does not establish physical-site
access, a concurrent service load target, or a sustained soak.
