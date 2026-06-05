"""Flatten a Template + Mapping (+ code lists + run params) into a ResolvedConfig
that the SQL builder can turn into a DuckDB reconciliation script.

Input JSON shapes mirror db/schema.sql:

template.json
{
  "template_id": "...", "version": "1.0.0",
  "fields": [ { "field_key": "...", "datatype": "DECIMAL" }, ... ],
  "rules":  [ { "rule_id": "...", "rule_key": "...", "type": "TOLERANCE",
               "params": {...}, "severity": "ERROR",
               "target_field_keys": ["..."] }, ... ]
}

mapping.json
{
  "mapping_id": "...", "template_version": "1.0.0", "expected_side": "SOURCE",
  "key_pairs":      [ { "field_key": "...", "src_col": "...", "dst_col": "...",
                        "src_transform": ..., "dst_transform": ... }, ... ],
  "field_bindings": [ { "field_key": "...", "src_col": "...", "dst_col": "...",
                        "src_transform": ..., "dst_transform": ... }, ... ]
}

code_lists.json
{ "NCB_ACCOUNT_STATUS": [ {"item_code": "11", "active": true}, ... ] }
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import json

SINGLE_FIELD_RULES = {
    "EQUALITY", "TOLERANCE", "RANGE", "REGEX", "NOT_NULL", "LOOKUP", "DATE_VALID",
}
BOTH_SIDE_RULES = {"EQUALITY", "TOLERANCE"}  # need src AND dst canonical values


@dataclass
class Binding:
    field_key: str
    src_col: Optional[str]
    dst_col: Optional[str]
    src_transform: Any = None
    dst_transform: Any = None


@dataclass
class Rule:
    rule_id: str
    rule_key: str
    type: str
    severity: str
    params: Dict[str, Any] = field(default_factory=dict)
    target_field_keys: List[str] = field(default_factory=list)


@dataclass
class ResolvedConfig:
    expected_side: str
    key_pairs: List[Binding]
    field_bindings: List[Binding]
    rules: List[Rule]            # single-field rules
    cross_rules: List[Rule]      # CROSS_FIELD rules
    code_lists: Dict[str, List[Dict[str, Any]]]
    as_of_date: str
    rule_engine_version: str = "rule-engine/1.0.0"

    # Derived: field_key -> Binding (deduped), per presence of a side column
    def src_fields(self) -> Dict[str, Binding]:
        out: Dict[str, Binding] = {}
        for b in list(self.key_pairs) + list(self.field_bindings):
            if b.src_col:
                out.setdefault(b.field_key, b)
        return out

    def dst_fields(self) -> Dict[str, Binding]:
        out: Dict[str, Binding] = {}
        for b in list(self.key_pairs) + list(self.field_bindings):
            if b.dst_col:
                out.setdefault(b.field_key, b)
        return out


def _binding(d: Dict[str, Any]) -> Binding:
    return Binding(
        field_key=d["field_key"],
        src_col=d.get("src_col"),
        dst_col=d.get("dst_col"),
        src_transform=d.get("src_transform"),
        dst_transform=d.get("dst_transform"),
    )


def resolve(template: Dict[str, Any], mapping: Dict[str, Any],
            code_lists: Optional[Dict[str, Any]] = None,
            as_of_date: str = "2026-06-04",
            rule_engine_version: str = "rule-engine/1.0.0") -> ResolvedConfig:
    key_pairs = [_binding(d) for d in mapping.get("key_pairs", [])]
    field_bindings = [_binding(d) for d in mapping.get("field_bindings", [])]

    rules: List[Rule] = []
    cross: List[Rule] = []
    for r in template.get("rules", []):
        rule = Rule(
            rule_id=r["rule_id"],
            rule_key=r.get("rule_key", r["rule_id"]),
            type=r["type"],
            severity=r.get("severity", "ERROR"),
            params=r.get("params", {}) or {},
            target_field_keys=r.get("target_field_keys", []),
        )
        (cross if rule.type == "CROSS_FIELD" else rules).append(rule)

    cfg = ResolvedConfig(
        expected_side=mapping.get("expected_side", "SOURCE"),
        key_pairs=key_pairs,
        field_bindings=field_bindings,
        rules=sorted(rules, key=lambda x: x.rule_id),
        cross_rules=sorted(cross, key=lambda x: x.rule_id),
        code_lists=code_lists or {},
        as_of_date=as_of_date,
        rule_engine_version=rule_engine_version,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: ResolvedConfig) -> None:
    if not cfg.key_pairs:
        raise ValueError("mapping has no key_pairs — cannot match records")
    src = cfg.src_fields()
    dst = cfg.dst_fields()

    for r in cfg.rules:
        if r.type not in SINGLE_FIELD_RULES:
            raise ValueError(f"rule {r.rule_id}: unknown single-field type {r.type}")
        if not r.target_field_keys:
            raise ValueError(f"rule {r.rule_id}: missing target_field_keys")
        k = r.target_field_keys[0]
        if k not in dst:
            raise ValueError(f"rule {r.rule_id}: field {k!r} has no destination binding")
        if r.type in BOTH_SIDE_RULES and k not in src:
            raise ValueError(f"rule {r.rule_id}: field {k!r} needs a source binding for {r.type}")
        if r.type == "LOOKUP":
            cl = r.params.get("code_list")
            if cl not in cfg.code_lists:
                raise ValueError(f"rule {r.rule_id}: code_list {cl!r} not provided")

    for r in cfg.cross_rules:
        for p in ("lhs", "rhs", "tolerance"):
            if p not in r.params:
                raise ValueError(f"cross rule {r.rule_id}: missing param {p!r}")


# ---- convenience loaders -------------------------------------------------
def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
