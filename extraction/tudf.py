"""TudfExtractor — a deterministic extractor for the real NCB / TransUnion Data
Format (TUDF) specification PDF.

TUDF segments come in two table shapes:

  Fixed-length (HEADER, ES, TRLR):
      <position> <Field Name> <CharType> <Length> <Description...>
      e.g.  "1 Segment Tag A 4 Must contain the value TUDF."

  Tagged / variable (PN, ID, PA, TL):
      <tag> <Field Name> <Requirement> <CharType> <F|V> <Length> <Description...>
      e.g.  "01 Family Name 1 Required A V 50 Contains the Family Name ..."
            "23 Account Status Required AN F 2 See Appendix A ..."

This parser walks the PDF line by line, tracks the current segment, and matches
field rows with the appropriate pattern. Each field is emitted as an
ExtractedField (datatype inferred from CharType + hints) with a citation back to
the page/line; Required fields get a NOT_NULL rule and YYYYMMDD dates get a
DATE_VALID rule. Everything is SUGGESTED — a human still approves.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple
import re

from .pdf import PdfDoc, PdfPage
from .contract import (ExtractionResult, ExtractedField, ExtractedRule, Citation)
from .extractor import Extractor

# segment title -> (code, format) ; format: "fixed" | "tagged"
# Titles must match the WHOLE line ($ anchored): a sentence in a field description
# such as "... the As Of Date in the TUDF Header Segment." must NOT reset the segment.
_SEGMENTS: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"^TUDF Header Segment$", re.I), "header", "fixed"),
    (re.compile(r"^Name Segment \(PN\)$", re.I), "pn", "tagged"),
    (re.compile(r"^Identification Segment \(ID\)$", re.I), "id", "tagged"),
    (re.compile(r"^Address Segment \(PA\)$", re.I), "pa", "tagged"),
    (re.compile(r"^Account Segment \(TL\)$", re.I), "tl", "tagged"),
    (re.compile(r"^End of Subject( Segment)?( \(ES\))?$", re.I), "es", "fixed"),
    (re.compile(r"^Trailer Segment \(TRLR\)$", re.I), "trlr", "fixed"),
]

_CHAR = r"(A/N|AN|A|N)"
_REQUIREMENT = r"(Required|When\s+Available|See\s+Comment|Optional|Conditional)"

# tagged: tag, name, requirement, chartype, F|V, length, desc
_RE_TAGGED = re.compile(
    rf"^(\*?\d{{2}}|[A-Z]{{2}})\s+(.+?)\s+{_REQUIREMENT}\s+{_CHAR}\s+([FV])\s+(\d+)\s+(.*)$")

# fixed: position, name, chartype, length, desc   (no requirement, no F/V)
_RE_FIXED = re.compile(rf"^(\d{{1,3}})\s+(.+?)\s+{_CHAR}\s+(\d+)\s+(.*)$")

# noise lines to skip outright
_NOISE = re.compile(r"(Confidential|TransUnion Data Format|Total characters|"
                    r"Position Field Name|Field\s*$|Tag\s*$|Name\s*$|Type\s*$|"
                    r"Char\.|Length Type Length|Length\s*$|Description/Comments|"
                    r"^Page\s|Fixed\s*$)", re.I)
_THAI = re.compile(r"[฀-๿]")

# a line that STARTS a new field row
_START_TAGGED = re.compile(r"^(\*?\d{2}|[A-Z]{2})\s+[A-Za-z]")
_START_FIXED = re.compile(r"^\d{1,3}\s+[A-Za-z]")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "field"


def _datatype(char: str, name: str, desc: str) -> Tuple[str, Optional[str]]:
    """Return (datatype, fmt_hint). YYYYMMDD -> DATE; numeric amounts -> DECIMAL."""
    if "YYYYMMDD" in desc.upper():
        return "DATE", "YYYYMMDD"
    low = name.lower()
    if char == "N" and any(w in low for w in ("amount", "balance", "limit", "value")):
        return "DECIMAL", None
    if char == "N":
        return "INTEGER", None
    return "STRING", None    # A, A/N, AN


def _required(requirement: Optional[str], desc: str, fmt: str) -> bool:
    if fmt == "tagged":
        return (requirement or "").lower().startswith("required")
    # fixed segments: mandatory unless the description says otherwise
    d = desc.lower()
    if any(p in d for p in ("not required", "fill with", "reserved", "can be used",
                            "fill the", "optional")):
        return False
    return True


class TudfExtractor(Extractor):
    name = "tudf-extractor/1.0.0"

    def extract(self, pdf: PdfDoc) -> ExtractionResult:
        if not pdf.full_text.strip():
            raise ValueError("PDF has no text (scanned); provide an OCR engine to load_pdf()")

        result = ExtractionResult(model=self.name, prompt_version="n/a")
        seg_code: Optional[str] = None
        seg_fmt: Optional[str] = None
        seen: set = set()
        buf: List[str] = []
        buf_page = 0

        def is_start(line: str) -> bool:
            pat = _START_TAGGED if seg_fmt == "tagged" else _START_FIXED
            return bool(pat.match(line))

        def flush():
            if not buf or seg_code is None:
                return
            self._emit(" ".join(buf), seg_code, seg_fmt, buf_page, seen, result)
            buf.clear()

        for page in pdf.pages:
            for line in page.lines:
                title = self._segment_of(line)
                if title:
                    flush()
                    seg_code, seg_fmt = title
                    continue
                if seg_code is None or _NOISE.search(line) or _THAI.search(line):
                    continue
                if is_start(line):
                    flush()
                    buf_page = page.page
                    buf.append(line)
                elif buf:
                    buf.append(line)          # continuation of the current field row
        flush()
        return result

    def _emit(self, text: str, seg_code: str, seg_fmt: str, page: int,
              seen: set, result: ExtractionResult) -> None:
        text = re.sub(r"\s+", " ", text).strip()
        parsed = self._tagged(text) if seg_fmt == "tagged" else self._fixed(text)
        if not parsed:
            return
        tag, fname, requirement, char, length, desc = parsed
        fname = fname.strip().rstrip(".")
        if len(fname) < 2 or len(fname) > 60:
            return
        key = f"{seg_code}.{_slug(fname)}"
        if key in seen:
            return
        seen.add(key)

        datatype, fmt_hint = _datatype(char, fname, desc)
        required = _required(requirement, desc, seg_fmt)
        cit = Citation(page=page, source_text=(text[:160]))

        result.fields.append(ExtractedField(
            field_key=key, name=fname, datatype=datatype, required=required, is_key=False,
            format=f"{char}:{length}" + (f"/{fmt_hint}" if fmt_hint else ""),
            citation=cit, confidence=0.9 if seg_fmt == "tagged" else 0.85,
            reasoning=f"{seg_code.upper()} segment field {tag}, page {page}"))

        if required:
            result.rules.append(ExtractedRule(
                rule_key=f"{key}__not_null", type="NOT_NULL", target_field_keys=[key],
                severity="ERROR", citation=cit, confidence=0.85,
                reasoning="field marked Required"))
        if datatype == "DATE":
            result.rules.append(ExtractedRule(
                rule_key=f"{key}__date_valid", type="DATE_VALID", target_field_keys=[key],
                params={"format": "%Y%m%d", "not_future": True}, severity="ERROR",
                citation=cit, confidence=0.8, reasoning="description specifies YYYYMMDD"))

    # ---- helpers ----
    @staticmethod
    def _segment_of(line: str) -> Optional[Tuple[str, str]]:
        for pat, code, fmt in _SEGMENTS:
            if pat.search(line):
                return code, fmt
        return None

    @staticmethod
    def _tagged(line: str):
        m = _RE_TAGGED.match(line)
        if not m:
            return None
        tag, name, requirement, char, _lentype, length, desc = m.groups()
        return tag, name, requirement, char.replace("AN", "A/N"), length, desc

    @staticmethod
    def _fixed(line: str):
        m = _RE_FIXED.match(line)
        if not m:
            return None
        pos, name, char, length, desc = m.groups()
        # guard: position should be a small ascending int (1..120); skip junk
        if int(pos) > 200:
            return None
        return pos, name, None, char.replace("AN", "A/N"), length, desc
