"""API test: upload the real TUDF spec and extract it via ?extractor=tudf.

Skipped unless the confidential TUDF PDF is available locally (set TUDF_SPEC_PDF).
Run:  python -m api.test_api_tudf   (or pytest)
"""
from __future__ import annotations
import os

import pytest
from fastapi.testclient import TestClient

from api.main import create_app

_env = os.environ.get("TUDF_SPEC_PDF")          # confidential spec: env var only, never committed
PDF = _env if (_env and os.path.exists(_env)) else None
needs_pdf = pytest.mark.skipif(PDF is None, reason="TUDF spec PDF not available; set TUDF_SPEC_PDF")


@needs_pdf
def test_tudf_upload_and_extract():
    c = TestClient(create_app())
    with open(PDF, "rb") as f:
        doc = c.post("/api/v1/documents", data={"name": "TUDF v14"},
                     files={"file": ("tudf.pdf", f, "application/pdf")}).json()
    assert doc["pages"] > 30 and doc["is_scanned"] is False

    r = c.post(f"/api/v1/documents/{doc['id']}/extract",
               params={"template_key": "tudf-consumer", "extractor": "tudf"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "DRAFT"
    # many SUGGESTED fields+rules awaiting human review
    assert body["pending_review"]["total_pending"] >= 50
    assert body["extraction_run"]["model"].startswith("tudf-extractor")

    # the draft can be reviewed via the API
    rev = c.get(f"/api/v1/templates/{body['template_id']}/review").json()
    assert any(f["field_key"] == "header.as_of_date" and f["datatype"] == "DATE"
               for f in rev["fields"])


@needs_pdf
def test_unknown_extractor_rejected():
    c = TestClient(create_app())
    with open(PDF, "rb") as f:
        doc = c.post("/api/v1/documents", data={"name": "x"},
                     files={"file": ("tudf.pdf", f, "application/pdf")}).json()
    r = c.post(f"/api/v1/documents/{doc['id']}/extract", params={"extractor": "nope"})
    assert r.status_code == 400


if __name__ == "__main__":
    if PDF is None:
        print("SKIPPED (set TUDF_SPEC_PDF)"); raise SystemExit(0)
    c = TestClient(create_app())
    with open(PDF, "rb") as f:
        doc = c.post("/api/v1/documents", data={"name": "TUDF v14"},
                     files={"file": ("tudf.pdf", f, "application/pdf")}).json()
    r = c.post(f"/api/v1/documents/{doc['id']}/extract",
               params={"template_key": "tudf-consumer", "extractor": "tudf"}).json()
    print("pages:", doc["pages"], "| pending:", r["pending_review"]["total_pending"],
          "| model:", r["extraction_run"]["model"])
    print("RESULT: PASS")
