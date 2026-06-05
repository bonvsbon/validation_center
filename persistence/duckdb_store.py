"""DuckDBStore — durable LOCAL persistence (a single .duckdb file, no server).

Implements the same ReconStore interface as InMemoryStore / PostgresStore, so the
API and worker are storage-agnostic. This is the recommended local default: zero
server to run, results survive process restarts, and it reuses the DuckDB engine
already in the stack.

Concurrency: one connection guarded by a lock (single-process app + background
worker). For multi-process deployments use PostgresStore.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import json
import os
import threading

import duckdb


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_iso(v) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)

from .store import (ReconStore, ReconRunRecord, by_field_counts, by_severity_counts,
                    _summary_from_rollup)

SUMMARY_COLS = ["total_records", "match_count", "mismatch_count",
                "missing_src", "missing_dst", "duplicate_count", "exception_count"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recon_runs (
    run_id VARCHAR PRIMARY KEY, tenant_id VARCHAR, template_version_id VARCHAR,
    mapping_id VARCHAR, source_dataset_id VARCHAR, dest_dataset_id VARCHAR,
    status VARCHAR, rule_engine_version VARCHAR, as_of_date VARCHAR,
    error_message VARCHAR, queued_at VARCHAR, started_at VARCHAR, finished_at VARCHAR
);
CREATE TABLE IF NOT EXISTS recon_summary (
    run_id VARCHAR PRIMARY KEY, total_records BIGINT, match_count BIGINT,
    mismatch_count BIGINT, missing_src BIGINT, missing_dst BIGINT,
    duplicate_count BIGINT, exception_count BIGINT, by_severity VARCHAR, by_field VARCHAR
);
CREATE TABLE IF NOT EXISTS recon_results (
    run_id VARCHAR, record_key VARCHAR, category VARCHAR, field_key VARCHAR,
    rule_id VARCHAR, severity VARCHAR, expected VARCHAR, actual VARCHAR,
    verdict VARCHAR, detail VARCHAR
);
CREATE TABLE IF NOT EXISTS artifacts (
    kind VARCHAR, id VARCHAR, body VARCHAR, created_at VARCHAR,
    PRIMARY KEY (kind, id)
);
"""


class DuckDBStore(ReconStore):
    def __init__(self, path: str = "./data/vc.duckdb") -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._con = duckdb.connect(path)
        self._con.execute(_SCHEMA)

    # ---- runs ----
    def create_run(self, meta: ReconRunRecord) -> str:
        with self._lock:
            self._con.execute(
                """INSERT INTO recon_runs (run_id, tenant_id, template_version_id,
                     mapping_id, source_dataset_id, dest_dataset_id, status,
                     rule_engine_version, as_of_date, queued_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [meta.run_id, meta.tenant_id, meta.template_version_id, meta.mapping_id,
                 meta.source_dataset_id, meta.dest_dataset_id, meta.status,
                 meta.rule_engine_version, meta.as_of_date, _as_iso(meta.queued_at)])
        return meta.run_id

    def mark_running(self, run_id: str) -> None:
        self._exec("UPDATE recon_runs SET status='RUNNING', started_at=? WHERE run_id=?", [_iso(), run_id])

    def mark_completed(self, run_id: str, summary: Dict[str, int]) -> None:
        self._exec("UPDATE recon_runs SET status='COMPLETED', finished_at=? WHERE run_id=?", [_iso(), run_id])

    def mark_failed(self, run_id: str, error: str) -> None:
        self._exec("UPDATE recon_runs SET status='FAILED', finished_at=?, error_message=? WHERE run_id=?",
                   [_iso(), error, run_id])

    def save_results(self, run_id: str, field_results: List[Dict[str, Any]],
                     rollup: List[Tuple[str, str]]) -> None:
        with self._lock:
            c = self._con
            c.execute("DELETE FROM recon_results WHERE run_id=?", [run_id])
            c.execute("DELETE FROM recon_summary WHERE run_id=?", [run_id])
            field_rows = [[run_id, r["record_key"], r["category"], r.get("field_key"),
                           r.get("rule_id"), r.get("severity"), r.get("expected"),
                           r.get("actual"), r.get("verdict"),
                           json.dumps(r.get("detail") or {}, ensure_ascii=False)]
                          for r in field_results]
            rollup_rows = [[run_id, k, cat, None, None, None, None, None, None, None]
                           for k, cat in rollup]
            allrows = field_rows + rollup_rows
            if allrows:
                c.executemany(
                    "INSERT INTO recon_results VALUES (?,?,?,?,?,?,?,?,?,?)", allrows)
            s = _summary_from_rollup(rollup)
            c.execute(
                "INSERT INTO recon_summary VALUES (?,?,?,?,?,?,?,?,?,?)",
                [run_id, s["total_records"], s["match_count"], s["mismatch_count"],
                 s["missing_src"], s["missing_dst"], s["duplicate_count"], s["exception_count"],
                 json.dumps(by_severity_counts(field_results), ensure_ascii=False),
                 json.dumps(by_field_counts(field_results), ensure_ascii=False)])

    def get_run(self, run_id: str) -> Optional[ReconRunRecord]:
        with self._lock:
            row = self._con.execute(
                """SELECT run_id, tenant_id, template_version_id, mapping_id,
                          source_dataset_id, dest_dataset_id, status, rule_engine_version,
                          as_of_date, error_message, queued_at, started_at, finished_at
                   FROM recon_runs WHERE run_id=?""", [run_id]).fetchone()
            if not row:
                return None
            rec = ReconRunRecord(
                run_id=row[0], tenant_id=row[1], template_version_id=row[2], mapping_id=row[3],
                source_dataset_id=row[4], dest_dataset_id=row[5], status=row[6],
                rule_engine_version=row[7], as_of_date=row[8], error_message=row[9],
                queued_at=row[10], started_at=row[11], finished_at=row[12])
            s = self._con.execute(
                f"SELECT {', '.join(SUMMARY_COLS)} FROM recon_summary WHERE run_id=?",
                [run_id]).fetchone()
            if s:
                rec.summary = dict(zip(SUMMARY_COLS, [int(x) for x in s]))
            return rec

    def list_runs(self) -> List[ReconRunRecord]:
        with self._lock:
            ids = [r[0] for r in self._con.execute(
                "SELECT run_id FROM recon_runs ORDER BY queued_at DESC LIMIT 200").fetchall()]
        return [self.get_run(i) for i in ids]

    def get_results(self, run_id: str, category: Optional[str] = None,
                    field_key: Optional[str] = None, offset: int = 0,
                    limit: int = 50) -> Tuple[int, List[Dict[str, Any]]]:
        clauses = ["run_id=?", "field_key IS NOT NULL"]
        params: List[Any] = [run_id]
        if category:
            clauses.append("category=?"); params.append(category)
        if field_key:
            clauses.append("field_key=?"); params.append(field_key)
        where = " AND ".join(clauses)
        with self._lock:
            total = int(self._con.execute(
                f"SELECT count(*) FROM recon_results WHERE {where}", params).fetchone()[0])
            rows = self._con.execute(
                f"""SELECT record_key, field_key, rule_id, severity, expected, actual,
                           category, verdict, detail FROM recon_results WHERE {where}
                    ORDER BY record_key, field_key OFFSET ? LIMIT ?""",
                params + [offset, limit]).fetchall()
        cols = ["record_key", "field_key", "rule_id", "severity", "expected",
                "actual", "category", "verdict", "detail"]
        return total, [dict(zip(cols, r)) for r in rows]

    def get_rollup(self, run_id: str) -> List[Tuple[str, str]]:
        with self._lock:
            rows = self._con.execute(
                """SELECT record_key, category FROM recon_results
                   WHERE run_id=? AND field_key IS NULL ORDER BY record_key""",
                [run_id]).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ---- artifacts ----
    def put_artifact(self, kind: str, artifact_id: str, body: Dict[str, Any]) -> None:
        with self._lock:
            self._con.execute("DELETE FROM artifacts WHERE kind=? AND id=?", [kind, artifact_id])
            self._con.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?)",
                [kind, artifact_id, json.dumps(body, ensure_ascii=False), _iso()])

    def get_artifact(self, kind: str, artifact_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._con.execute(
                "SELECT body FROM artifacts WHERE kind=? AND id=?", [kind, artifact_id]).fetchone()
        return json.loads(row[0]) if row else None

    def list_artifacts(self, kind: str) -> List[Tuple[str, Dict[str, Any]]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT id, body FROM artifacts WHERE kind=?", [kind]).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def _exec(self, sql: str, params) -> None:
        with self._lock:
            self._con.execute(sql, params)
