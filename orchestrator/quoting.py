"""SQL quoting / literal helpers.

All identifiers and string literals that originate from template/mapping config
MUST pass through here before being embedded in generated SQL. This is the single
choke point that prevents SQL injection from field names, column names, patterns,
code-list values, etc.
"""
from __future__ import annotations
import re


def ident(name: str) -> str:
    """Quote a SQL identifier (column/table)."""
    s = str(name)
    if '"' in s:
        s = s.replace('"', '""')
    return f'"{s}"'


def lit(value) -> str:
    """Quote a string literal (single-quote escaped)."""
    return "'" + str(value).replace("'", "''") + "'"


def num(value) -> str:
    """Emit a numeric literal, rejecting anything that is not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a number, got {value!r}")
    return repr(value)


def boolsql(value) -> str:
    return "TRUE" if value else "FALSE"


def safe_name(name: str) -> str:
    """Sanitize to a bare identifier (letters/digits/underscore) for temp tables."""
    s = re.sub(r"\W", "_", str(name))
    if not s or s[0].isdigit():
        s = "_" + s
    return s


# Whitelist for user-authored CROSS_FIELD expressions: field tokens f_xxx,
# numbers, arithmetic operators, parentheses, dots and spaces. Nothing else.
_EXPR_OK = re.compile(r"^[A-Za-z0-9_+\-*/().,\s]+$")


def check_expr(expr: str) -> str:
    """Validate a CROSS_FIELD arithmetic expression. Raises on anything suspicious."""
    s = str(expr)
    if not _EXPR_OK.match(s):
        raise ValueError(f"unsafe expression: {expr!r}")
    lowered = s.lower()
    for bad in ("select", "insert", "update", "delete", "drop", ";", "--", "/*"):
        if bad in lowered:
            raise ValueError(f"unsafe expression contains {bad!r}: {expr!r}")
    return s
