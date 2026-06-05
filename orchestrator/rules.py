"""Generate the per-rule DuckDB SELECT for each rule type.

Every generated SELECT emits exactly this column contract (8 columns, in order):
    record_key, field_key, rule_id, severity, expected, actual, category, verdict

Single-field rules read from the `matched` table, which exposes, for every bound
field key ``k``: ``s_k`` (source canonical value) and ``d_k`` (destination value).
CROSS_FIELD rules read from `matched_wide`, which exposes destination canonical
values as ``f_k``.

These builders are the executable mirror of db/rule-engine/rule_templates.sql.
"""
from __future__ import annotations
import re
from typing import List

from .resolver import Rule, ResolvedConfig
from .quoting import lit, num, safe_name, check_expr

# Wrap every f_<field> token in a CROSS_FIELD expression with a numeric cast, so
# arithmetic works on canonical VARCHAR columns. Non-numeric values -> NULL -> EXCEPTION.
_FIELD_TOKEN = re.compile(r"\bf_[A-Za-z0-9_]+\b")


def _numeric_expr(expr: str) -> str:
    return _FIELD_TOKEN.sub(r"TRY_CAST(\g<0> AS DECIMAL(18,4))", expr)


def _select(field_key_sql: str, rule_id_sql: str, severity_sql: str,
            expected: str, actual: str, category: str, verdict: str,
            from_clause: str, record_key: str = "record_key") -> str:
    return (
        "SELECT\n"
        f"  {record_key} AS record_key,\n"
        f"  {field_key_sql} AS field_key,\n"
        f"  {rule_id_sql} AS rule_id,\n"
        f"  {severity_sql} AS severity,\n"
        f"  {expected} AS expected,\n"
        f"  {actual} AS actual,\n"
        f"  {category} AS category,\n"
        f"  {verdict} AS verdict\n"
        f"{from_clause}"
    )


def build_single_rule_sql(rule: Rule) -> str:
    k = rule.target_field_keys[0]
    s, d = f"s_{safe_name(k)}", f"d_{safe_name(k)}"
    fk, rid, sev = lit(k), lit(rule.rule_id), lit(rule.severity)
    p = rule.params

    if rule.type == "EQUALITY":
        eq = f"{s} IS NOT DISTINCT FROM {d}"
        return _select(
            fk, rid, sev,
            f"CAST({s} AS VARCHAR)", f"CAST({d} AS VARCHAR)",
            f"CASE WHEN {eq} THEN 'MATCH' ELSE 'MISMATCH' END",
            f"CASE WHEN {eq} THEN 'equal' ELSE 'not_equal' END",
            "FROM matched")

    if rule.type == "TOLERANCE":
        scale = int(p.get("scale", 6))
        tol = num(p["tolerance"])
        cs = f"TRY_CAST({s} AS DECIMAL(38,{scale}))"
        cd = f"TRY_CAST({d} AS DECIMAL(38,{scale}))"
        null = f"{cs} IS NULL OR {cd} IS NULL"
        return _select(
            fk, rid, sev,
            f"CAST({s} AS VARCHAR)", f"CAST({d} AS VARCHAR)",
            f"CASE WHEN {null} THEN 'EXCEPTION' "
            f"WHEN abs({cs} - {cd}) <= {tol} THEN 'MATCH' ELSE 'MISMATCH' END",
            f"CASE WHEN {null} THEN 'uncastable' "
            f"ELSE 'diff=' || CAST(abs({cs} - {cd}) AS VARCHAR) END",
            "FROM matched")

    if rule.type == "RANGE":
        lo, hi = num(p["min"]), num(p["max"])
        cd = f"TRY_CAST({d} AS DOUBLE)"
        return _select(
            fk, rid, sev,
            lit(f"[{p['min']},{p['max']}]"), f"CAST({d} AS VARCHAR)",
            f"CASE WHEN {cd} IS NULL THEN 'EXCEPTION' "
            f"WHEN {cd} BETWEEN {lo} AND {hi} THEN 'MATCH' ELSE 'MISMATCH' END",
            f"CASE WHEN {cd} IS NULL THEN 'uncastable' "
            f"WHEN {cd} BETWEEN {lo} AND {hi} THEN 'in_range' ELSE 'out_of_range' END",
            "FROM matched")

    if rule.type == "REGEX":
        pat = lit(p["pattern"])
        m = f"regexp_full_match(CAST({d} AS VARCHAR), {pat})"
        return _select(
            fk, rid, sev,
            pat, f"CAST({d} AS VARCHAR)",
            f"CASE WHEN {d} IS NULL THEN 'MISMATCH' WHEN {m} THEN 'MATCH' ELSE 'MISMATCH' END",
            f"CASE WHEN {d} IS NULL THEN 'empty' WHEN {m} THEN 'format_ok' ELSE 'format_bad' END",
            "FROM matched")

    if rule.type == "NOT_NULL":
        blank = f"{d} IS NULL OR trim(CAST({d} AS VARCHAR)) = ''"
        return _select(
            fk, rid, sev,
            lit("NOT_NULL"), f"CAST({d} AS VARCHAR)",
            f"CASE WHEN {blank} THEN 'MISMATCH' ELSE 'MATCH' END",
            f"CASE WHEN {blank} THEN 'missing' ELSE 'present' END",
            "FROM matched")

    if rule.type == "LOOKUP":
        cl = safe_name(p["code_list"])
        join = (f"FROM matched LEFT JOIN cl_{cl} cl "
                f"ON cl.item_code = CAST({d} AS VARCHAR) AND cl.active")
        return _select(
            lit(k), rid, sev,
            lit(f"in:{p['code_list']}"), f"CAST({d} AS VARCHAR)",
            f"CASE WHEN {d} IS NULL THEN 'MISMATCH' "
            f"WHEN cl.item_code IS NOT NULL THEN 'MATCH' ELSE 'MISMATCH' END",
            f"CASE WHEN {d} IS NULL THEN 'empty' "
            f"WHEN cl.item_code IS NOT NULL THEN 'found' ELSE 'not_in_list' END",
            join)

    if rule.type == "DATE_VALID":
        fmt = lit(p.get("format", "%Y-%m-%d"))
        not_future = bool(p.get("not_future", False))
        as_of = p["__as_of_date__"]  # injected by builder
        parsed = f"try_strptime(CAST({d} AS VARCHAR), {fmt})"
        future = (f"{parsed}::DATE > {as_of}") if not_future else "FALSE"
        return _select(
            lit(k), rid, sev,
            lit(f"valid_date({p.get('format', '%Y-%m-%d')})"), f"CAST({d} AS VARCHAR)",
            f"CASE WHEN {parsed} IS NULL THEN 'MISMATCH' "
            f"WHEN {future} THEN 'MISMATCH' ELSE 'MATCH' END",
            f"CASE WHEN {parsed} IS NULL THEN 'unparseable' "
            f"WHEN {future} THEN 'future_date' ELSE 'valid' END",
            "FROM matched")

    raise ValueError(f"unsupported single rule type {rule.type}")


def build_cross_rule_sql(rule: Rule) -> str:
    lhs = check_expr(rule.params["lhs"])
    rhs = check_expr(rule.params["rhs"])
    tol = num(rule.params["tolerance"])
    clhs = f"({_numeric_expr(lhs)})"
    crhs = f"({_numeric_expr(rhs)})"
    null = f"{clhs} IS NULL OR {crhs} IS NULL"
    return _select(
        lit(f"cross:{rule.rule_key}"), lit(rule.rule_id), lit(rule.severity),
        f"CAST({crhs} AS VARCHAR)", f"CAST({clhs} AS VARCHAR)",
        f"CASE WHEN {null} THEN 'EXCEPTION' "
        f"WHEN abs({clhs} - {crhs}) <= {tol} THEN 'MATCH' ELSE 'MISMATCH' END",
        f"CASE WHEN {null} THEN 'uncastable' "
        f"ELSE 'diff=' || CAST(abs({clhs} - {crhs}) AS VARCHAR) END",
        "FROM matched_wide")


def build_field_eval_union(cfg: ResolvedConfig) -> str:
    members: List[str] = []
    for r in cfg.rules:
        if r.type == "DATE_VALID":
            r.params = {**r.params, "__as_of_date__": f"DATE {lit(cfg.as_of_date)}"}
        members.append(build_single_rule_sql(r))
    for r in cfg.cross_rules:
        members.append(build_cross_rule_sql(r))
    if not members:
        raise ValueError("no rules to evaluate")
    return "\nUNION ALL\n".join(members)
