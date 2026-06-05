"""Assemble the full DuckDB reconciliation script from a ResolvedConfig.

Produces the setup SQL (everything up to and including `field_results`,
`record_rollup`, `run_summary`). The engine executes it, then reads the result
tables back.
"""
from __future__ import annotations
from typing import List

from .resolver import ResolvedConfig
from .transforms import compile_transform
from .quoting import ident, lit, safe_name, boolsql
from .rules import build_field_eval_union


def _canon_projection(fields, col_attr, tf_attr) -> List[str]:
    """Return ['<expr> AS f_<key>', ...] for one side."""
    out = []
    for key, b in fields.items():
        col = getattr(b, col_attr)
        expr = compile_transform(ident(col), getattr(b, tf_attr))
        out.append(f"{expr} AS f_{safe_name(key)}")
    return out


def _key_exprs(cfg: ResolvedConfig, side: str) -> List[str]:
    col_attr = "src_col" if side == "src" else "dst_col"
    tf_attr = "src_transform" if side == "src" else "dst_transform"
    out = []
    for kp in cfg.key_pairs:
        col = getattr(kp, col_attr)
        expr = compile_transform(ident(col), getattr(kp, tf_attr))
        out.append(f"CAST({expr} AS VARCHAR)")
    return out


def build_code_list_tables(cfg: ResolvedConfig) -> str:
    stmts = []
    for name, items in cfg.code_lists.items():
        tbl = f"cl_{safe_name(name)}"
        rows = [f"({lit(it['item_code'])}, {boolsql(it.get('active', True))})"
                for it in items]
        if rows:
            values = ", ".join(rows)
            stmts.append(
                f"CREATE OR REPLACE TEMP TABLE {tbl} AS "
                f"SELECT * FROM (VALUES {values}) AS t(item_code, active);")
        else:
            stmts.append(
                f"CREATE OR REPLACE TEMP TABLE {tbl} AS "
                f"SELECT NULL::VARCHAR AS item_code, NULL::BOOLEAN AS active WHERE FALSE;")
    return "\n".join(stmts)


def build_setup_sql(cfg: ResolvedConfig, src_csv: str, dst_csv: str) -> str:
    src_fields = cfg.src_fields()
    dst_fields = cfg.dst_fields()

    src_proj = ",\n       ".join(_canon_projection(src_fields, "src_col", "src_transform"))
    dst_proj = ",\n       ".join(_canon_projection(dst_fields, "dst_col", "dst_transform"))
    src_key = ", ".join(_key_exprs(cfg, "src"))
    dst_key = ", ".join(_key_exprs(cfg, "dst"))

    s_cols = ", ".join(f"s.f_{safe_name(k)} AS s_{safe_name(k)}" for k in src_fields)
    d_cols = ", ".join(f"d.f_{safe_name(k)} AS d_{safe_name(k)}" for k in dst_fields)
    wide_cols = ", ".join(f"d_{safe_name(k)} AS f_{safe_name(k)}" for k in dst_fields)

    field_union = build_field_eval_union(cfg)
    code_lists = build_code_list_tables(cfg)

    return f"""
-- ===== Step 1: STAGE =====
CREATE OR REPLACE TEMP TABLE src_raw AS
  SELECT * FROM read_csv_auto({lit(src_csv)}, header=true, all_varchar=true);
CREATE OR REPLACE TEMP TABLE dst_raw AS
  SELECT * FROM read_csv_auto({lit(dst_csv)}, header=true, all_varchar=true);

-- ===== Step 2: CANONIZE =====
CREATE OR REPLACE TEMP TABLE src_keyed AS
  SELECT concat_ws('|', {src_key}) AS record_key,
       {src_proj}
  FROM src_raw;
CREATE OR REPLACE TEMP TABLE dst_keyed AS
  SELECT concat_ws('|', {dst_key}) AS record_key,
       {dst_proj}
  FROM dst_raw;

-- ===== Step 3: DUPLICATES =====
CREATE OR REPLACE TEMP TABLE dup_keys AS
  SELECT record_key, 'SOURCE' AS side FROM src_keyed GROUP BY record_key HAVING COUNT(*) > 1
  UNION ALL
  SELECT record_key, 'DEST'   FROM dst_keyed GROUP BY record_key HAVING COUNT(*) > 1;
CREATE OR REPLACE TEMP TABLE src_u AS
  SELECT * FROM src_keyed WHERE record_key NOT IN (SELECT record_key FROM dup_keys WHERE side='SOURCE');
CREATE OR REPLACE TEMP TABLE dst_u AS
  SELECT * FROM dst_keyed WHERE record_key NOT IN (SELECT record_key FROM dup_keys WHERE side='DEST');

-- ===== Step 4: MATCHED =====
CREATE OR REPLACE TEMP TABLE matched AS
  SELECT s.record_key, {s_cols}, {d_cols}
  FROM src_u s JOIN dst_u d USING (record_key);
CREATE OR REPLACE TEMP TABLE matched_wide AS
  SELECT record_key, {wide_cols} FROM matched;

-- ===== Step 5: MISSING (full presence) =====
CREATE OR REPLACE TEMP TABLE rec_missing AS
  SELECT record_key, 'MISSING_IN_DEST' AS category
    FROM (SELECT DISTINCT record_key FROM src_keyed)
   WHERE record_key NOT IN (SELECT DISTINCT record_key FROM dst_keyed)
  UNION ALL
  SELECT record_key, 'MISSING_IN_SOURCE'
    FROM (SELECT DISTINCT record_key FROM dst_keyed)
   WHERE record_key NOT IN (SELECT DISTINCT record_key FROM src_keyed);

-- ===== Code lists for LOOKUP =====
{code_lists}

-- ===== Step 6: FIELD EVALUATION =====
CREATE OR REPLACE TEMP TABLE field_results AS
{field_union};

-- ===== Step 7: RECORD ROLLUP =====
CREATE OR REPLACE TEMP TABLE record_rollup AS
WITH base AS (
  SELECT DISTINCT record_key FROM src_keyed
  UNION SELECT DISTINCT record_key FROM dst_keyed
),
agg AS (
  SELECT record_key,
         BOOL_OR(category='EXCEPTION') AS has_exc,
         BOOL_OR(category='MISMATCH' AND severity='ERROR') AS has_err_mismatch
  FROM field_results GROUP BY record_key
)
SELECT b.record_key,
  CASE WHEN m.category IS NOT NULL THEN m.category
       WHEN dk.record_key IS NOT NULL THEN 'DUPLICATE'
       WHEN COALESCE(a.has_exc, false) THEN 'EXCEPTION'
       WHEN COALESCE(a.has_err_mismatch, false) THEN 'MISMATCH'
       ELSE 'MATCH' END AS category
FROM base b
LEFT JOIN rec_missing m USING (record_key)
LEFT JOIN (SELECT DISTINCT record_key FROM dup_keys) dk USING (record_key)
LEFT JOIN agg a USING (record_key);

-- ===== Step 8: SUMMARY =====
CREATE OR REPLACE TEMP TABLE run_summary AS
SELECT
  COUNT(*) AS total_records,
  COUNT(*) FILTER (WHERE category='MATCH') AS match_count,
  COUNT(*) FILTER (WHERE category='MISMATCH') AS mismatch_count,
  COUNT(*) FILTER (WHERE category='MISSING_IN_SOURCE') AS missing_src,
  COUNT(*) FILTER (WHERE category='MISSING_IN_DEST') AS missing_dst,
  COUNT(*) FILTER (WHERE category='DUPLICATE') AS duplicate_count,
  COUNT(*) FILTER (WHERE category='EXCEPTION') AS exception_count
FROM record_rollup;
"""
