"""Tests for the real NCB / TUDF spec extractor.

Two layers:
  * pure unit tests on the parser internals — always run (no PDF needed);
  * integration tests against the actual TUDF spec PDF — SKIPPED unless the file is
    available. The TUDF spec is a Confidential NCB document and is NOT committed to
    the repo; point the tests at a local copy via the TUDF_SPEC_PDF env var.

Run:  python -m extraction.test_tudf   (or pytest)
"""
from __future__ import annotations
import functools
import os

import pytest

from extraction.tudf import (TudfExtractor, _datatype, _slug, _required,
                            _RE_TAGGED, _RE_FIXED)
from extraction import load_pdf, new_extraction_run, to_draft_template, approval

# ---- locate the (confidential, un-committed) spec PDF via env var only ----
#   run locally with:  set TUDF_SPEC_PDF=C:\path\to\TUDF...pdf   (Windows)
#                       export TUDF_SPEC_PDF=/path/to/TUDF...pdf  (bash)
_env = os.environ.get("TUDF_SPEC_PDF")
PDF = _env if (_env and os.path.exists(_env)) else None
needs_pdf = pytest.mark.skipif(PDF is None, reason="TUDF spec PDF not available; set TUDF_SPEC_PDF")


# =====================================================================
# pure unit tests — always run
# =====================================================================
def test_datatype_inference():
    assert _datatype("A", "Member Name", "the name")[0] == "STRING"
    assert _datatype("A/N", "Member Code", "code")[0] == "STRING"
    assert _datatype("N", "Version", "must contain 13")[0] == "INTEGER"
    assert _datatype("N", "Credit Limit", "amount in THB") == ("DECIMAL", None)
    assert _datatype("N", "As Of Date", "a date in YYYYMMDD format") == ("DATE", "YYYYMMDD")


def test_slug():
    assert _slug("As Of Date") == "as_of_date"
    assert _slug("Family Name 1") == "family_name_1"
    assert _slug("Current/New Member Code") == "current_new_member_code"


def test_required_logic():
    assert _required("Required", "x", "tagged") is True
    assert _required("When Available", "x", "tagged") is False
    assert _required("See Comment", "x", "tagged") is False
    assert _required(None, "Must contain the value TUDF", "fixed") is True
    assert _required(None, "Fill with zeroes (Reserved for future use)", "fixed") is False


def test_tagged_row_regex():
    m = _RE_TAGGED.match("01 Family Name 1 Required A V 50 Contains the Family Name")
    assert m and m.group(1) == "01" and m.group(2) == "Family Name 1"
    assert m.group(3) == "Required" and m.group(4) == "A" and m.group(5) == "V" and m.group(6) == "50"
    # AN char-type + multi-word requirement
    m2 = _RE_TAGGED.match("23 Account Status Required AN F 2 See Appendix A")
    assert m2 and m2.group(4) == "AN" and m2.group(6) == "2"


def test_fixed_row_regex():
    m = _RE_FIXED.match("5 Version N 2 Must contain the number 13.")
    assert m and m.group(1) == "5" and m.group(2) == "Version"
    assert m.group(3) == "N" and m.group(4) == "2"


# =====================================================================
# integration tests — against the real PDF (skipped if unavailable)
# =====================================================================
@functools.lru_cache(maxsize=1)
def _extract():
    doc = load_pdf(PDF)
    return doc, TudfExtractor().extract(doc)


@needs_pdf
def test_not_scanned_and_has_many_fields():
    doc, res = _extract()
    assert doc.is_scanned is False
    assert len(res.fields) >= 55
    segments = {f.field_key.split(".")[0] for f in res.fields}
    assert {"header", "pn", "id", "pa", "tl"} <= segments
    # the long ACCOUNT (TL) segment is fully walked, not cut off at a page break
    tl = [f for f in res.fields if f.field_key.startswith("tl.")]
    assert len(tl) >= 25 and max(f.citation.page for f in tl) >= 31


@needs_pdf
def test_tl_amounts_are_decimal():
    _, res = _extract()
    decimals = {f.field_key for f in res.fields if f.datatype == "DECIMAL"}
    # numeric money fields in the account segment must infer DECIMAL (never float)
    assert "tl.credit_limit_original_loan_amount" in decimals
    assert any("amount" in k for k in decimals) and len(decimals) >= 3


@needs_pdf
def test_header_segment_exact():
    _, res = _extract()
    h = {f.field_key: f for f in res.fields if f.field_key.startswith("header.")}
    assert len(h) == 10                              # the TUDF header has exactly 10 fields
    assert h["header.segment_tag"].datatype == "STRING" and h["header.segment_tag"].required
    assert h["header.version"].datatype == "INTEGER"
    assert h["header.member_code"].format == "A/N:10"
    assert h["header.as_of_date"].datatype == "DATE"
    assert h["header.tracing_number"].datatype == "INTEGER" and h["header.tracing_number"].required
    assert h["header.future_use"].required is False   # "Reserved for future use"


@needs_pdf
def test_dates_required_and_rules_consistent():
    _, res = _extract()
    date_keys = {f.field_key for f in res.fields if f.datatype == "DATE"}
    assert {"header.as_of_date", "pn.date_of_birth", "tl.date_account_opened"} <= date_keys

    required = {f.field_key for f in res.fields if f.required}
    not_null = {r.target_field_keys[0] for r in res.rules if r.type == "NOT_NULL"}
    assert required == not_null and len(required) > 0

    date_valid = {r.target_field_keys[0] for r in res.rules if r.type == "DATE_VALID"}
    assert date_keys == date_valid
    dv = next(r for r in res.rules if r.type == "DATE_VALID")
    assert dv.params["format"] == "%Y%m%d"

    assert all(f.citation and f.citation.page >= 1 and f.citation.source_text for f in res.fields)


@needs_pdf
def test_draft_template_all_suggested_and_gated():
    doc, res = _extract()
    erun = new_extraction_run(res, doc)
    tmpl = to_draft_template(res, doc, erun.run_id, template_key="tudf-consumer")
    assert tmpl["status"] == "DRAFT"
    assert all(f["review_status"] == "SUGGESTED" for f in tmpl["fields"])
    assert all(r["review_status"] == "SUGGESTED" for r in tmpl["rules"])
    assert tmpl["source_document"]["pages"] == doc.page_count
    try:
        approval.require_approved(tmpl)
        assert False, "un-reviewed TUDF draft must be blocked"
    except ValueError:
        pass


if __name__ == "__main__":
    # pure units
    test_datatype_inference(); test_slug(); test_required_logic()
    test_tagged_row_regex(); test_fixed_row_regex()
    print("pure unit tests: PASS")
    if PDF is None:
        print("integration: SKIPPED (set TUDF_SPEC_PDF to the spec path)")
        raise SystemExit(0)
    doc, res = _extract()
    from collections import Counter
    seg = Counter(f.field_key.split(".")[0] for f in res.fields)
    print(f"extracted {len(res.fields)} fields, {len(res.rules)} rules | per segment {dict(seg)}")
    test_header_segment_exact(); test_dates_required_and_rules_consistent()
    test_draft_template_all_suggested_and_gated()
    print("integration tests: PASS")
