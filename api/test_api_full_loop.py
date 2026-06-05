"""The complete AI-assisted loop over HTTP, with a human approval gate at BOTH AI steps:

  upload PDF -> AI extract (Draft Template, SUGGESTED)
             -> human approves template
             -> AI suggest mapping (edges, SUGGESTED, confidence)
             -> human approves edges -> mapping materialized
             -> reconcile -> validated report

Run:  python -m api.test_api_full_loop   (or pytest)
"""
from __future__ import annotations
import json
import os
import tempfile

from fastapi.testclient import TestClient

from api.main import create_app
from extraction import build_sample_pdf

FX = os.path.join(os.path.dirname(__file__), "..", "orchestrator", "fixtures")
EXP = (7, 2, 1, 1, 1, 1, 1)


def _load(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f)


def _run_loop(c):
    # datasets + code lists
    cid = c.post("/api/v1/code-lists", json={"body": _load("code_lists.json")}).json()["id"]
    with open(os.path.join(FX, "source.csv"), "rb") as f:
        sid = c.post("/api/v1/datasets", data={"role": "SOURCE", "name": "src"},
                     files={"file": ("source.csv", f, "text/csv")}).json()["id"]
    with open(os.path.join(FX, "dest.csv"), "rb") as f:
        did = c.post("/api/v1/datasets", data={"role": "DESTINATION", "name": "dst"},
                     files={"file": ("dest.csv", f, "text/csv")}).json()["id"]

    # 1) PDF -> AI extract -> draft template
    pdf = os.path.join(tempfile.mkdtemp(), "spec.pdf")
    build_sample_pdf(pdf)
    with open(pdf, "rb") as f:
        doc = c.post("/api/v1/documents", data={"name": "spec"},
                     files={"file": ("spec.pdf", f, "application/pdf")}).json()
    ext = c.post(f"/api/v1/documents/{doc['id']}/extract").json()
    tid = ext["template_id"]

    # 2) human approves the template
    c.post(f"/api/v1/templates/{tid}/approve", params={"approver": "lead"})

    # 3) AI suggests the mapping
    sug = c.post("/api/v1/mappings/suggest", json={
        "template_id": tid, "source_dataset_id": sid, "dest_dataset_id": did}).json()
    sug_id = sug["suggestion_id"]
    assert sug["graph"]["edges"], "expected suggested edges"
    assert all(e["review_status"] == "SUGGESTED" for e in sug["graph"]["edges"])

    # 4) human approves the edges -> mapping materialized
    appr = c.post(f"/api/v1/mappings/suggestions/{sug_id}/approve",
                  json={"reject_edge_ids": [], "approver": "lead"}).json()
    mid = appr["mapping_id"]
    assert len(appr["mapping"]["key_pairs"]) == 2

    # 5) reconcile
    run = c.post("/api/v1/recon/runs", json={
        "template_id": tid, "mapping_id": mid, "code_lists_id": cid,
        "source_dataset_id": sid, "dest_dataset_id": did, "as_of_date": "2026-06-04"})
    return run


def test_full_ai_loop():
    c = TestClient(create_app())
    run = _run_loop(c)
    assert run.status_code == 202, run.text
    s = run.json()["summary"]
    assert tuple(s[k] for k in ["total_records", "match_count", "mismatch_count",
                 "missing_src", "missing_dst", "duplicate_count", "exception_count"]) == EXP


if __name__ == "__main__":
    c = TestClient(create_app())
    run = _run_loop(c)
    s = run.json().get("summary", {})
    got = tuple(s.get(k, -1) for k in ["total_records", "match_count", "mismatch_count",
                "missing_src", "missing_dst", "duplicate_count", "exception_count"])
    print("full AI loop summary:", got)
    print("RESULT             :", "PASS" if run.status_code == 202 and got == EXP else "FAIL")
    raise SystemExit(0 if got == EXP else 1)
