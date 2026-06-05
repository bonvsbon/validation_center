"""Run-history test: multiple reconciliation runs are listed (newest first) with
their status + summary, and each run's results remain retrievable. Backs the
frontend "History" panel.

Run:  python -m api.test_api_history   (or pytest)
"""
from __future__ import annotations
import os
import tempfile

from fastapi.testclient import TestClient

from api.main import create_app
from persistence import DuckDBStore

EXP = (7, 2, 1, 1, 1, 1, 1)


def _client():
    # DuckDB-backed so the history is durable, like the real local server
    path = os.path.join(tempfile.mkdtemp(), "hist.duckdb")
    return TestClient(create_app(store=DuckDBStore(path)))


def _one_run(c):
    boot = c.post("/api/v1/demo/bootstrap").json()
    mid = c.post(f"/api/v1/mappings/suggestions/{boot['suggestion_id']}/approve",
                 json={"reject_edge_ids": []}).json()["mapping_id"]
    run = c.post("/api/v1/recon/runs", json={
        "template_id": boot["template_id"], "mapping_id": mid,
        "code_lists_id": boot["code_lists_id"], "source_dataset_id": boot["source_dataset_id"],
        "dest_dataset_id": boot["dest_dataset_id"], "as_of_date": boot["as_of_date"]}).json()
    return run["run_id"]


def test_history_lists_runs_with_summaries():
    c = _client()
    r1 = _one_run(c)
    r2 = _one_run(c)

    runs = c.get("/api/v1/recon/runs").json()
    ids = [r["run_id"] for r in runs]
    assert r1 in ids and r2 in ids and len(runs) >= 2
    for r in runs:
        assert r["status"] == "COMPLETED"
        assert tuple(r["summary"][k] for k in ["total_records", "match_count", "mismatch_count",
                     "missing_src", "missing_dst", "duplicate_count", "exception_count"]) == EXP

    # each run's results still retrievable
    res = c.get(f"/api/v1/recon/runs/{r1}/results", params={"category": "EXCEPTION"}).json()
    assert res["total"] == 1


if __name__ == "__main__":
    c = _client()
    a, b = _one_run(c), _one_run(c)
    runs = c.get("/api/v1/recon/runs").json()
    print("runs in history:", len(runs))
    for r in runs:
        print("  ", r["run_id"][:8], r["status"], r["summary"]["match_count"], "match")
    ok = len(runs) >= 2 and all(r["status"] == "COMPLETED" for r in runs)
    print("RESULT         :", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
