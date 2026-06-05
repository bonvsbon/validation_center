"""Smoke test: the NCB fixture must reproduce the validated reference result.

Run directly:   python -m orchestrator.test_smoke
Or with pytest:  pytest orchestrator/test_smoke.py
"""
from __future__ import annotations
import os

from .resolver import resolve, load_json
from .engine import run, SUMMARY_COLS

HERE = os.path.dirname(__file__)
FX = os.path.join(HERE, "fixtures")

# (total, match, mismatch, missing_src, missing_dst, duplicate, exception)
EXPECTED_SUMMARY = (7, 2, 1, 1, 1, 1, 1)

EXPECTED_ROLLUP = {
    "A001|1100000000001": "MATCH",
    "A002|1100000000002": "MISMATCH",
    "A003|1100000000003": "MATCH",       # rate out-of-range but WARNING -> no demote
    "A004|1100000000004": "DUPLICATE",
    "A005|1100000000005": "MISSING_IN_DEST",
    "A006|1100000000006": "MISSING_IN_SOURCE",
    "A007|1100000000007": "EXCEPTION",   # balance 'ABC' uncastable
}


def _run():
    cfg = resolve(
        load_json(os.path.join(FX, "template_ncb.json")),
        load_json(os.path.join(FX, "mapping_ncb.json")),
        load_json(os.path.join(FX, "code_lists.json")),
        as_of_date="2026-06-04",
    )
    return run(cfg,
               os.path.join(FX, "source.csv"),
               os.path.join(FX, "dest.csv"))


def test_summary_matches_reference():
    r = _run()
    assert r.summary_tuple() == EXPECTED_SUMMARY, \
        f"{dict(zip(SUMMARY_COLS, r.summary_tuple()))}"


def test_rollup_matches_reference():
    r = _run()
    assert dict(r.record_rollup) == EXPECTED_ROLLUP


def test_determinism():
    a, b = _run(), _run()
    assert a.summary_tuple() == b.summary_tuple()
    assert a.record_rollup == b.record_rollup


if __name__ == "__main__":
    res = _run()
    ok = res.summary_tuple() == EXPECTED_SUMMARY and dict(res.record_rollup) == EXPECTED_ROLLUP
    print("summary :", dict(zip(SUMMARY_COLS, res.summary_tuple())))
    print("rollup  :", dict(res.record_rollup))
    print("RESULT  :", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
