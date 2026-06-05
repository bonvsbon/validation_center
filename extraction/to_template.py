"""Map an ExtractionResult into a Draft Template (resolver-compatible dict) and an
ExtractionRun audit record.

Everything lands as status=DRAFT with every field/rule review_status=SUGGESTED.
Nothing is usable for a reconciliation run until a human approves it (see
approval.py). The template stamps source_document hash + extraction_run_id so the
draft is traceable back to the exact PDF and extraction.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from .contract import ExtractionResult
from .pdf import PdfDoc


@dataclass
class ExtractionRun:
    run_id: str
    file_hash: str
    model: str
    prompt_version: str
    page_count: int
    field_count: int
    rule_count: int
    low_confidence: List[str]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def new_extraction_run(result: ExtractionResult, doc: PdfDoc,
                       threshold: float = 0.7) -> ExtractionRun:
    return ExtractionRun(
        run_id=str(uuid.uuid4()),
        file_hash=doc.file_hash,
        model=result.model,
        prompt_version=result.prompt_version,
        page_count=doc.page_count,
        field_count=len(result.fields),
        rule_count=len(result.rules),
        low_confidence=result.low_confidence(threshold),
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _cit(c) -> Optional[Dict[str, Any]]:
    return asdict(c) if c else None


def to_draft_template(result: ExtractionResult, doc: PdfDoc,
                      extraction_run_id: str, template_key: str,
                      version: str = "1.0.0") -> Dict[str, Any]:
    fields: List[Dict[str, Any]] = []
    for f in result.fields:
        fields.append({
            "field_key": f.field_key,
            "name": f.name,
            "label_th": f.label_th,
            "datatype": f.datatype,
            "required": f.required,
            "is_key": f.is_key,
            "format": f.format,
            "enum_code_list": f.enum_code_list,
            "citation": _cit(f.citation),
            "confidence": f.confidence,
            "ai_reasoning": f.reasoning,
            "review_status": "SUGGESTED",
        })

    rules: List[Dict[str, Any]] = []
    for r in result.rules:
        rules.append({
            "rule_id": r.rule_key,           # resolver uses rule_id
            "rule_key": r.rule_key,
            "type": r.type,
            "params": r.params,
            "severity": r.severity,
            "target_field_keys": r.target_field_keys,
            "citation": _cit(r.citation),
            "confidence": r.confidence,
            "ai_reasoning": r.reasoning,
            "review_status": "SUGGESTED",
        })

    return {
        "template_id": str(uuid.uuid4()),
        "key": template_key,
        "version": version,
        "status": "DRAFT",
        "source_document": {"file_hash": doc.file_hash, "pages": doc.page_count},
        "extraction_run_id": extraction_run_id,
        "fields": fields,
        "rules": rules,
    }
