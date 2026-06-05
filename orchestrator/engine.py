"""DuckDB implementation of the CompareEngine interface.

run() generates the SQL from the resolved config, executes it on an in-memory
DuckDB connection, and returns the deterministic result set. Optionally persists
field_results + summary to Parquet (a stand-in for the Postgres recon_results /
recon_summary sink described in rule-engine-spec.md §6).
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
import os

import duckdb

from .resolver import ResolvedConfig
from .sql_builder import build_setup_sql
from .quoting import lit

SUMMARY_COLS = [
    "total_records", "match_count", "mismatch_count",
    "missing_src", "missing_dst", "duplicate_count", "exception_count",
]


@dataclass
class RunResult:
    rule_engine_version: str
    as_of_date: str
    summary: Dict[str, int]
    record_rollup: List[Tuple[str, str]]          # (record_key, category)
    field_results: List[Dict[str, Any]]           # drill-down rows
    generated_sql: str

    def summary_tuple(self) -> Tuple[int, ...]:
        return tuple(self.summary[c] for c in SUMMARY_COLS)


def run(cfg: ResolvedConfig, src_csv: str, dst_csv: str,
        out_dir: Optional[str] = None, keep_sql: bool = True) -> RunResult:
    sql = build_setup_sql(cfg, src_csv, dst_csv)
    con = duckdb.connect()
    con.execute(sql)

    srow = con.execute("SELECT * FROM run_summary").fetchone()
    summary = dict(zip(SUMMARY_COLS, [int(x) for x in srow]))

    rollup = [(r[0], r[1]) for r in
              con.execute("SELECT record_key, category FROM record_rollup "
                          "ORDER BY record_key").fetchall()]

    fr_cols = ["record_key", "field_key", "rule_id", "severity",
               "expected", "actual", "category", "verdict"]
    field_rows = [dict(zip(fr_cols, r)) for r in
                  con.execute(f"SELECT {', '.join(fr_cols)} FROM field_results "
                              "ORDER BY record_key, field_key").fetchall()]

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        fr_path = os.path.join(out_dir, "field_results.parquet").replace("\\", "/")
        rr_path = os.path.join(out_dir, "record_rollup.parquet").replace("\\", "/")
        con.execute(f"COPY field_results TO {lit(fr_path)} (FORMAT PARQUET);")
        con.execute(f"COPY record_rollup TO {lit(rr_path)} (FORMAT PARQUET);")
        # NOTE: production sink writes these to Postgres recon_results (partitioned)
        # and recon_summary, stamped with rule_engine_version + the run id.

    con.close()
    return RunResult(
        rule_engine_version=cfg.rule_engine_version,
        as_of_date=cfg.as_of_date,
        summary=summary,
        record_rollup=rollup,
        field_results=field_rows,
        generated_sql=sql if keep_sql else "",
    )
