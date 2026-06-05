"""API test for the P3 flow over HTTP:

  upload PDF -> extract (draft, SUGGESTED) -> run BLOCKED (422, not approved)
  -> approve -> run SUCCEEDS with the validated summary.

Run:  python -m api.test_api_extraction   (or pytest)
"""
from __future__ import annotations
import json
import os
import tempfile

from fastapi.testclient import TestClient

from api.main import create_app
from extraction import build_sample_pdf

FX = os.path.join(os.path.dirname(__file__), "..", "orchestrator", "fixtures")


def _load(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f)


def _setup_data(c):
    mid = c.post("/api/v1/mappings", json={"body": _load("mapping_ncb.json")}).json()["id"]
    cid = c.post("/api/v1/code-lists", json={"body": _load("code_lists.json")}).json()["id"]
    with open(os.path.join(FX, "source.csv"), "rb") as f:
        sid = c.post("/api/v1/datasets", data={"role": "SOURCE", "name": "src"},
                     files={"file": ("source.csv", f, "text/csv")}).json()["id"]
    with open(os.path.join(FX, "dest.csv"), "rb") as f:
        did = c.post("/api/v1/datasets", data={"role": "DESTINATION", "name": "dst"},
                     files={"file": ("dest.csv", f, "text/csv")}).json()["id"]
    return mid, cid, sid, did


def _upload_and_extract(c):
    pdf_path = os.path.join(tempfile.mkdtemp(), "ncb_spec.pdf")
    build_sample_pdf(pdf_path)
    with open(pdf_path, "rb") as f:
        doc = c.post("/api/v1/documents", data={"name": "NCB M16 spec"},
                     files={"file": ("ncb_spec.pdf", f, "application/pdf")}).json()
    assert doc["pages"] >= 1 and doc["is_scanned"] is False
    ext = c.post(f"/api/v1/documents/{doc['id']}/extract",
                 params={"template_key": "ncb-m16"}).json()
    return ext


def test_pdf_to_draft_then_guarded_run():
    c = TestClient(create_app())
    mid, cid, sid, did = _setup_data(c)
    ext = _upload_and_extract(c)
    tid = ext["template_id"]
    assert ext["status"] == "DRAFT"
    assert ext["pending_review"]["total_pending"] == 17  # 9 fields + 8 rules SUGGESTED
    assert any("cross" in x for x in ext["pending_review"]["low_confidence"])

    body = {"template_id": tid, "mapping_id": mid, "code_lists_id": cid,
            "source_dataset_id": sid, "dest_dataset_id": did, "as_of_date": "2026-06-04"}

    # run must be BLOCKED while the draft is unapproved
    blocked = c.post("/api/v1/recon/runs", json=body)
    assert blocked.status_code == 422
    assert "not fully approved" in blocked.text

    # approve, then run succeeds
    appr = c.post(f"/api/v1/templates/{tid}/approve", params={"approver": "lead_a"}).json()
    assert appr["status"] == "PUBLISHED" and appr["pending_review"]["total_pending"] == 0

    ok = c.post("/api/v1/recon/runs", json=body)
    assert ok.status_code == 202, ok.text
    s = ok.json()["summary"]
    assert (s["total_records"], s["match_count"], s["mismatch_count"], s["missing_src"],
            s["missing_dst"], s["duplicate_count"], s["exception_count"]) == (7, 2, 1, 1, 1, 1, 1)


if __name__ == "__main__":
    c = TestClient(create_app())
    mid, cid, sid, did = _setup_data(c)
    ext = _upload_and_extract(c)
    tid = ext["template_id"]
    print("draft status :", ext["status"], "| pending:", ext["pending_review"]["total_pending"],
          "| low-conf:", ext["pending_review"]["low_confidence"])
    body = {"template_id": tid, "mapping_id": mid, "code_lists_id": cid,
            "source_dataset_id": sid, "dest_dataset_id": did, "as_of_date": "2026-06-04"}
    blocked = c.post("/api/v1/recon/runs", json=body)
    print("run before approve :", blocked.status_code, "(expected 422)")
    appr = c.post(f"/api/v1/templates/{tid}/approve", params={"approver": "lead_a"}).json()
    print("after approve      :", appr["status"], "pending", appr["pending_review"]["total_pending"])
    ok = c.post("/api/v1/recon/runs", json=body)
    s = ok.json().get("summary", {})
    good = (blocked.status_code == 422 and ok.status_code == 202
            and tuple(s.get(k, -1) for k in ["total_records", "match_count", "mismatch_count",
                      "missing_src", "missing_dst", "duplicate_count", "exception_count"])
            == (7, 2, 1, 1, 1, 1, 1))
    print("run after approve  :", ok.status_code, "summary", s)
    print("RESULT             :", "PASS" if good else "FAIL")
    raise SystemExit(0 if good else 1)
