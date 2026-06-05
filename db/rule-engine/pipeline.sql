-- =====================================================================
-- Rule Engine — Reconciliation Pipeline scaffold (DuckDB)
-- engine version: rule-engine/1.0.0
--
-- This is the ORCHESTRATION skeleton. The engine generates the concrete
-- per-field SELECTs (Step 6) from db/rule-engine/rule_templates.sql by
-- substituting each compared field's {{placeholders}}.
--
-- Placeholders used here (filled by the orchestrator from the resolved config):
--   {{src_csv}} / {{dst_csv}}     paths or staging refs of the two datasets
--   {{key_src_expr}} / {{key_dst_expr}}   key expression(s) per side (transforms applied)
--   {{canon_src_cols}} / {{canon_dst_cols}}  canonical f_<field> projections
--   {{field_eval_union}}          UNION ALL of generated per-field rule SELECTs
--   {{cross_field_union}}         UNION ALL of generated CROSS_FIELD SELECTs (optional)
-- =====================================================================

-- ---------------------------------------------------------------------
-- Step 1 — STAGE (load raw rows). CSV shown; Excel via the 'excel' extension.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE src_raw AS
  SELECT * FROM read_csv_auto('{{src_csv}}', header = true, all_varchar = true);
CREATE OR REPLACE TEMP TABLE dst_raw AS
  SELECT * FROM read_csv_auto('{{dst_csv}}', header = true, all_varchar = true);
-- all_varchar = true keeps every value as text; the engine casts explicitly in rules
-- so cast failures surface as EXCEPTION rather than silent load-time coercion.

-- ---------------------------------------------------------------------
-- Step 2 — CANONIZE (apply transforms, build record_key + f_<field> columns)
-- ---------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE src_keyed AS
  SELECT
    concat_ws('|', {{key_src_expr}})  AS record_key,
    {{canon_src_cols}}                                 -- e.g.  trim("outstanding") AS f_outstanding_balance, ...
  FROM src_raw;

CREATE OR REPLACE TEMP TABLE dst_keyed AS
  SELECT
    concat_ws('|', {{key_dst_expr}})  AS record_key,
    {{canon_dst_cols}}                                 -- e.g.  round(TRY_CAST("OUTSTANDING_BAL" AS DOUBLE),2) AS f_outstanding_balance, ...
  FROM dst_raw;

-- ---------------------------------------------------------------------
-- Step 3 — DUPLICATES (keys appearing more than once on either side)
-- ---------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE dup_keys AS
  SELECT record_key, 'SOURCE' AS side, COUNT(*) AS cnt
    FROM src_keyed GROUP BY record_key HAVING COUNT(*) > 1
  UNION ALL
  SELECT record_key, 'DEST', COUNT(*)
    FROM dst_keyed GROUP BY record_key HAVING COUNT(*) > 1;

-- Unique (non-duplicate) keyed rows, safe to 1:1 compare
CREATE OR REPLACE TEMP TABLE src_u AS
  SELECT * FROM src_keyed
   WHERE record_key NOT IN (SELECT record_key FROM dup_keys WHERE side = 'SOURCE');
CREATE OR REPLACE TEMP TABLE dst_u AS
  SELECT * FROM dst_keyed
   WHERE record_key NOT IN (SELECT record_key FROM dup_keys WHERE side = 'DEST');

-- ---------------------------------------------------------------------
-- Step 4 — MATCHED (unique keys present on BOTH sides). Wide row for CROSS_FIELD.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE matched_wide AS
  SELECT s.record_key,
         s.* EXCLUDE (record_key),                     -- f_*  from source  (alias s_* if needed)
         d.* EXCLUDE (record_key)                      -- f_*  from dest
  FROM src_u s JOIN dst_u d USING (record_key);
-- For per-field rules the engine builds `matched` views exposing {{src}} and {{dst}}
-- canonical expressions; conceptually:
--   matched(record_key, <src f_field>, <dst f_field>)

-- ---------------------------------------------------------------------
-- Step 5 — MISSING (anti-joins) → record-level result rows
-- Computed from FULL presence (src_keyed/dst_keyed), NOT the unique sets,
-- so a key duplicated on one side is classified DUPLICATE, not MISSING.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE rec_missing AS
  SELECT record_key, 'MISSING_IN_DEST'   AS category
    FROM (SELECT DISTINCT record_key FROM src_keyed)
   WHERE record_key NOT IN (SELECT DISTINCT record_key FROM dst_keyed)
  UNION ALL
  SELECT record_key, 'MISSING_IN_SOURCE'
    FROM (SELECT DISTINCT record_key FROM dst_keyed)
   WHERE record_key NOT IN (SELECT DISTINCT record_key FROM src_keyed);

-- ---------------------------------------------------------------------
-- Step 6 — FIELD EVALUATION (generated UNION ALL of per-field rule SELECTs)
--          Each member comes from rule_templates.sql for that field's rule_type.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE field_results AS
{{field_eval_union}}
{{cross_field_union}}   -- prefixed with "UNION ALL" by the generator, or empty
;

-- ---------------------------------------------------------------------
-- Step 7 — RECORD-LEVEL ROLLUP (priority: missing > dup > exception > mismatch > match)
-- ---------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE record_rollup AS
WITH base AS (   -- all distinct keys seen on either side (incl. duplicated keys)
  SELECT DISTINCT record_key FROM src_keyed
  UNION SELECT DISTINCT record_key FROM dst_keyed
),
agg AS (
  SELECT record_key,
         BOOL_OR(category = 'EXCEPTION')                              AS has_exc,
         BOOL_OR(category = 'MISMATCH' AND severity = 'ERROR')        AS has_err_mismatch
  FROM field_results GROUP BY record_key
)
SELECT b.record_key,
  CASE
    WHEN m.category IS NOT NULL                THEN m.category               -- MISSING_*
    WHEN dk.record_key IS NOT NULL             THEN 'DUPLICATE'
    WHEN COALESCE(a.has_exc, false)            THEN 'EXCEPTION'
    WHEN COALESCE(a.has_err_mismatch, false)   THEN 'MISMATCH'
    ELSE 'MATCH'
  END AS category
FROM base b
LEFT JOIN rec_missing m USING (record_key)
LEFT JOIN (SELECT DISTINCT record_key FROM dup_keys) dk USING (record_key)
LEFT JOIN agg a USING (record_key);

-- ---------------------------------------------------------------------
-- Step 8 — SUMMARY (headline counts; by_field/by_severity built by the app from field_results)
-- ---------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE run_summary AS
SELECT
  COUNT(*)                                            AS total_records,
  COUNT(*) FILTER (WHERE category = 'MATCH')          AS match_count,
  COUNT(*) FILTER (WHERE category = 'MISMATCH')       AS mismatch_count,
  COUNT(*) FILTER (WHERE category = 'MISSING_IN_SOURCE') AS missing_src,
  COUNT(*) FILTER (WHERE category = 'MISSING_IN_DEST')   AS missing_dst,
  COUNT(*) FILTER (WHERE category = 'DUPLICATE')      AS duplicate_count,
  COUNT(*) FILTER (WHERE category = 'EXCEPTION')      AS exception_count
FROM record_rollup;

-- The orchestrator then:
--   * reads field_results (drill-down) + record_rollup + run_summary
--   * writes them to Postgres (recon_results partition + recon_summary)
--   * stamps rule_engine_version on the run.
