"""Compile transform JSON (from mapping edges) into DuckDB SQL expressions.

Transforms make the two sides comparable; they are NOT judgments. A transform is
either a single ``{"op": ...}`` dict or a list of them applied left-to-right.

Supported ops mirror docs/architecture/rule-engine-spec.md §3.
"""
from __future__ import annotations
from typing import Optional, Union, List, Dict, Any

from .quoting import lit, num

Transform = Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]


def compile_transform(expr: str, transform: Transform) -> str:
    """Wrap ``expr`` (already a valid SQL expression, e.g. a quoted column) with the
    transform chain and return the resulting SQL expression."""
    if not transform:
        return expr
    ops = transform if isinstance(transform, list) else [transform]
    for op in ops:
        expr = _apply(expr, op)
    return expr


def _apply(expr: str, op: Dict[str, Any]) -> str:
    name = op.get("op")
    args = op.get("args", [])
    if name == "trim":
        return f"trim({expr})"
    if name == "upper":
        return f"upper({expr})"
    if name == "lower":
        return f"lower({expr})"
    if name == "round":
        return f"round(TRY_CAST({expr} AS DOUBLE), {int(args[0])})"
    if name == "scale":
        return f"(TRY_CAST({expr} AS DOUBLE) / {num(args[0])})"
    if name == "substr":
        return f"substr({expr}, {int(args[0])}, {int(args[1])})"
    if name == "pad_left":
        return f"lpad({expr}, {int(args[0])}, {lit(args[1])})"
    if name == "replace":
        return f"replace({expr}, {lit(op['from'])}, {lit(op['to'])})"
    if name == "null_if":
        return f"nullif({expr}, {lit(args[0])})"
    if name == "date_format":
        return f"strftime(try_strptime({expr}, {lit(op['from'])}), {lit(op['to'])})"
    raise ValueError(f"unknown transform op: {name!r}")
