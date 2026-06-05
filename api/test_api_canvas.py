"""API test for the canvas-driven flow:

  POST /demo/bootstrap  -> suggestion graph + ids (one call)
  -> approve suggestion  -> mapping materialized
  -> reconcile           -> validated summary

This is exactly what the React Flow canvas's "Load from API" + "Approve & reconcile"
buttons call. Run:  python -m api.test_api_canvas   (or pytest)
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from api.main import create_app

EXP = (7, 2, 1, 1, 1, 1, 1)


def test_canvas_bootstrap_to_reconcile():
    c = TestClient(create_app())
    boot = c.post("/api/v1/demo/bootstrap").json()
    assert boot["graph"]["edges"], "bootstrap should return suggested edges"
    assert all(e["review_status"] == "SUGGESTED" for e in boot["graph"]["edges"])

    appr = c.post(f"/api/v1/mappings/suggestions/{boot['suggestion_id']}/approve",
                  json={"reject_edge_ids": [], "approver": "canvas-user"}).json()
    mid = appr["mapping_id"]

    run = c.post("/api/v1/recon/runs", json={
        "template_id": boot["template_id"], "mapping_id": mid,
        "code_lists_id": boot["code_lists_id"],
        "source_dataset_id": boot["source_dataset_id"],
        "dest_dataset_id": boot["dest_dataset_id"],
        "as_of_date": boot["as_of_date"]})
    assert run.status_code == 202, run.text
    s = run.json()["summary"]
    assert tuple(s[k] for k in ["total_records", "match_count", "mismatch_count",
                 "missing_src", "missing_dst", "duplicate_count", "exception_count"]) == EXP


if __name__ == "__main__":
    c = TestClient(create_app())
    boot = c.post("/api/v1/demo/bootstrap").json()
    print("bootstrap   :", len(boot["graph"]["nodes"]), "nodes,",
          len(boot["graph"]["edges"]), "edges | pending", boot["pending"]["pending"])
    appr = c.post(f"/api/v1/mappings/suggestions/{boot['suggestion_id']}/approve",
                  json={"reject_edge_ids": []}).json()
    run = c.post("/api/v1/recon/runs", json={
        "template_id": boot["template_id"], "mapping_id": appr["mapping_id"],
        "code_lists_id": boot["code_lists_id"], "source_dataset_id": boot["source_dataset_id"],
        "dest_dataset_id": boot["dest_dataset_id"], "as_of_date": boot["as_of_date"]})
    s = run.json().get("summary", {})
    got = tuple(s.get(k, -1) for k in ["total_records", "match_count", "mismatch_count",
                "missing_src", "missing_dst", "duplicate_count", "exception_count"])
    print("reconcile   :", got)
    print("RESULT      :", "PASS" if run.status_code == 202 and got == EXP else "FAIL")
    raise SystemExit(0 if got == EXP else 1)
