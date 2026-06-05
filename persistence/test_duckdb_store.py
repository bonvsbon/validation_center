"""DuckDBStore durability test: results written by one instance survive a close +
reopen (i.e. a process restart) — the point of a LOCAL on-disk store.

Run:  python -m persistence.test_duckdb_store   (or pytest)
"""
from __future__ import annotations
import os
import tempfile

from persistence import DuckDBStore, ReconRunRecord, new_id
from orchestrator.resolver import resolve, load_json
from orchestrator.engine import run as engine_run

FX = os.path.join(os.path.dirname(__file__), "..", "orchestrator", "fixtures")


def _produce_result():
    cfg = resolve(load_json(os.path.join(FX, "template_ncb.json")),
                  load_json(os.path.join(FX, "mapping_ncb.json")),
                  load_json(os.path.join(FX, "code_lists.json")), as_of_date="2026-06-04")
    return engine_run(cfg, os.path.join(FX, "source.csv"), os.path.join(FX, "dest.csv"))


def test_persist_and_reopen():
    path = os.path.join(tempfile.mkdtemp(), "vc_test.duckdb")
    res = _produce_result()

    # write with one instance, then close (simulate shutdown)
    store = DuckDBStore(path)
    meta = ReconRunRecord(run_id=new_id(), template_version_id="tv1", mapping_id="m1",
                          source_dataset_id="s", dest_dataset_id="d",
                          rule_engine_version=res.rule_engine_version, as_of_date="2026-06-04")
    run_id = store.create_run(meta)
    store.mark_running(run_id)
    store.save_results(run_id, res.field_results, res.record_rollup)
    store.mark_completed(run_id, res.summary)
    store.close()

    # reopen a fresh instance on the same file -> data must be there
    store2 = DuckDBStore(path)
    rec = store2.get_run(run_id)
    assert rec is not None and rec.status == "COMPLETED"
    assert rec.summary == {"total_records": 7, "match_count": 2, "mismatch_count": 1,
                           "missing_src": 1, "missing_dst": 1, "duplicate_count": 1,
                           "exception_count": 1}
    total, rows = store2.get_results(run_id, category="MISMATCH")
    assert total == 2
    rollup = store2.get_rollup(run_id)
    assert dict(rollup)["A007|1100000000007"] == "EXCEPTION"
    store2.close()


if __name__ == "__main__":
    test_persist_and_reopen()
    print("DuckDBStore durability: PASS")
