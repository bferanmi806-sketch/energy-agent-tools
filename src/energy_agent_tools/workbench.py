"""Private, session-scoped SQLite artifacts and bounded dataframe operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from .models import DataKind, EnergyError, EnergyResult, Json, Session


class Workbench:
    def __init__(
        self,
        root: Path,
        inline_bytes: int = 16000,
        max_bytes: int = 20_000_000,
        user_quota_bytes: int = 100_000_000,
        global_quota_bytes: int = 1_000_000_000,
        retention_seconds: int = 7 * 24 * 3600,
    ):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.path = root / "artifacts.sqlite3"
        self.inline_bytes = inline_bytes
        self.max_bytes = max_bytes
        if min(user_quota_bytes, global_quota_bytes, retention_seconds) <= 0:
            raise ValueError("Artifact quotas and retention must be positive")
        self.user_quota_bytes = user_quota_bytes
        self.global_quota_bytes = global_quota_bytes
        self.retention_seconds = retention_seconds
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS artifacts "
                "(id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, payload TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(artifacts)")}
            if "created_at" not in columns:
                db.execute("ALTER TABLE artifacts ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
                db.execute("UPDATE artifacts SET created_at=? WHERE created_at=0", (time.time(),))
            db.execute("CREATE INDEX IF NOT EXISTS artifact_owner ON artifacts(user_id,session_id)")
        os.chmod(self.path, 0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def persist(self, session: Session, result: EnergyResult) -> Json:
        payload = result.model_dump_json()
        if len(payload.encode()) > self.max_bytes:
            raise EnergyError("output_too_large", "Output exceeds local artifact size limit.")
        artifact_id = uuid4().hex
        created_at = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM artifacts WHERE created_at < ?", (created_at - self.retention_seconds,)
            )
            usage = db.execute(
                "SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM artifacts WHERE user_id=?",
                (session.user_id,),
            ).fetchone()[0]
            total = db.execute(
                "SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM artifacts"
            ).fetchone()[0]
            size = len(payload.encode())
            if usage + size > self.user_quota_bytes or total + size > self.global_quota_bytes:
                raise EnergyError(
                    "artifact_quota_exceeded",
                    "Delete old artifacts or increase the operator quota.",
                )
            db.execute(
                "INSERT INTO artifacts(id,user_id,session_id,payload,created_at) VALUES (?,?,?,?,?)",
                (artifact_id, session.user_id, session.id, payload, created_at),
            )
        rows = result.data if isinstance(result.data, list) else None
        return {
            "artifact_id": artifact_id,
            "expires_at": datetime.fromtimestamp(
                created_at + self.retention_seconds, UTC
            ).isoformat(),
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "rows": len(rows) if rows is not None else None,
            "bytes": len(payload.encode()),
            "preview": rows[:3] if rows is not None else None,
        }

    def compact(self, session: Session, result: EnergyResult) -> Json:
        dumped = result.model_dump(mode="json")
        if len(result.model_dump_json().encode()) <= self.inline_bytes:
            return dumped
        ref = self.persist(session, result)
        dumped["data"] = ref
        # Even a preview may contain very large cells. Keep the envelope bounded.
        if len(json.dumps(dumped).encode()) > self.inline_bytes:
            dumped["data"]["preview"] = None
        if len(json.dumps(dumped).encode()) > self.inline_bytes:
            for field in ("assumptions", "warnings"):
                dumped[field] = [str(v)[:200] for v in dumped[field][:3]]
            dumped["provenance"] = [
                {
                    "artifact_id": ref["artifact_id"],
                    "note": "Full result envelope and provenance retained in artifact.",
                }
            ]
            for field in ("unit", "source", "quality", "resolution"):
                if isinstance(dumped[field], str):
                    dumped[field] = dumped[field][:200]
        return dumped

    def read(self, session: Session, artifact_id: str) -> EnergyResult:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM artifacts WHERE id=? AND user_id=? AND session_id=? AND created_at>=?",
                (artifact_id, session.user_id, session.id, time.time() - self.retention_seconds),
            ).fetchone()
        if row is None:
            raise EnergyError("artifact_not_found", "Artifact does not exist in this session.")
        return EnergyResult.model_validate_json(row[0])

    def delete(self, session: Session, artifact_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM artifacts WHERE id=? AND user_id=? AND session_id=?",
                (artifact_id, session.user_id, session.id),
            )

    def list_artifacts(self, session: Session) -> list[Json]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,length(CAST(payload AS BLOB)),created_at FROM artifacts WHERE user_id=? AND session_id=? AND created_at>=? ORDER BY created_at DESC LIMIT 1000",
                (session.user_id, session.id, time.time() - self.retention_seconds),
            ).fetchall()
        return [
            {
                "artifact_id": row[0],
                "bytes": row[1],
                "expires_at": datetime.fromtimestamp(
                    row[2] + self.retention_seconds, UTC
                ).isoformat(),
            }
            for row in rows
        ]

    def frame(self, session: Session, artifact_id: str) -> tuple[pd.DataFrame, EnergyResult]:
        result = self.read(session, artifact_id)
        if not isinstance(result.data, list) or not result.data:
            raise EnergyError(
                "not_tabular", "Artifact must contain a nonempty list of row objects."
            )
        if not all(isinstance(row, dict) for row in result.data) or len(result.data) > 100_000:
            raise EnergyError("not_tabular", "Artifact requires at most 100000 row objects.")
        return pd.DataFrame(result.data), result

    @staticmethod
    def derived(
        data: Any,
        inputs: list[tuple[str, EnergyResult]],
        unit: str,
        assumptions: list[str] | None = None,
    ) -> EnergyResult:
        return EnergyResult(
            data=data,
            kind=DataKind.CALCULATED,
            unit=unit,
            source="workbench",
            timezone=inputs[0][1].timezone,
            quality="derived",
            assumptions=assumptions or [],
            warnings=list(dict.fromkeys(w for _, r in inputs for w in r.warnings)),
            provenance=[
                {
                    "artifact_id": a,
                    "input_kind": r.kind.value,
                    "source": r.source,
                    "unit": r.unit,
                    "quantity_shape": r.quantity_shape,
                    "provenance": r.provenance,
                }
                for a, r in inputs
            ],
        )

    def summarize(self, session: Session, artifact_id: str, column: str) -> EnergyResult:
        frame, result = self.frame(session, artifact_id)
        if column not in frame:
            raise EnergyError("column_not_found", "Requested column is missing.")
        values = pd.to_numeric(frame[column], errors="coerce")
        data = {
            "count": int(values.count()),
            "missing": int(values.isna().sum()),
            "sum": float(values.sum())
            if values.count()
            and result.unit not in {"W", "kW", "MW"}
            and result.quantity_shape not in {"counter", "instantaneous"}
            else None,
            "mean": float(values.mean()) if values.count() else None,
            "min": float(values.min()) if values.count() else None,
            "max": float(values.max()) if values.count() else None,
        }
        return self.derived(
            data,
            [(artifact_id, result)],
            result.unit,
            [
                "Sum applies to interval quantities. Counters require differences; power requires integration over explicit durations."
            ],
        )

    def resample(
        self,
        session: Session,
        artifact_id: str,
        timestamp: str,
        column: str,
        frequency: str,
        aggregation: str,
    ) -> EnergyResult:
        frame, result = self.frame(session, artifact_id)
        if timestamp not in frame or column not in frame:
            raise EnergyError("column_not_found", "Timestamp or value column is missing.")
        if frequency not in {"15min", "30min", "1h", "1D"} or aggregation not in {"sum", "mean"}:
            raise EnergyError(
                "invalid_operation", "Use supported resampling frequency and sum/mean."
            )
        if aggregation == "sum" and result.quantity_shape in {"counter", "instantaneous"}:
            raise EnergyError(
                "quantity_incompatible",
                "Summation requires interval quantities; transform counters or instantaneous observations explicitly.",
            )
        if aggregation == "sum" and result.unit in {"W", "kW", "MW"}:
            raise EnergyError(
                "unit_incompatible",
                "Integrate power over explicit durations before summing energy.",
            )
        # Explicit UTC offsets avoid silently assigning the wrong DST fold.
        times = pd.to_datetime(frame[timestamp], utc=True, errors="raise")
        if any(pd.Timestamp(t).tzinfo is None for t in frame[timestamp]):
            raise EnergyError("naive_timestamp", "Each timestamp must include its UTC offset.")
        if times.duplicated().any():
            raise EnergyError("duplicate_timestamp", "Resampling requires unique UTC instants.")
        series = pd.Series(pd.to_numeric(frame[column], errors="raise").to_numpy(), index=times)
        series.index = pd.DatetimeIndex(series.index).tz_convert(result.timezone)
        grouped = series.resample(frequency)
        out = grouped.sum(min_count=1) if aggregation == "sum" else grouped.mean()
        rows = [
            {
                "timestamp": pd.Timestamp(str(t)).isoformat(),
                column: None if pd.isna(v) else float(v),
            }
            for t, v in out.items()
        ]
        derived = self.derived(
            rows,
            [(artifact_id, result)],
            result.unit,
            [f"{aggregation} on {frequency} bins in {result.timezone}; empty bins are null."],
        )
        derived.resolution = frequency
        derived.quantity_shape = result.quantity_shape
        return derived

    def join(self, session: Session, left: str, right: str, timestamp: str) -> EnergyResult:
        lf, lr = self.frame(session, left)
        rf, rr = self.frame(session, right)
        if timestamp not in lf or timestamp not in rf:
            raise EnergyError("column_not_found", "Join timestamp column is missing.")
        for f in (lf, rf):
            if any(pd.Timestamp(t).tzinfo is None for t in f[timestamp]):
                raise EnergyError("naive_timestamp", "Join timestamps require UTC offsets.")
            f[timestamp] = pd.to_datetime(f[timestamp], utc=True)
            if f[timestamp].duplicated().any():
                raise EnergyError(
                    "duplicate_timestamp", "Resample duplicated timestamps before joining."
                )
        joined = lf.merge(
            rf, on=timestamp, how="inner", suffixes=("_left", "_right"), validate="one_to_one"
        )
        joined[timestamp] = joined[timestamp].map(lambda t: t.isoformat())
        rows = json.loads(joined.to_json(orient="records"))
        result = self.derived(
            rows,
            [(left, lr), (right, rr)],
            "mixed",
            ["Exact UTC timestamp inner join. Resample first when resolutions differ."],
        )
        if len(joined) < max(len(lf), len(rf)):
            result.warnings.append("Some rows did not match and were omitted.")
        result.provenance.append(
            {
                "columns": {
                    "left": {"unit": lr.unit, "kind": lr.kind.value},
                    "right": {"unit": rr.unit, "kind": rr.kind.value},
                }
            }
        )
        return result

    def pivot(
        self, session: Session, artifact_id: str, timestamp: str, variable: str, value: str
    ) -> EnergyResult:
        frame, result = self.frame(session, artifact_id)
        if any(c not in frame for c in (timestamp, variable, value)):
            raise EnergyError("column_not_found", "Pivot columns are missing.")
        if any(pd.Timestamp(t).tzinfo is None for t in frame[timestamp]):
            raise EnergyError("naive_timestamp", "Pivot timestamps require explicit offsets.")
        frame[timestamp] = pd.to_datetime(frame[timestamp], utc=True, errors="raise").map(
            lambda value: value.isoformat()
        )
        if "unit" in frame and (frame.groupby(variable)["unit"].nunique() > 1).any():
            raise EnergyError(
                "unit_incompatible", "Each pivot variable must have one consistent unit."
            )
        if frame.duplicated([timestamp, variable]).any():
            raise EnergyError(
                "duplicate_timestamp", "Pivot requires one value per timestamp/variable."
            )
        if frame[variable].nunique() > 50:
            raise EnergyError("too_many_columns", "Pivot supports up to 50 variables.")
        wide = frame.pivot(index=timestamp, columns=variable, values=value).reset_index()
        rows = json.loads(wide.to_json(orient="records"))
        derived = self.derived(
            rows,
            [(artifact_id, result)],
            result.unit,
            ["Pivot changes row layout; it does not change physical quantities."],
        )
        if "unit" in frame:
            units = (
                frame.groupby(variable)["unit"].agg(lambda values: sorted(set(values))).to_dict()
            )
            derived.provenance.append({"column_units": units})
        return derived

    def calculate(
        self,
        session: Session,
        artifact_id: str,
        calculation: Callable[[pd.DataFrame], Any],
        unit: str,
    ) -> EnergyResult:
        """Trusted operator Python API. Deliberately unavailable as arbitrary code over MCP."""
        frame, result = self.frame(session, artifact_id)
        return self.derived(
            calculation(frame.copy()),
            [(artifact_id, result)],
            unit,
            ["Calculation supplied by trusted local Python application."],
        )
