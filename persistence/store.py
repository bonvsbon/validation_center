"""Reconciliation result store.

Two implementations behind one interface:
  * InMemoryStore  — used by the API and tests (fully runnable here).
  * PostgresStore  — production target; writes recon_runs / recon_results
                     (partitioned) / recon_summary per db/schema.sql.

The orchestrator's RunResult is persisted as:
  * recon_summary       : one row of headline counts (+ by_severity / by_field).
  * recon_results       : field-level rows (field_key set) AND record-level
                          rollup rows (field_key NULL, category = record category).
Every run row stamps rule_engine_version + as_of_date for reproducibility.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class ReconRunRecord:
    run_id: str
    template_version_id: str
    mapping_id: str
    source_dataset_id: Optional[str]
    dest_dataset_id: Optional[str]
    rule_engine_version: str
    as_of_date: str
    status: str = "QUEUED"
    tenant_id: Optional[str] = None
    error_message: Optional[str] = None
    queued_at: datetime = field(default_factory=_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary: Optional[Dict[str, int]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("queued_at", "started_at", "finished_at"):
            v = d[k]
            if v is not None and hasattr(v, "isoformat"):
                d[k] = v.isoformat()   # datetime -> str; already-str passes through
        return d


def by_field_counts(field_results: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in field_results:
        if r["category"] in ("MISMATCH", "EXCEPTION"):
            out[r["field_key"]] = out.get(r["field_key"], 0) + 1
    return out


def by_severity_counts(field_results: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in field_results:
        if r["category"] in ("MISMATCH", "EXCEPTION") and r.get("severity"):
            out[r["severity"]] = out.get(r["severity"], 0) + 1
    return out


# =====================================================================
class ReconStore:
    """Interface."""

    def create_run(self, meta: ReconRunRecord) -> str: ...
    def mark_running(self, run_id: str) -> None: ...
    def mark_completed(self, run_id: str, summary: Dict[str, int]) -> None: ...
    def mark_failed(self, run_id: str, error: str) -> None: ...
    def save_results(self, run_id: str,
                     field_results: List[Dict[str, Any]],
                     rollup: List[Tuple[str, str]]) -> None: ...
    def get_run(self, run_id: str) -> Optional[ReconRunRecord]: ...
    def list_runs(self) -> List[ReconRunRecord]: ...
    def get_results(self, run_id: str, category: Optional[str] = None,
                    field_key: Optional[str] = None,
                    offset: int = 0, limit: int = 50) -> Tuple[int, List[Dict[str, Any]]]: ...
    def get_rollup(self, run_id: str) -> List[Tuple[str, str]]: ...


# =====================================================================
class InMemoryStore(ReconStore):
    def __init__(self) -> None:
        self._runs: Dict[str, ReconRunRecord] = {}
        self._results: Dict[str, List[Dict[str, Any]]] = {}
        self._rollup: Dict[str, List[Tuple[str, str]]] = {}

    def create_run(self, meta: ReconRunRecord) -> str:
        self._runs[meta.run_id] = meta
        return meta.run_id

    def mark_running(self, run_id: str) -> None:
        r = self._runs[run_id]; r.status = "RUNNING"; r.started_at = _now()

    def mark_completed(self, run_id: str, summary: Dict[str, int]) -> None:
        r = self._runs[run_id]; r.status = "COMPLETED"; r.finished_at = _now(); r.summary = summary

    def mark_failed(self, run_id: str, error: str) -> None:
        r = self._runs[run_id]; r.status = "FAILED"; r.finished_at = _now(); r.error_message = error

    def save_results(self, run_id, field_results, rollup) -> None:
        self._results[run_id] = list(field_results)
        self._rollup[run_id] = list(rollup)

    def get_run(self, run_id):
        return self._runs.get(run_id)

    def list_runs(self):
        return sorted(self._runs.values(), key=lambda r: r.queued_at, reverse=True)

    def get_results(self, run_id, category=None, field_key=None, offset=0, limit=50):
        rows = self._results.get(run_id, [])
        if category:
            rows = [r for r in rows if r["category"] == category]
        if field_key:
            rows = [r for r in rows if r["field_key"] == field_key]
        return len(rows), rows[offset:offset + limit]

    def get_rollup(self, run_id):
        return self._rollup.get(run_id, [])


# =====================================================================
class PostgresStore(ReconStore):
    """Production store. Lazily imports psycopg so the module loads without it.

    Requires the schema from db/schema.sql to be applied first.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _conn(self):
        import psycopg  # lazy
        return psycopg.connect(self._dsn)

    def create_run(self, meta: ReconRunRecord) -> str:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO recon_runs
                   (id, tenant_id, template_version_id, mapping_id,
                    source_dataset_id, dest_dataset_id, status,
                    rule_engine_version, params, queued_at)
                   VALUES (%s,%s,%s,%s,%s,%s,'QUEUED',%s,%s, now())""",
                (meta.run_id, meta.tenant_id, meta.template_version_id, meta.mapping_id,
                 meta.source_dataset_id, meta.dest_dataset_id, meta.rule_engine_version,
                 _json({"as_of_date": meta.as_of_date})))
        return meta.run_id

    def mark_running(self, run_id: str) -> None:
        self._exec("UPDATE recon_runs SET status='RUNNING', started_at=now() WHERE id=%s", (run_id,))

    def mark_completed(self, run_id: str, summary: Dict[str, int]) -> None:
        self._exec("UPDATE recon_runs SET status='COMPLETED', finished_at=now() WHERE id=%s", (run_id,))

    def mark_failed(self, run_id: str, error: str) -> None:
        self._exec("UPDATE recon_runs SET status='FAILED', finished_at=now(), error_message=%s WHERE id=%s",
                   (error, run_id))

    def save_results(self, run_id, field_results, rollup) -> None:
        with self._conn() as c, c.cursor() as cur:
            # 1) per-run partition (cheap drop / prune)
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS recon_results_{_safe(run_id)} '
                f"PARTITION OF recon_results FOR VALUES IN (%s)", (run_id,))
            # 2) field-level rows
            cur.executemany(
                """INSERT INTO recon_results
                   (run_id, record_key, category, field_key, rule_id, severity,
                    expected, actual, verdict, detail)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(run_id, r["record_key"], r["category"], r["field_key"],
                  _uuid_or_none(r.get("rule_id")), r.get("severity"),
                  r.get("expected"), r.get("actual"), r.get("verdict"),
                  _json(r.get("detail") or {})) for r in field_results])
            # 3) record-level rollup rows (field_key NULL)
            cur.executemany(
                """INSERT INTO recon_results
                   (run_id, record_key, category, field_key)
                   VALUES (%s,%s,%s, NULL)""",
                [(run_id, k, cat) for k, cat in rollup])
            # 4) summary
            s = _summary_from_rollup(rollup)
            s["by_field"] = by_field_counts(field_results)
            s["by_severity"] = by_severity_counts(field_results)
            cur.execute(
                """INSERT INTO recon_summary
                   (run_id, total_records, match_count, mismatch_count,
                    missing_src, missing_dst, duplicate_count, exception_count,
                    by_severity, by_field)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id) DO UPDATE SET
                     total_records=EXCLUDED.total_records,
                     match_count=EXCLUDED.match_count,
                     mismatch_count=EXCLUDED.mismatch_count,
                     missing_src=EXCLUDED.missing_src,
                     missing_dst=EXCLUDED.missing_dst,
                     duplicate_count=EXCLUDED.duplicate_count,
                     exception_count=EXCLUDED.exception_count,
                     by_severity=EXCLUDED.by_severity, by_field=EXCLUDED.by_field""",
                (run_id, s["total_records"], s["match_count"], s["mismatch_count"],
                 s["missing_src"], s["missing_dst"], s["duplicate_count"],
                 s["exception_count"], _json(s["by_severity"]), _json(s["by_field"])))

    def get_run(self, run_id):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT id, template_version_id, mapping_id, source_dataset_id,
                                  dest_dataset_id, rule_engine_version, status, params,
                                  queued_at, started_at, finished_at FROM recon_runs WHERE id=%s""",
                        (run_id,))
            row = cur.fetchone()
            if not row:
                return None
            rec = ReconRunRecord(
                run_id=str(row[0]), template_version_id=str(row[1]), mapping_id=str(row[2]),
                source_dataset_id=row[3] and str(row[3]), dest_dataset_id=row[4] and str(row[4]),
                rule_engine_version=row[5], status=row[6],
                as_of_date=(row[7] or {}).get("as_of_date", ""))
            cur.execute("""SELECT total_records, match_count, mismatch_count, missing_src,
                                  missing_dst, duplicate_count, exception_count
                           FROM recon_summary WHERE run_id=%s""", (run_id,))
            s = cur.fetchone()
            if s:
                rec.summary = dict(zip(
                    ["total_records", "match_count", "mismatch_count", "missing_src",
                     "missing_dst", "duplicate_count", "exception_count"], [int(x) for x in s]))
            return rec

    def list_runs(self):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT id FROM recon_runs ORDER BY queued_at DESC LIMIT 200")
            return [self.get_run(str(r[0])) for r in cur.fetchall()]

    def get_results(self, run_id, category=None, field_key=None, offset=0, limit=50):
        clauses = ["run_id=%s", "field_key IS NOT NULL"]
        params: List[Any] = [run_id]
        if category:
            clauses.append("category=%s"); params.append(category)
        if field_key:
            clauses.append("field_key=%s"); params.append(field_key)
        where = " AND ".join(clauses)
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM recon_results WHERE {where}", params)
            total = int(cur.fetchone()[0])
            cur.execute(
                f"""SELECT record_key, field_key, rule_id, severity, expected, actual,
                           category, verdict, detail
                    FROM recon_results WHERE {where}
                    ORDER BY record_key, field_key OFFSET %s LIMIT %s""",
                params + [offset, limit])
            cols = ["record_key", "field_key", "rule_id", "severity", "expected",
                    "actual", "category", "verdict", "detail"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            return total, rows

    def get_rollup(self, run_id):
        with self._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT record_key, category FROM recon_results
                           WHERE run_id=%s AND field_key IS NULL ORDER BY record_key""", (run_id,))
            return [(r[0], r[1]) for r in cur.fetchall()]

    # helpers
    def _exec(self, sql: str, params) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(sql, params)


# ---- module helpers ----
def _summary_from_rollup(rollup: List[Tuple[str, str]]) -> Dict[str, int]:
    cats = [c for _, c in rollup]
    return {
        "total_records": len(cats),
        "match_count": cats.count("MATCH"),
        "mismatch_count": cats.count("MISMATCH"),
        "missing_src": cats.count("MISSING_IN_SOURCE"),
        "missing_dst": cats.count("MISSING_IN_DEST"),
        "duplicate_count": cats.count("DUPLICATE"),
        "exception_count": cats.count("EXCEPTION"),
    }


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def _safe(run_id: str) -> str:
    return run_id.replace("-", "")


def _uuid_or_none(v):
    return v if v else None
