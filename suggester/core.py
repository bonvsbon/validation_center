"""AI mapping suggestion (P4).

Given a Template (the PDF-derived spec) plus Source and Destination dataset columns,
propose a BOM-style mapping graph:  PDF_FIELD -> SOURCE_FIELD -> SOURCE_FIELD -> DEST_FIELD,
each edge carrying a confidence + reasoning and born review_status=SUGGESTED.

  * MockSuggester      — deterministic name/datatype similarity; runnable + tested.
  * AnthropicSuggester — Claude-backed for fuzzy/Thai column names (gated on API key).

A human approves/rejects edges; only then is a resolver-compatible mapping
(key_pairs + field_bindings) materialized. AI never finalizes a mapping itself.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
import difflib
import os
import re

_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _looks_numeric(samples: List[Any]) -> bool:
    vals = [str(v) for v in (samples or []) if v not in (None, "")]
    return bool(vals) and all(_NUM.match(v) for v in vals)


def _datatype_compatible(field_datatype: str, samples: List[Any]) -> bool:
    numeric_field = field_datatype in ("DECIMAL", "INTEGER")
    return numeric_field == _looks_numeric(samples)


@dataclass
class Edge:
    id: str
    kind: str                       # PDF_TO_SOURCE | SOURCE_TO_DEST
    from_node: str
    to_node: str
    is_key: bool = False
    confidence: float = 0.0
    reasoning: str = ""
    review_status: str = "SUGGESTED"
    ai_suggested: bool = True


@dataclass
class Node:
    id: str
    type: str                       # PDF_FIELD | SOURCE_FIELD | DEST_FIELD
    label: str
    ref: str                        # field_key or column_name
    x: float = 0.0
    y: float = 0.0


@dataclass
class SuggestedMapping:
    template_id: str
    source_dataset_id: str
    dest_dataset_id: str
    nodes: List[Node] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "source_dataset_id": self.source_dataset_id,
            "dest_dataset_id": self.dest_dataset_id,
            "model": self.model,
            "nodes": [asdict(n) for n in self.nodes],
            "edges": [asdict(e) for e in self.edges],
        }


class Suggester:
    name = "suggester"

    def suggest(self, template: Dict[str, Any], source_cols: List[Dict[str, Any]],
                dest_cols: List[Dict[str, Any]],
                source_dataset_id: str = "", dest_dataset_id: str = "") -> SuggestedMapping:
        raise NotImplementedError


class MockSuggester(Suggester):
    name = "mock-suggester/1.0.0"
    THRESHOLD = 0.45

    def suggest(self, template, source_cols, dest_cols,
                source_dataset_id="", dest_dataset_id="") -> SuggestedMapping:
        fields = template.get("fields", [])
        sm = SuggestedMapping(
            template_id=template.get("template_id", ""),
            source_dataset_id=source_dataset_id, dest_dataset_id=dest_dataset_id,
            model=self.name)

        # nodes: 3 columns
        for i, f in enumerate(fields):
            sm.nodes.append(Node(id=f"pdf_{f['field_key']}", type="PDF_FIELD",
                                 label=f["field_key"], ref=f["field_key"], x=0, y=i * 90))
        for i, c in enumerate(source_cols):
            sm.nodes.append(Node(id=f"src_{c['column_name']}", type="SOURCE_FIELD",
                                 label=c["column_name"], ref=c["column_name"], x=360, y=i * 90))
        for i, c in enumerate(dest_cols):
            sm.nodes.append(Node(id=f"dst_{c['column_name']}", type="DEST_FIELD",
                                 label=c["column_name"], ref=c["column_name"], x=720, y=i * 90))

        for f in fields:
            fk = f["field_key"]
            dt = f.get("datatype", "STRING")
            src = self._best(fk, dt, source_cols)
            dst = self._best(fk, dt, dest_cols)
            if src:
                col, conf, why = src
                sm.edges.append(Edge(
                    id=f"e_{fk}_src", kind="PDF_TO_SOURCE",
                    from_node=f"pdf_{fk}", to_node=f"src_{col}",
                    is_key=bool(f.get("is_key")), confidence=conf, reasoning=why))
            if dst:
                col, conf, why = dst
                sm.edges.append(Edge(
                    id=f"e_{fk}_dst", kind="SOURCE_TO_DEST",
                    from_node=(f"src_{src[0]}" if src else f"pdf_{fk}"),
                    to_node=f"dst_{col}",
                    is_key=bool(f.get("is_key")), confidence=conf, reasoning=why))
        return sm

    def _best(self, field_key, datatype, cols):
        best = None
        for c in cols:
            name = c["column_name"]
            score = _sim(field_key, name)
            compat = _datatype_compatible(datatype, c.get("sample_values"))
            if compat:
                score = min(1.0, score + 0.05)
            if best is None or score > best[1]:
                why = (f"name similarity {_sim(field_key, name):.2f}"
                       + (", datatype compatible" if compat else ", datatype mismatch"))
                best = (name, round(score, 2), why)
        if best and best[1] >= self.THRESHOLD:
            return best
        return None


class AnthropicSuggester(Suggester):
    """Claude-backed suggester for fuzzy / Thai column names. Gated on API key.
    Returns the same SuggestedMapping shape. Not exercised offline."""
    name_prefix = "anthropic-suggester"

    def __init__(self, model: str = "claude-sonnet-4-5", api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def name(self) -> str:
        return f"{self.name_prefix}/{self.model}"

    def suggest(self, template, source_cols, dest_cols,
                source_dataset_id="", dest_dataset_id="") -> SuggestedMapping:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set; cannot run AnthropicSuggester")
        # Production: send field/column names + sample values, constrain to a tool
        # that emits {field_key, src_col, dst_col, confidence, reasoning}. The graph
        # is assembled exactly like MockSuggester. Omitted here (no key offline).
        raise NotImplementedError("wire the Anthropic tool call here in production")
