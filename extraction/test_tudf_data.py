"""Tests for the TUDF data-file parser (extraction/tudf_data.py).

  * round-trip on the committed synthetic DEMO_LAYOUT (always runs);
  * a full reconcile: a TUDF data file is parsed into rows and compared against a
    source 'system of record' CSV via the real Rule Engine (always runs);
  * round-trip on the REAL spec layout (skipped unless TUDF_SPEC_PDF is set).

Run:  python -m extraction.test_tudf_data   (or pytest)
"""
from __future__ import annotations
import os
import tempfile

import pytest

from extraction.tudf_data import (DEMO_LAYOUT, encode_file, parse_file, rows_to_csv)
from extraction import load_pdf, TudfExtractor
from orchestrator.resolver import resolve
from orchestrator.engine import run as engine_run

HEADER = {"header.segment_tag": "TUDF", "header.version": "14",
          "header.as_of_date": "20260101", "header.member_code": "MEMBER0001"}

_env = os.environ.get("TUDF_SPEC_PDF")
PDF = _env if (_env and os.path.exists(_env)) else None
needs_pdf = pytest.mark.skipif(PDF is None, reason="TUDF spec PDF not available; set TUDF_SPEC_PDF")


def _subject(idn, fam, dob, acc, status, limit, owed, opened):
    return {
        "pn.segment_tag": "N01", "pn.family_name_1": fam, "pn.id_number": idn,
        "pn.date_of_birth": dob,
        "tl.segment_tag": "TL01", "tl.account_no": acc, "tl.account_status": status,
        "tl.credit_limit": limit, "tl.amount_owed": owed, "tl.date_account_opened": opened,
    }


# =====================================================================
def test_roundtrip_demo_layout():
    subjects = [
        _subject("1100000000001", "SMITH", "19640423", "ACC001", "11", "50000", "12000", "20230115"),
        _subject("1100000000002", "JONES", "19800101", "ACC002", "13", "12000", "0", "20220630"),
    ]
    text = encode_file(HEADER, subjects, DEMO_LAYOUT)
    # the spec example: "SMITH" in Surname1 -> tag 01, len 05
    assert "0105SMITH" in text
    header_row, rows = parse_file(text, DEMO_LAYOUT)

    assert header_row["header.segment_tag"] == "TUDF" and header_row["header.version"] == "14"
    assert len(rows) == 2
    for got, exp in zip(rows, subjects):
        for k in ("pn.id_number", "pn.family_name_1", "pn.date_of_birth",
                  "tl.account_no", "tl.account_status", "tl.credit_limit",
                  "tl.amount_owed", "tl.date_account_opened"):
            assert got[k] == exp[k], (k, got.get(k), exp[k])


def test_reconcile_source_vs_parsed_tudf():
    # source = system of record (3 accounts)
    src_rows = [
        {"account_no": "ACC001", "account_status": "11", "credit_limit": "50000", "date_account_opened": "20230115"},
        {"account_no": "ACC002", "account_status": "13", "credit_limit": "12000", "date_account_opened": "20220630"},
        {"account_no": "ACC003", "account_status": "20", "credit_limit": "0", "date_account_opened": "20240101"},
    ]
    # destination = the TUDF file: ACC002 status differs (13->99); ACC003 dropped;
    # ACC004 added (not in source)
    tudf_subjects = [
        _subject("1100000000001", "SMITH", "19640423", "ACC001", "11", "50000", "12000", "20230115"),
        _subject("1100000000002", "JONES", "19800101", "ACC002", "99", "12000", "0", "20220630"),
        _subject("1100000000004", "BROWN", "19900909", "ACC004", "11", "3000", "0", "20250101"),
    ]
    tmp = tempfile.mkdtemp()
    src_csv = os.path.join(tmp, "source.csv")
    rows_to_csv(src_rows, ["account_no", "account_status", "credit_limit", "date_account_opened"], src_csv)

    text = encode_file(HEADER, tudf_subjects, DEMO_LAYOUT)
    _, parsed = parse_file(text, DEMO_LAYOUT)
    dst_csv = os.path.join(tmp, "dest.csv")
    rows_to_csv(parsed, ["tl.account_no", "tl.account_status", "tl.credit_limit",
                         "tl.date_account_opened"], dst_csv)

    template = {
        "template_id": "tudf_demo", "version": "1.0.0",
        "fields": [
            {"field_key": "account_no", "datatype": "STRING", "is_key": True},
            {"field_key": "account_status", "datatype": "STRING"},
            {"field_key": "credit_limit", "datatype": "DECIMAL"},
            {"field_key": "date_account_opened", "datatype": "DATE"},
        ],
        "rules": [
            {"rule_id": "r_status", "type": "EQUALITY", "severity": "ERROR",
             "target_field_keys": ["account_status"]},
            {"rule_id": "r_limit", "type": "TOLERANCE", "severity": "ERROR",
             "params": {"tolerance": 0, "scale": 2}, "target_field_keys": ["credit_limit"]},
            {"rule_id": "r_open", "type": "DATE_VALID", "severity": "ERROR",
             "params": {"format": "%Y%m%d", "not_future": True},
             "target_field_keys": ["date_account_opened"]},
        ],
    }
    mapping = {
        "expected_side": "SOURCE",
        "key_pairs": [{"field_key": "account_no", "src_col": "account_no", "dst_col": "tl.account_no"}],
        "field_bindings": [
            {"field_key": "account_no", "src_col": "account_no", "dst_col": "tl.account_no"},
            {"field_key": "account_status", "src_col": "account_status", "dst_col": "tl.account_status"},
            {"field_key": "credit_limit", "src_col": "credit_limit", "dst_col": "tl.credit_limit"},
            {"field_key": "date_account_opened", "src_col": "date_account_opened", "dst_col": "tl.date_account_opened"},
        ],
    }
    cfg = resolve(template, mapping, {}, as_of_date="2026-06-04")
    res = engine_run(cfg, src_csv, dst_csv)
    # total 4: ACC001 match, ACC002 mismatch(status), ACC003 missing in dest, ACC004 missing in source
    assert res.summary_tuple() == (4, 1, 1, 1, 1, 0, 0)


@needs_pdf
def test_roundtrip_on_real_spec_layout():
    """Encode + parse using the layout derived from the actual TUDF spec PDF."""
    layout = TudfExtractor().build_layout(load_pdf(PDF))
    assert "header" in layout and "pn" in layout and "tl" in layout
    # pick a few real fields and round-trip them
    pn_fam = next(f for f in layout["pn"]["fields"] if f["field_key"] == "pn.family_name_1")
    tl_amt = next(f for f in layout["tl"]["fields"]
                  if f["field_key"] == "tl.credit_limit_original_loan_amount")
    subj = {"pn.segment_tag": "N01", "pn.family_name_1": "SMITH",
            "tl.segment_tag": "TL01", tl_amt["field_key"]: "500000"}
    header = {f["field_key"]: ("TUDF" if f["field_key"].endswith("segment_tag") else "1")
              for f in layout["header"]["fields"]}
    text = encode_file(header, [subj], layout)
    _, rows = parse_file(text, layout)
    assert len(rows) == 1
    assert rows[0]["pn.family_name_1"] == "SMITH"
    assert rows[0][tl_amt["field_key"]] == "500000"
    assert pn_fam["tag"] == "01"     # the real spec tags Surname1 as 01


if __name__ == "__main__":
    test_roundtrip_demo_layout()
    print("round-trip (demo): PASS")
    test_reconcile_source_vs_parsed_tudf()
    print("reconcile source vs TUDF: PASS")
    if PDF:
        test_roundtrip_on_real_spec_layout()
        print("round-trip (real spec layout): PASS")
    else:
        print("real-layout round-trip: SKIPPED (set TUDF_SPEC_PDF)")
