"""OCR path test:

  a scanned PDF (no text layer) blocks extraction
    -> route through an OCR engine (MockOcr) -> text recovered
    -> extraction produces the same Draft Template as the digital PDF.

Proves the scanned-document routing without needing a Tesseract binary. Run:
  python -m extraction.test_ocr   (or pytest)
"""
from __future__ import annotations
import os
import tempfile

from extraction import (build_scanned_pdf, spec_text, load_pdf,
                        MockExtractor, MockOcr)


def _scanned():
    p = os.path.join(tempfile.mkdtemp(), "scanned.pdf")
    build_scanned_pdf(p)
    return p


def test_scanned_pdf_has_no_text_layer():
    doc = load_pdf(_scanned())
    assert doc.is_scanned is True
    assert doc.full_text.strip() == ""


def test_extraction_blocked_without_ocr():
    doc = load_pdf(_scanned())
    try:
        MockExtractor().extract(doc)
        assert False, "scanned PDF should block extraction without OCR"
    except ValueError as e:
        assert "OCR" in str(e)


def test_ocr_recovers_text_then_extracts():
    doc = load_pdf(_scanned(), ocr=MockOcr(spec_text()))
    assert doc.is_scanned is True          # flag stays; text was recovered via OCR
    assert doc.full_text.strip()
    result = MockExtractor().extract(doc)
    assert len(result.fields) == 9 and len(result.rules) == 8


if __name__ == "__main__":
    p = _scanned()
    d0 = load_pdf(p)
    print("scanned detect :", d0.is_scanned, "| text len:", len(d0.full_text.strip()))
    try:
        MockExtractor().extract(d0); blocked = "NOT blocked (BUG)"
    except ValueError:
        blocked = "blocked (correct)"
    print("without OCR    :", blocked)
    d1 = load_pdf(p, ocr=MockOcr(spec_text()))
    r = MockExtractor().extract(d1)
    print("with MockOcr   :", len(r.fields), "fields,", len(r.rules), "rules")
    ok = d0.is_scanned and blocked.startswith("blocked") and len(r.fields) == 9 and len(r.rules) == 8
    print("RESULT         :", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
