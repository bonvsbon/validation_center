"""End-to-end API test: register everything from the NCB fixtures, trigger a run
through HTTP, and assert the validated reference result. Also exercises results
filtering and the xlsx export.

Run:   python -m api.test_api      (or: pytest api/test_api.py)
"""
from __future__ import annotations
import json
import os

from fastapi.testclient import TestClient

from api.main import create_app

HERE = os.path.dirname(__file__)
FX = os.path.join(HERE, "..", "orchestrator", "fixtures")


def _load(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f)


def _client():
    return TestClient(create_app())


def _setup(c):
    tid = c.post("/api/v1/templates", json={"body": _load("template_ncb.json")}).json()["id"]
    mid = c.post("/api/v1/mappings", json={"body": _load("mapping_ncb.json")}).json()["id"]
    cid = c.post("/api/v1/code-lists", json={"body": _load("code_lists.json")}).json()["id"]
    with open(os.path.join(FX, "source.csv"), "rb") as f:
        sid = c.post("/api/v1/datasets", data={"role": "SOURCE", "name": "src"},
                     files={"file": ("source.csv", f, "text/csv")}).json()["id"]
    with open(os.path.join(FX, "dest.csv"), "rb") as f:
        did = c.post("/api/v1/datasets", data={"role": "DESTINATION", "name": "dst"},
                     files={"file": ("dest.csv", f, "text/csv")}).json()["id"]
    return tid, mid, cid, sid, did


def test_full_flow():
    c = _client()
    tid, mid, cid, sid, did = _setup(c)
    r = c.post("/api/v1/recon/runs", json={
        "template_id": tid, "mapping_id": mid, "code_lists_id": cid,
        "source_dataset_id": sid, "dest_dataset_id": did, "as_of_date": "2026-06-04"})
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["status"] == "COMPLETED"
    s = run["summary"]
    assert (s["total_records"], s["match_count"], s["mismatch_count"], s["missing_src"],
            s["missing_dst"], s["duplicate_count"], s["exception_count"]) == (7, 2, 1, 1, 1, 1, 1)
    run_id = run["run_id"]

    # filter: field-level mismatches = status (ERROR) + rate (WARNING)
    res = c.get(f"/api/v1/recon/runs/{run_id}/results", params={"category": "MISMATCH"}).json()
    assert res["total"] == 2
    assert {i["field_key"] for i in res["items"]} == {"status", "rate"}

    # narrower filter by field
    only = c.get(f"/api/v1/recon/runs/{run_id}/results",
                 params={"category": "MISMATCH", "field_key": "status"}).json()
    assert only["total"] == 1 and only["items"][0]["actual"] == "99"

    # export xlsx
    ex = c.get(f"/api/v1/recon/runs/{run_id}/export")
    assert ex.status_code == 200
    assert ex.headers["content-type"].startswith(
        "application/vnd.openxmlformats")
    assert len(ex.content) > 2000  # a real workbook


if __name__ == "__main__":
    c = _client()
    tid, mid, cid, sid, did = _setup(c)
    run = c.post("/api/v1/recon/runs", json={
        "template_id": tid, "mapping_id": mid, "code_lists_id": cid,
        "source_dataset_id": sid, "dest_dataset_id": did, "as_of_date": "2026-06-04"}).json()
    print("run status :", run["status"])
    print("summary    :", run["summary"])
    rid = run["run_id"]
    res = c.get(f"/api/v1/recon/runs/{rid}/results", params={"category": "MISMATCH"}).json()
    print("mismatch rows:", res["total"], res["items"])
    ex = c.get(f"/api/v1/recon/runs/{rid}/export")
    print("export bytes :", len(ex.content), ex.headers.get("content-type"))
    ok = (run["status"] == "COMPLETED"
          and tuple(run["summary"][k] for k in
                    ["total_records", "match_count", "mismatch_count", "missing_src",
                     "missing_dst", "duplicate_count", "exception_count"]) == (7, 2, 1, 1, 1, 1, 1)
          and len(ex.content) > 2000)
    print("RESULT     :", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
