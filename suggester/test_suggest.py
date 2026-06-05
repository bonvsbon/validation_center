"""P4 test: AI suggests a mapping graph from template fields + dataset columns,
a human approves the edges, and the materialized mapping reconciles to the
validated result — proving the full AI-assisted loop with human approval.

Run:  python -m suggester.test_suggest   (or pytest)
"""
from __future__ import annotations
import csv
import os

from suggester import MockSuggester, pending_summary, approve_edges, to_mapping
from orchestrator.resolver import resolve, load_json
from orchestrator.engine import run as engine_run

FX = os.path.join(os.path.dirname(__file__), "..", "orchestrator", "fixtures")


def _columns(csv_path):
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [r for _, r in zip(range(5), reader)]
    return [{"column_name": h, "sample_values": [r[i] for r in rows]}
            for i, h in enumerate(header)]


def _suggest():
    template = load_json(os.path.join(FX, "template_ncb.json"))
    template["template_id"] = "tmpl_test"
    src = _columns(os.path.join(FX, "source.csv"))
    dst = _columns(os.path.join(FX, "dest.csv"))
    sm = MockSuggester().suggest(template, src, dst, "ds_src", "ds_dst")
    return template, sm


def test_suggestions_are_drafts_with_confidence():
    _, sm = _suggest()
    assert sm.nodes and sm.edges
    # every edge is SUGGESTED and carries a confidence + reasoning
    for e in sm.edges:
        assert e.review_status == "SUGGESTED"
        assert 0.0 <= e.confidence <= 1.0 and e.reasoning
    # key fields (account_no, id_card) should be flagged on their edges
    key_edges = {e.from_node for e in sm.edges if e.is_key}
    assert "pdf_account_no" in key_edges and "pdf_id_card" in key_edges


def test_approve_then_materialize_then_reconcile():
    template, sm = _suggest()
    ps = pending_summary(sm)
    assert ps["pending"] == len(sm.edges) > 0
    approve_edges(sm)                      # human approves all
    mapping = to_mapping(sm)
    assert len(mapping["key_pairs"]) == 2  # account_no + id_card
    assert len(mapping["field_bindings"]) == 9

    code_lists = load_json(os.path.join(FX, "code_lists.json"))
    cfg = resolve(template, mapping, code_lists, as_of_date="2026-06-04")
    res = engine_run(cfg, os.path.join(FX, "source.csv"), os.path.join(FX, "dest.csv"))
    assert res.summary_tuple() == (7, 2, 1, 1, 1, 1, 1)


if __name__ == "__main__":
    template, sm = _suggest()
    print("model        :", sm.model)
    print("nodes/edges  :", len(sm.nodes), "/", len(sm.edges))
    print("pending      :", pending_summary(sm))
    print("sample edges :")
    for e in sm.edges[:4]:
        print(f"   {e.kind:14} {e.from_node:22} -> {e.to_node:14} conf={e.confidence} key={e.is_key}")
    approve_edges(sm)
    mapping = to_mapping(sm)
    print("key_pairs    :", [k["field_key"] for k in mapping["key_pairs"]])
    print("bindings     :", len(mapping["field_bindings"]))
    code_lists = load_json(os.path.join(FX, "code_lists.json"))
    cfg = resolve(template, mapping, code_lists, as_of_date="2026-06-04")
    res = engine_run(cfg, os.path.join(FX, "source.csv"), os.path.join(FX, "dest.csv"))
    ok = res.summary_tuple() == (7, 2, 1, 1, 1, 1, 1)
    print("reconcile    :", res.summary_tuple())
    print("RESULT       :", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
