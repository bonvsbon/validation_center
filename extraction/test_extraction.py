"""End-to-end P3 test:

  generate a sample NCB spec PDF
    -> load + MockExtractor
    -> Draft Template (everything SUGGESTED, citations present, low-conf flagged)
    -> approval gate blocks an un-reviewed template
    -> human approves
    -> the approved template + existing mapping reconcile to the validated result.

Run:  python -m extraction.test_extraction   (or pytest)
"""
from __future__ import annotations
import os
import tempfile

from extraction import (build_sample_pdf, load_pdf, MockExtractor,
                        new_extraction_run, to_draft_template, approval)
from orchestrator.resolver import resolve, load_json
from orchestrator.engine import run as engine_run

FX = os.path.join(os.path.dirname(__file__), "..", "orchestrator", "fixtures")


def _extract():
    pdf_path = os.path.join(tempfile.mkdtemp(), "ncb_spec.pdf")
    build_sample_pdf(pdf_path)
    doc = load_pdf(pdf_path)
    result = MockExtractor().extract(doc)
    erun = new_extraction_run(result, doc)
    tmpl = to_draft_template(result, doc, erun.run_id, template_key="ncb-m16")
    return doc, result, erun, tmpl


def test_extraction_produces_suggested_draft():
    doc, result, erun, tmpl = _extract()
    assert not doc.is_scanned
    assert len(tmpl["fields"]) == 9
    assert len(tmpl["rules"]) == 8
    assert tmpl["status"] == "DRAFT"
    # everything must be SUGGESTED and carry a PDF citation
    for f in tmpl["fields"]:
        assert f["review_status"] == "SUGGESTED"
        assert f["citation"] and f["citation"]["page"] >= 1 and f["citation"]["source_text"]
    for r in tmpl["rules"]:
        assert r["review_status"] == "SUGGESTED"
        assert r["citation"] and r["citation"]["source_text"]
    # the cross-field rule is low-confidence -> flagged for review
    assert any("cross" in x for x in erun.low_confidence)
    assert erun.file_hash and erun.field_count == 9 and erun.rule_count == 8


def test_approval_gate_blocks_then_allows():
    _, _, _, tmpl = _extract()
    try:
        approval.require_approved(tmpl)
        assert False, "should have blocked an un-approved template"
    except ValueError:
        pass
    approval.approve_all(tmpl, approver="analyst_a")
    assert approval.is_fully_approved(tmpl)
    assert tmpl["status"] == "PUBLISHED" and tmpl.get("locked") is True


def test_full_loop_pdf_to_report():
    _, _, _, tmpl = _extract()
    approval.approve_all(tmpl, approver="approver_b")
    approval.require_approved(tmpl)  # must not raise

    mapping = load_json(os.path.join(FX, "mapping_ncb.json"))
    code_lists = load_json(os.path.join(FX, "code_lists.json"))
    cfg = resolve(tmpl, mapping, code_lists, as_of_date="2026-06-04")
    res = engine_run(cfg, os.path.join(FX, "source.csv"), os.path.join(FX, "dest.csv"))
    assert res.summary_tuple() == (7, 2, 1, 1, 1, 1, 1)


if __name__ == "__main__":
    doc, result, erun, tmpl = _extract()
    print("pages         :", doc.page_count, "| scanned:", doc.is_scanned)
    print("extracted     :", erun.field_count, "fields,", erun.rule_count, "rules")
    print("model         :", erun.model)
    print("low-confidence :", erun.low_confidence)
    print("sample field  :", {k: tmpl['fields'][2][k] for k in
                              ('field_key', 'datatype', 'review_status', 'confidence', 'citation')})
    print("sample rule   :", {k: tmpl['rules'][1][k] for k in
                              ('rule_key', 'type', 'params', 'review_status', 'confidence')})
    try:
        approval.require_approved(tmpl); gate = "NOT blocked (BUG)"
    except ValueError:
        gate = "blocked (correct)"
    print("approval gate :", gate)
    approval.approve_all(tmpl, approver="analyst_a")
    mapping = load_json(os.path.join(FX, "mapping_ncb.json"))
    code_lists = load_json(os.path.join(FX, "code_lists.json"))
    cfg = resolve(tmpl, mapping, code_lists, as_of_date="2026-06-04")
    res = engine_run(cfg, os.path.join(FX, "source.csv"), os.path.join(FX, "dest.csv"))
    ok = res.summary_tuple() == (7, 2, 1, 1, 1, 1, 1)
    print("reconcile      :", res.summary_tuple())
    print("RESULT         :", "PASS" if ok and gate.startswith("blocked") else "FAIL")
    raise SystemExit(0 if ok else 1)
