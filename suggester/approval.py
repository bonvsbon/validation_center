"""Human approval over a SuggestedMapping, then materialize a resolver-compatible
mapping (key_pairs + field_bindings) from the APPROVED edges only.

AI proposes the graph; a human approves/rejects edges; this turns the approved
subset into the mapping the Rule Engine actually runs.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from .core import SuggestedMapping, Edge, Node


def pending_summary(sm: SuggestedMapping, threshold: float = 0.7) -> Dict[str, Any]:
    pending = [e.id for e in sm.edges if e.review_status == "SUGGESTED"]
    low = [e.id for e in sm.edges if e.confidence < threshold]
    return {"total_edges": len(sm.edges), "pending": len(pending),
            "pending_ids": pending, "low_confidence": low}


def approve_edges(sm: SuggestedMapping, reject_ids: Optional[List[str]] = None,
                  approver: str = "system") -> SuggestedMapping:
    reject = set(reject_ids or [])
    for e in sm.edges:
        e.review_status = "REJECTED" if e.id in reject else "APPROVED"
    return sm


def _node_index(sm: SuggestedMapping) -> Dict[str, Node]:
    return {n.id: n for n in sm.nodes}


def to_mapping(sm: SuggestedMapping, name: str = "ai-suggested",
               template_version: str = "1.0.0") -> Dict[str, Any]:
    """Build the resolver mapping from APPROVED edges.

    Each field gets src_col from its approved PDF_TO_SOURCE edge and dst_col from
    its approved SOURCE_TO_DEST edge. Fields whose edge is_key=True become key_pairs.
    """
    nodes = _node_index(sm)
    by_field: Dict[str, Dict[str, Any]] = {}

    for e in sm.edges:
        if e.review_status != "APPROVED":
            continue
        if e.kind == "PDF_TO_SOURCE":
            fk = nodes[e.from_node].ref          # pdf node ref = field_key
            col = nodes[e.to_node].ref
            d = by_field.setdefault(fk, {"field_key": fk})
            d["src_col"] = col
            d["is_key"] = d.get("is_key") or e.is_key
        elif e.kind == "SOURCE_TO_DEST":
            # from is a source node; tie back to the field via matching src_col
            dst_col = nodes[e.to_node].ref
            src_ref = nodes[e.from_node].ref
            # find the field that maps to this source column
            for fk, d in by_field.items():
                if d.get("src_col") == src_ref:
                    d["dst_col"] = dst_col
                    d["is_key"] = d.get("is_key") or e.is_key
                    break

    bindings = [d for d in by_field.values() if "src_col" in d and "dst_col" in d]
    key_pairs = [{"field_key": d["field_key"], "src_col": d["src_col"], "dst_col": d["dst_col"]}
                 for d in bindings if d.get("is_key")]
    field_bindings = [{"field_key": d["field_key"], "src_col": d["src_col"],
                       "dst_col": d["dst_col"]} for d in bindings]

    return {
        "name": name,
        "template_version": template_version,
        "expected_side": "SOURCE",
        "key_pairs": key_pairs,
        "field_bindings": field_bindings,
    }
