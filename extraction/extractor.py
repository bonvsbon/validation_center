"""Extractors: turn a PdfDoc into an ExtractionResult.

  * MockExtractor      — deterministic, parses the FIELDS/RULES sections. Runnable
                         and testable here; the reference implementation of the
                         contract.
  * AnthropicExtractor — calls Claude with a constrained tool (structured output).
                         Used in production for free-form specs. Gated on an API key.

Both produce identical-shaped ExtractionResult; everything is born SUGGESTED.
"""
from __future__ import annotations
from typing import Any, Dict, List
import os
import re

from .pdf import PdfDoc, PdfPage
from .contract import (ExtractionResult, ExtractedField, ExtractedRule, Citation)
from . import prompts

KNOWN_TYPES = {"STRING", "INTEGER", "DECIMAL", "DATE", "DATETIME", "BOOLEAN", "ENUM"}
KNOWN_RULES = {"EQUALITY", "TOLERANCE", "RANGE", "REGEX", "NOT_NULL", "LOOKUP",
               "DATE_VALID", "CROSS_FIELD"}


def _coerce(v: str) -> Any:
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d*\.\d+", v):
        return float(v)
    return v


def _parse_params(tokens: List[str]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for tok in tokens:
        if "=" in tok:
            k, val = tok.split("=", 1)
            params[k] = _coerce(val)
    return params


class Extractor:
    name = "extractor"

    def extract(self, pdf: PdfDoc) -> ExtractionResult:  # pragma: no cover
        raise NotImplementedError


class MockExtractor(Extractor):
    name = "mock-extractor/1.0.0"

    def extract(self, pdf: PdfDoc) -> ExtractionResult:
        if pdf.is_scanned:
            raise ValueError("PDF appears scanned (no text layer); OCR required before extraction")

        result = ExtractionResult(model=self.name, prompt_version="n/a")
        section = None
        for page in pdf.pages:
            for line in page.lines:
                up = line.upper()
                if up.startswith("SECTION: FIELDS"):
                    section = "FIELDS"; continue
                if up.startswith("SECTION: RULES"):
                    section = "RULES"; continue

                if section == "FIELDS" and " | " in line:
                    result.fields.append(self._field(line, page))
                elif section == "RULES" and " :: " in line:
                    result.rules.append(self._rule(line, page))
        return result

    def _field(self, line: str, page: PdfPage) -> ExtractedField:
        parts = [p.strip() for p in line.split("|")]
        key = parts[0]
        raw_type = parts[1].upper() if len(parts) > 1 else "STRING"
        flags = parts[2].lower().split() if len(parts) > 2 else []

        enum_cl = None
        m = re.match(r"ENUM\((?P<cl>[^)]+)\)", raw_type)
        if m:
            datatype = "ENUM"; enum_cl = m.group("cl")
        else:
            datatype = raw_type if raw_type in KNOWN_TYPES else "STRING"

        known = (datatype in KNOWN_TYPES) and (raw_type.split("(")[0] in KNOWN_TYPES or datatype == "ENUM")
        conf = 0.95 if known else 0.6
        if datatype == "ENUM":
            conf = 0.9
        return ExtractedField(
            field_key=key, name=key.replace("_", " "), datatype=datatype,
            required="required" in flags, is_key="key" in flags, enum_code_list=enum_cl,
            citation=Citation(page=page.page, source_text=line),
            confidence=conf,
            reasoning=f"parsed from FIELDS section, page {page.page}",
        )

    def _rule(self, line: str, page: PdfPage) -> ExtractedRule:
        segs = [s.strip() for s in line.split("::")]
        target = segs[0]
        body = segs[1].split() if len(segs) > 1 else []
        severity = segs[2].upper() if len(segs) > 2 else "ERROR"

        rtype = body[0].upper() if body else "EQUALITY"
        params = _parse_params(body[1:])
        targets = [t.strip() for t in target.split(",")]
        rule_key = f"{targets[0]}_{rtype.lower()}"

        known = rtype in KNOWN_RULES
        # cross-field inference is inherently harder -> lower confidence (will be flagged)
        conf = 0.66 if rtype == "CROSS_FIELD" else (0.9 if known else 0.5)
        return ExtractedRule(
            rule_key=rule_key, type=rtype if known else "EQUALITY",
            target_field_keys=targets, params=params, severity=severity,
            citation=Citation(page=page.page, source_text=line),
            confidence=conf,
            reasoning=f"parsed from RULES section, page {page.page}",
        )


class AnthropicExtractor(Extractor):
    """Production extractor — constrains Claude to the emit_template tool.

    Requires ANTHROPIC_API_KEY. Not exercised in offline tests; the code path is
    here for production wiring.
    """
    name_prefix = "anthropic"

    def __init__(self, model: str = "claude-sonnet-4-5", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def name(self) -> str:
        return f"{self.name_prefix}/{self.model}"

    def extract(self, pdf: PdfDoc) -> ExtractionResult:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set; cannot run AnthropicExtractor")
        if pdf.is_scanned:
            raise ValueError("PDF appears scanned; OCR required before extraction")
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key)
        resp = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=prompts.SYSTEM,
            tools=[prompts.TOOL],
            tool_choice={"type": "tool", "name": "emit_template"},
            messages=[{"role": "user",
                       "content": prompts.build_user_prompt(pdf.pages)}],
        )
        payload = next((b.input for b in resp.content if b.type == "tool_use"), None)
        if payload is None:
            raise RuntimeError("model did not return the emit_template tool call")
        return self._from_payload(payload)

    def _from_payload(self, payload: Dict[str, Any]) -> ExtractionResult:
        res = ExtractionResult(model=self.name, prompt_version=prompts.PROMPT_VERSION)
        for f in payload.get("fields", []):
            cit = f.get("citation") or {}
            res.fields.append(ExtractedField(
                field_key=f["field_key"], name=f.get("name", f["field_key"]),
                datatype=f["datatype"], required=f.get("required", False),
                is_key=f.get("is_key", False), label_th=f.get("label_th"),
                format=f.get("format"), enum_code_list=f.get("enum_code_list"),
                citation=Citation(page=cit.get("page", 0),
                                  source_text=cit.get("source_text", "")),
                confidence=float(f.get("confidence", 0)), reasoning=f.get("reasoning", "")))
        for r in payload.get("rules", []):
            cit = r.get("citation") or {}
            res.rules.append(ExtractedRule(
                rule_key=r["rule_key"], type=r["type"],
                target_field_keys=r.get("target_field_keys", []),
                params=r.get("params", {}), severity=r.get("severity", "ERROR"),
                citation=Citation(page=cit.get("page", 0),
                                  source_text=cit.get("source_text", "")),
                confidence=float(r.get("confidence", 0)), reasoning=r.get("reasoning", "")))
        return res
