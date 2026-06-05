"""Async run test: trigger a background reconciliation, then poll the run until it
reaches COMPLETED. Also verifies the DuckDBStore-backed path end-to-end.

Run:  python -m api.test_api_async   (or pytest)
"""
from __future__ import annotations
import os
import tempfile
import time

from fastapi.testclient import TestClient

from api.main import create_app
from persistence import DuckDBStore

EXP = (7, 2, 1, 1, 1, 1, 1)


def _client_with_local_db():
    path = os.path.join(tempfile.mkdtemp(), "async.duckdb")
    return TestClient(create_app(store=DuckDBStore(path))), path


def _bootstrap_ids(c):
    return c.post("/api/v1/demo/bootstrap").json()


def _approve(c, boot):
    return c.post(f"/api/v1/mappings/suggestions/{boot['suggestion_id']}/approve",
                  json={"reject_edge_ids": []}).json()["mapping_id"]


def test_background_run_completes_and_persists():
    c, _ = _client_with_local_db()
    boot = _bootstrap_ids(c)
    mid = _approve(c, boot)
    body = {"template_id": boot["template_id"], "mapping_id": mid,
            "code_lists_id": boot["code_lists_id"],
            "source_dataset_id": boot["source_dataset_id"],
            "dest_dataset_id": boot["dest_dataset_id"],
            "as_of_date": boot["as_of_date"], "background": True}

    started = c.post("/api/v1/recon/runs", json=body).json()
    assert started["status"] in ("QUEUED", "RUNNING", "COMPLETED")
    run_id = started["run_id"]

    # poll
    summary = None
    for _ in range(50):
        r = c.get(f"/api/v1/recon/runs/{run_id}").json()
        if r["status"] in ("COMPLETED", "FAILED"):
            summary = r.get("summary"); status = r["status"]; break
        time.sleep(0.05)
    assert status == "COMPLETED", "background run did not complete"
    assert tuple(summary[k] for k in ["total_records", "match_count", "mismatch_count",
                 "missing_src", "missing_dst", "duplicate_count", "exception_count"]) == EXP

    # results persisted in the local DuckDB store
    res = c.get(f"/api/v1/recon/runs/{run_id}/results", params={"category": "EXCEPTION"}).json()
    assert res["total"] == 1


if __name__ == "__main__":
    c, path = _client_with_local_db()
    boot = _bootstrap_ids(c); mid = _approve(c, boot)
    body = {"template_id": boot["template_id"], "mapping_id": mid,
            "code_lists_id": boot["code_lists_id"], "source_dataset_id": boot["source_dataset_id"],
            "dest_dataset_id": boot["dest_dataset_id"], "as_of_date": boot["as_of_date"],
            "background": True}
    started = c.post("/api/v1/recon/runs", json=body).json()
    print("submitted   :", started["status"], started["run_id"])
    status, summary = None, None
    for _ in range(50):
        r = c.get(f"/api/v1/recon/runs/{started['run_id']}").json()
        status = r["status"]
        if status in ("COMPLETED", "FAILED"):
            summary = r.get("summary"); break
        time.sleep(0.05)
    got = tuple((summary or {}).get(k, -1) for k in ["total_records", "match_count",
                "mismatch_count", "missing_src", "missing_dst", "duplicate_count", "exception_count"])
    print("polled      :", status, got)
    print("db file      :", os.path.basename(path))
    print("RESULT      :", "PASS" if status == "COMPLETED" and got == EXP else "FAIL")
    raise SystemExit(0 if got == EXP else 1)
