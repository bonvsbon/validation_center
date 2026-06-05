"""Spec durability test: a template + mapping created in one app instance survive a
'restart' — a fresh create_app() on the same local DuckDB file reloads them, and a
reconciliation can run against the restored spec.

Run:  python -m api.test_api_persist_specs   (or pytest)
"""
from __future__ import annotations
import os
import tempfile

from fastapi.testclient import TestClient

from api.main import create_app
from persistence import DuckDBStore

EXP = (7, 2, 1, 1, 1, 1, 1)


def _bootstrap_and_approve(c):
    boot = c.post("/api/v1/demo/bootstrap").json()
    mid = c.post(f"/api/v1/mappings/suggestions/{boot['suggestion_id']}/approve",
                 json={"reject_edge_ids": []}).json()["mapping_id"]
    return boot, mid


def test_specs_survive_restart():
    path = os.path.join(tempfile.mkdtemp(), "specs.duckdb")

    # instance #1: create template (approved) + mapping
    c1 = TestClient(create_app(store=DuckDBStore(path)))
    boot, mid = _bootstrap_and_approve(c1)
    tid = boot["template_id"]

    # instance #2: fresh app on the SAME file -> specs reloaded from the store
    c2 = TestClient(create_app(store=DuckDBStore(path)))
    rev = c2.get(f"/api/v1/templates/{tid}/review")
    assert rev.status_code == 200, "template should be restored after restart"
    assert rev.json()["status"] == "PUBLISHED"

    # the restored template + mapping still reconcile (datasets re-registered here)
    boot2, _ = _bootstrap_and_approve(c2)   # datasets/code-list for this instance
    run = c2.post("/api/v1/recon/runs", json={
        "template_id": tid, "mapping_id": mid,
        "code_lists_id": boot2["code_lists_id"],
        "source_dataset_id": boot2["source_dataset_id"],
        "dest_dataset_id": boot2["dest_dataset_id"], "as_of_date": "2026-06-04"})
    assert run.status_code == 202, run.text
    s = run.json()["summary"]
    assert tuple(s[k] for k in ["total_records", "match_count", "mismatch_count",
                 "missing_src", "missing_dst", "duplicate_count", "exception_count"]) == EXP


if __name__ == "__main__":
    path = os.path.join(tempfile.mkdtemp(), "specs.duckdb")
    c1 = TestClient(create_app(store=DuckDBStore(path)))
    boot, mid = _bootstrap_and_approve(c1)
    tid = boot["template_id"]
    print("instance #1: created template", tid[:8], "mapping", mid[:8])
    c2 = TestClient(create_app(store=DuckDBStore(path)))
    rev = c2.get(f"/api/v1/templates/{tid}/review")
    print("instance #2: template restored ->", rev.status_code, rev.json().get("status"))
    ok = rev.status_code == 200 and rev.json().get("status") == "PUBLISHED"
    print("RESULT      :", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
