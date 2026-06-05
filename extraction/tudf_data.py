"""TUDF data-file encoder/parser (layout-driven).

Turns a TUDF data file into rows (one dict per subject) so it can become a
reconciliation dataset, and can synthesize a conforming file for testing. Driven
by a layout (segment -> field framing) which comes either from
`TudfExtractor.build_layout(pdf)` (the real spec) or the committed synthetic
`DEMO_LAYOUT` (CI-safe — generic field names, not the confidential spec map).

Framing rules (from the TUDF spec, "Formatting the TUDF"):
  * Fixed-length segments (HEADER/ES/TRLR): positional, no tags.
  * Variable-length segments (PN/ID/PA/TL): every field is
        <2-char tag><2-char length><data>
    e.g. "0105SMITH" (tag 01, len 05, SMITH); "070819640423" (tag 07, len 08,
    date 19640423). The segment begins with its 2-letter segment-tag field
    (e.g. "PN03N01"). Subjects end with an ES marker; the file ends with TR.
"""
from __future__ import annotations
from typing import Any, Dict, List, Tuple
import csv

_SEG_IDS = {"PN", "ID", "PA", "TL", "ES", "TR", "TUDF"}


# --- committed synthetic layout (generic names; NOT the confidential spec map) ---
DEMO_LAYOUT: Dict[str, dict] = {
    "header": {"code": "header", "fmt": "fixed", "total": 24, "fields": [
        {"field_key": "header.segment_tag", "tag": None, "position": 1, "length": 4, "char": "A", "datatype": "STRING"},
        {"field_key": "header.version", "tag": None, "position": 5, "length": 2, "char": "N", "datatype": "INTEGER"},
        {"field_key": "header.as_of_date", "tag": None, "position": 7, "length": 8, "char": "N", "datatype": "DATE"},
        {"field_key": "header.member_code", "tag": None, "position": 15, "length": 10, "char": "A/N", "datatype": "STRING"},
    ]},
    "pn": {"code": "pn", "fmt": "tagged", "fields": [
        {"field_key": "pn.segment_tag", "tag": "PN", "length": 3, "lentype": "F", "char": "A/N", "datatype": "STRING"},
        {"field_key": "pn.family_name_1", "tag": "01", "length": 50, "lentype": "V", "char": "A", "datatype": "STRING"},
        {"field_key": "pn.id_number", "tag": "02", "length": 13, "lentype": "V", "char": "A/N", "datatype": "STRING"},
        {"field_key": "pn.date_of_birth", "tag": "07", "length": 8, "lentype": "F", "char": "N", "datatype": "DATE"},
    ]},
    "tl": {"code": "tl", "fmt": "tagged", "fields": [
        {"field_key": "tl.segment_tag", "tag": "TL", "length": 4, "lentype": "F", "char": "A/N", "datatype": "STRING"},
        {"field_key": "tl.account_no", "tag": "01", "length": 20, "lentype": "V", "char": "A/N", "datatype": "STRING"},
        {"field_key": "tl.account_status", "tag": "23", "length": 2, "lentype": "F", "char": "A/N", "datatype": "STRING"},
        {"field_key": "tl.credit_limit", "tag": "12", "length": 9, "lentype": "V", "char": "N", "datatype": "DECIMAL"},
        {"field_key": "tl.amount_owed", "tag": "13", "length": 9, "lentype": "V", "char": "N", "datatype": "DECIMAL"},
        {"field_key": "tl.date_account_opened", "tag": "09", "length": 8, "lentype": "F", "char": "N", "datatype": "DATE"},
    ]},
}


def _fixed_total(seg: dict) -> int:
    if seg.get("total"):
        return seg["total"]
    return max((f["position"] + f["length"] - 1) for f in seg["fields"])


def _pad(value: Any, char: str, length: int, lentype: str) -> str:
    v = "" if value is None else str(value)
    if lentype == "F":
        if char.startswith("N"):
            return v.rjust(length, "0")[:length]      # numeric: right-justify, zero-fill
        return v.ljust(length)[:length]               # alpha/AN: left-justify, space-fill
    return v                                           # variable: actual length


# ---------------- encode ----------------
def encode_fixed(values: Dict[str, Any], seg: dict) -> str:
    buf = [" "] * _fixed_total(seg)
    for f in seg["fields"]:
        data = _pad(values.get(f["field_key"], ""), f["char"], f["length"], "F")
        start = f["position"] - 1
        buf[start:start + f["length"]] = list(data)
    return "".join(buf)


def encode_var(values: Dict[str, Any], seg: dict) -> str:
    parts: List[str] = []
    for f in seg["fields"]:
        fk = f["field_key"]
        if fk.endswith(".segment_tag"):
            val = _pad(values.get(fk, ""), f["char"], f["length"], f["lentype"])
            parts.append(f"{f['tag']}{len(val):02d}{val}")
        elif fk in values and values[fk] not in (None, ""):
            val = _pad(values[fk], f["char"], f["length"], f["lentype"])
            parts.append(f"{f['tag']}{len(val):02d}{val}")
    return "".join(parts)


def encode_file(header: Dict[str, Any], subjects: List[Dict[str, Any]],
                layout: Dict[str, dict], body_segments=("pn", "tl")) -> str:
    out = [encode_fixed(header, layout["header"])]
    for subj in subjects:
        for code in body_segments:
            out.append(encode_var(subj, layout[code]))
        out.append("ES00")          # end of subject
    out.append("TR00")              # trailer / end of file
    return "".join(out)


# ---------------- parse ----------------
def _tagmap(layout: Dict[str, dict]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for code, seg in layout.items():
        if seg["fmt"] != "tagged":
            continue
        out[code] = {f["tag"]: f["field_key"] for f in seg["fields"]
                     if f.get("tag") and str(f["tag"]).isdigit()}
    return out


def parse_fixed(text: str, seg: dict) -> Dict[str, str]:
    row: Dict[str, str] = {}
    for f in seg["fields"]:
        s = f["position"] - 1
        row[f["field_key"]] = text[s:s + f["length"]].strip()
    return row


def parse_file(text: str, layout: Dict[str, dict]) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """Return (header_row, [subject_row, ...])."""
    tagmap = _tagmap(layout)
    total = _fixed_total(layout["header"])
    header_row = parse_fixed(text[:total], layout["header"])

    rows: List[Dict[str, str]] = []
    cur: Dict[str, str] = {}
    cur_seg = None
    pos = total
    n = len(text)
    while pos + 4 <= n:
        tag = text[pos:pos + 2]
        try:
            length = int(text[pos + 2:pos + 4])
        except ValueError:
            break
        val = text[pos + 4:pos + 4 + length]
        pos += 4 + length

        up = tag.upper()
        if up == "ES":
            if cur:
                rows.append(cur); cur = {}
            cur_seg = None
            continue
        if up == "TR":
            break
        if up in _SEG_IDS:                 # segment-tag field (PN/ID/PA/TL)
            cur_seg = up.lower()
            cur[f"{cur_seg}.segment_tag"] = val.strip()
            continue
        if cur_seg:                        # numeric field tag
            fk = tagmap.get(cur_seg, {}).get(tag)
            if fk:
                cur[fk] = val.strip()
    if cur:
        rows.append(cur)
    return header_row, rows


# ---------------- helpers ----------------
def rows_to_csv(rows: List[Dict[str, str]], columns: List[str], path: str) -> str:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow([r.get(c, "") for c in columns])
    return path
