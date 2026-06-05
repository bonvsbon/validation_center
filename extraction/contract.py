"""Extraction data contract — what any Extractor must return.

This is the structured-output shape the LLM is constrained to (see prompts.py),
and also what the deterministic MockExtractor returns. Every extracted field and
rule carries a citation back to the PDF + a confidence + a short reasoning, and is
born with review_status = SUGGESTED (nothing is trusted until a human approves).
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Citation:
    page: int
    source_text: str
    bbox: Optional[List[float]] = None   # [x,y,w,h] when available (digital/OCR)


@dataclass
class ExtractedField:
    field_key: str
    name: str
    datatype: str                        # STRING|INTEGER|DECIMAL|DATE|DATETIME|BOOLEAN|ENUM
    required: bool = False
    is_key: bool = False
    label_th: Optional[str] = None
    format: Optional[str] = None
    enum_code_list: Optional[str] = None
    citation: Optional[Citation] = None
    confidence: float = 0.0
    reasoning: str = ""


@dataclass
class ExtractedRule:
    rule_key: str
    type: str                            # one of the 8 rule types
    target_field_keys: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    severity: str = "ERROR"
    citation: Optional[Citation] = None
    confidence: float = 0.0
    reasoning: str = ""


@dataclass
class ExtractionResult:
    fields: List[ExtractedField] = field(default_factory=list)
    rules: List[ExtractedRule] = field(default_factory=list)
    model: str = ""
    prompt_version: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def low_confidence(self, threshold: float = 0.7) -> List[str]:
        out = []
        for f in self.fields:
            if f.confidence < threshold:
                out.append(f"field:{f.field_key}")
        for r in self.rules:
            if r.confidence < threshold:
                out.append(f"rule:{r.rule_key}")
        return out
