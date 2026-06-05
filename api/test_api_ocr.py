"""API test: a scanned PDF upload is blocked at extraction unless the app is
configured with an OCR engine.

Run:  python -m api.test_api_ocr   (or pytest)
"""
from __future__ import annotations
import os
import tempfile

from fastapi.testclient import TestClient

from api.main import create_app
from extraction import build_scanned_pdf, spec_text, MockOcr


def _scanned_bytes():
    p = os.path.join(tempfile.mkdtemp(), "scanned.pdf")
    build_scanned_pdf(p)
    with open(p, "rb") as f:
        return f.read()


def _upload(c, data):
    return c.post("/api/v1/documents", data={"name": "scan"},
                  files={"file": ("scan.pdf", data, "application/pdf")}).json()


def test_scanned_blocked_without_ocr():
    c = TestClient(create_app())                       # no OCR
    doc = _upload(c, _scanned_bytes())
    assert doc["is_scanned"] is True
    r = c.post(f"/api/v1/documents/{doc['id']}/extract")
    assert r.status_code == 422 and "OCR" in r.text


def test_scanned_extracts_with_ocr():
    c = TestClient(create_app(ocr=MockOcr(spec_text())))   # OCR configured
    doc = _upload(c, _scanned_bytes())
    r = c.post(f"/api/v1/documents/{doc['id']}/extract")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "DRAFT"
    assert body["pending_review"]["total_pending"] == 17   # 9 fields + 8 rules


if __name__ == "__main__":
    c0 = TestClient(create_app())
    d0 = _upload(c0, _scanned_bytes())
    r0 = c0.post(f"/api/v1/documents/{d0['id']}/extract")
    print("no OCR    :", r0.status_code, "(expected 422)")
    c1 = TestClient(create_app(ocr=MockOcr(spec_text())))
    d1 = _upload(c1, _scanned_bytes())
    r1 = c1.post(f"/api/v1/documents/{d1['id']}/extract")
    print("with OCR  :", r1.status_code, "pending", r1.json().get("pending_review", {}).get("total_pending"))
    ok = r0.status_code == 422 and r1.status_code == 201
    print("RESULT    :", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
