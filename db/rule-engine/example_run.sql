-- =====================================================================
-- Rule Engine — WORKED EXAMPLE (fully concrete, no placeholders)
-- Run:  duckdb < example_run.sql      (or python: duckdb.sql(open(...).read()))
--
-- Demonstrates all 8 rule types + key matching + missing + duplicate + rollup,
-- using a tiny inline NCB-style motorcycle hire-purchase dataset.
-- =====================================================================

-- ---- Inline SOURCE (system of record) -------------------------------
CREATE OR REPLACE TEMP TABLE src_raw AS
SELECT * FROM (VALUES
  --  ACCNO   IDCARD          OUTSTANDING  STATUS  RATE  OPENDATE     TERM  INSTALL  TOTAL
  ('A001', '1100000000001', '50000.00', '11', '24', '2023-01-15', '12', '4500', '54000'),
  ('A002', '1100000000002', '12000',    '13', '18', '2022-06-30', '24', '600',  '14400'),
  ('A003', '1100000000003', '0',        '20', '10', '2024-12-01', '36', '0',    '0'),
  ('A004', '1100000000004', '9999.99',  '11', '15', '2021-03-03', '12', '900',  '10800'),
  ('A005', '1100000000005', '7000',     '11', '20', '2020-05-05', '6',  '1200', '7200'),  -- src-only (missing in dest)
  ('A007', '1100000000007', '1000',     '11', '10', '2023-01-01', '12', '100',  '1200')   -- pair for EXCEPTION demo
) AS t(ACCNO, IDCARD, OUTSTANDING, STATUS, RATE, OPENDATE, TERM, INSTALL, TOTAL);

-- ---- Inline DESTINATION (being verified) ----------------------------
CREATE OR REPLACE TEMP TABLE dst_raw AS
SELECT * FROM (VALUES
  ('A001', '1100000000001', '50000.01', '11', '24', '2023-01-15', '12', '4500', '54000'), -- bal within tolerance
  ('A002', '1100000000002', '12000',    '99', '18', '2022-06-30', '24', '600',  '14400'), -- STATUS 99 not in code list
  ('A003', '1100000000003', '0',        '20', '99', '2024-12-01', '36', '0',    '0'),     -- RATE 99 out of range
  ('A004', '1100000000004', 'NaN',      '11', '15', '2021-13-40', '12', '900',  '99999'), -- bad number, bad date, cross-field fail
  ('A006', '1100000000006', '3000',     '11', '12', '2025-01-01', '6',  '500',  '3000'),  -- dest-only (missing in source)
  ('A004', '1100000000004', '8888',     '11', '15', '2021-03-03', '12', '900',  '10800'), -- duplicate key A004 in dest
  ('A007', '1100000000007', 'ABC',      '11', '10', '2023-01-01', '12', '100',  '1200')   -- 'ABC' uncastable -> TOLERANCE EXCEPTION
) AS t(ACCNO, IDCARD, OUTSTANDING, STATUS, RATE, OPENDATE, TERM, INSTALL, TOTAL);

-- ---- Code list for LOOKUP (loaded from Postgres in production) -------
CREATE OR REPLACE TEMP TABLE cl_NCB_ACCOUNT_STATUS AS
SELECT * FROM (VALUES ('11', true), ('13', true), ('20', true), ('30', false))
AS t(item_code, active);

-- =====================================================================
-- Step 2 — CANONIZE: build record_key + canonical f_<field> columns
-- key = ACCNO|IDCARD ; transforms: trim, round(2), etc.
-- =====================================================================
CREATE OR REPLACE TEMP TABLE src_keyed AS
SELECT concat_ws('|', trim(ACCNO), trim(IDCARD))        AS record_key,
       round(TRY_CAST(OUTSTANDING AS DOUBLE), 2)        AS f_outstanding_balance,
       trim(STATUS)                                     AS f_status,
       TRY_CAST(RATE AS DOUBLE)                         AS f_rate,
       trim(OPENDATE)                                   AS f_open_date,
       trim(IDCARD)                                     AS f_id_card,
       TRY_CAST(TERM AS DOUBLE)                         AS f_term,
       TRY_CAST(INSTALL AS DOUBLE)                      AS f_install,
       TRY_CAST(TOTAL AS DOUBLE)                        AS f_total
FROM src_raw;

CREATE OR REPLACE TEMP TABLE dst_keyed AS
SELECT concat_ws('|', trim(ACCNO), trim(IDCARD))        AS record_key,
       round(TRY_CAST(OUTSTANDING AS DOUBLE), 2)        AS f_outstanding_balance,
       trim(STATUS)                                     AS f_status,
       TRY_CAST(RATE AS DOUBLE)                         AS f_rate,
       trim(OPENDATE)                                   AS f_open_date,
       trim(IDCARD)                                     AS f_id_card,
       TRY_CAST(TERM AS DOUBLE)                         AS f_term,
       TRY_CAST(INSTALL AS DOUBLE)                      AS f_install,
       TRY_CAST(TOTAL AS DOUBLE)                        AS f_total,
       OUTSTANDING                                      AS raw_outstanding  -- keep raw for cast-fail demo
FROM dst_raw;

-- =====================================================================
-- Step 3 — DUPLICATES
-- =====================================================================
CREATE OR REPLACE TEMP TABLE dup_keys AS
  SELECT record_key, 'SOURCE' side FROM src_keyed GROUP BY record_key HAVING COUNT(*)>1
  UNION ALL
  SELECT record_key, 'DEST'   FROM dst_keyed GROUP BY record_key HAVING COUNT(*)>1;

CREATE OR REPLACE TEMP TABLE src_u AS
  SELECT * FROM src_keyed WHERE record_key NOT IN (SELECT record_key FROM dup_keys WHERE side='SOURCE');
CREATE OR REPLACE TEMP TABLE dst_u AS
  SELECT * FROM dst_keyed WHERE record_key NOT IN (SELECT record_key FROM dup_keys WHERE side='DEST');

-- =====================================================================
-- Step 4 — MATCHED view exposing src/dst canonical values per field
-- =====================================================================
CREATE OR REPLACE TEMP TABLE matched AS
SELECT s.record_key,
       s.f_outstanding_balance AS s_bal,  d.f_outstanding_balance AS d_bal,
       s.f_status  AS s_status,           d.f_status  AS d_status,
       s.f_rate    AS s_rate,             d.f_rate    AS d_rate,
       s.f_open_date AS s_open,           d.f_open_date AS d_open,
       s.f_id_card AS s_idcard,           d.f_id_card AS d_idcard,
       d.raw_outstanding AS d_bal_raw,
       d.f_term AS d_term, d.f_install AS d_install, d.f_total AS d_total
FROM src_u s JOIN dst_u d USING (record_key);

CREATE OR REPLACE TEMP TABLE matched_wide AS SELECT * FROM matched;

-- =====================================================================
-- Step 5 — MISSING
-- =====================================================================
-- Missing is computed from FULL presence (src_keyed/dst_keyed), not the unique sets,
-- so a key that is merely duplicated on one side is NOT misread as "missing".
CREATE OR REPLACE TEMP TABLE rec_missing AS
  SELECT record_key, 'MISSING_IN_DEST' category
    FROM (SELECT DISTINCT record_key FROM src_keyed)
   WHERE record_key NOT IN (SELECT DISTINCT record_key FROM dst_keyed)
  UNION ALL
  SELECT record_key, 'MISSING_IN_SOURCE'
    FROM (SELECT DISTINCT record_key FROM dst_keyed)
   WHERE record_key NOT IN (SELECT DISTINCT record_key FROM src_keyed);

-- =====================================================================
-- Step 6 — FIELD EVALUATION (one generated SELECT per rule, UNION ALL)
-- =====================================================================
CREATE OR REPLACE TEMP TABLE field_results AS
-- (1) EQUALITY on id_card
SELECT record_key, 'id_card' field_key, 'r_eq' rule_id, 'ERROR' severity,
  CAST(s_idcard AS VARCHAR) expected, CAST(d_idcard AS VARCHAR) actual,
  CASE WHEN s_idcard IS NOT DISTINCT FROM d_idcard THEN 'MATCH' ELSE 'MISMATCH' END category,
  CASE WHEN s_idcard IS NOT DISTINCT FROM d_idcard THEN 'equal' ELSE 'not_equal' END verdict
FROM matched
UNION ALL
-- (2) TOLERANCE on balance (+-0.01). DECIMAL exact arithmetic (money != float!).
--     A004 dst is 'NaN' -> uncastable -> EXCEPTION
SELECT record_key, 'outstanding_balance', 'r_tol', 'ERROR',
  CAST(s_bal AS VARCHAR), CAST(d_bal_raw AS VARCHAR),
  CASE WHEN TRY_CAST(s_bal AS DECIMAL(18,4)) IS NULL OR TRY_CAST(d_bal_raw AS DECIMAL(18,4)) IS NULL THEN 'EXCEPTION'
       WHEN abs(TRY_CAST(s_bal AS DECIMAL(18,4)) - TRY_CAST(d_bal_raw AS DECIMAL(18,4))) <= 0.01 THEN 'MATCH' ELSE 'MISMATCH' END,
  CASE WHEN TRY_CAST(s_bal AS DECIMAL(18,4)) IS NULL OR TRY_CAST(d_bal_raw AS DECIMAL(18,4)) IS NULL THEN 'uncastable'
       ELSE 'diff=' || CAST(abs(TRY_CAST(s_bal AS DECIMAL(18,4)) - TRY_CAST(d_bal_raw AS DECIMAL(18,4))) AS VARCHAR) END
FROM matched
UNION ALL
-- (3) RANGE on rate [0,36]. A003 dst rate 99 -> out of range
SELECT record_key, 'rate', 'r_rng', 'WARNING',
  '[0,36]', CAST(d_rate AS VARCHAR),
  CASE WHEN d_rate IS NULL THEN 'EXCEPTION'
       WHEN d_rate BETWEEN 0 AND 36 THEN 'MATCH' ELSE 'MISMATCH' END,
  CASE WHEN d_rate IS NULL THEN 'uncastable'
       WHEN d_rate BETWEEN 0 AND 36 THEN 'in_range' ELSE 'out_of_range' END
FROM matched
UNION ALL
-- (4) REGEX id_card must be 13 digits
SELECT record_key, 'id_card_fmt', 'r_rgx', 'ERROR',
  '^[0-9]{13}$', CAST(d_idcard AS VARCHAR),
  CASE WHEN d_idcard IS NULL THEN 'MISMATCH'
       WHEN regexp_full_match(CAST(d_idcard AS VARCHAR), '^[0-9]{13}$') THEN 'MATCH' ELSE 'MISMATCH' END,
  'regex'
FROM matched
UNION ALL
-- (5) NOT_NULL on status
SELECT record_key, 'status_present', 'r_nn', 'ERROR',
  'NOT_NULL', CAST(d_status AS VARCHAR),
  CASE WHEN d_status IS NULL OR trim(d_status)='' THEN 'MISMATCH' ELSE 'MATCH' END,
  'presence'
FROM matched
UNION ALL
-- (6) LOOKUP status in code list. A002 dst status 99 -> not in list
SELECT m.record_key, 'status_code', 'r_lk', 'ERROR',
  'in:NCB_ACCOUNT_STATUS', CAST(d_status AS VARCHAR),
  CASE WHEN d_status IS NULL THEN 'MISMATCH'
       WHEN cl.item_code IS NOT NULL THEN 'MATCH' ELSE 'MISMATCH' END,
  CASE WHEN cl.item_code IS NOT NULL THEN 'found' ELSE 'not_in_list' END
FROM matched m LEFT JOIN cl_NCB_ACCOUNT_STATUS cl ON cl.item_code = d_status AND cl.active
UNION ALL
-- (7) DATE_VALID open_date not future. Uses a FIXED as_of date (run param),
--     never the machine clock -> deterministic + immune to server locale (e.g. Thai BE).
SELECT record_key, 'open_date', 'r_dt', 'ERROR',
  'valid_date(%Y-%m-%d)', CAST(d_open AS VARCHAR),
  CASE WHEN try_strptime(d_open, '%Y-%m-%d') IS NULL THEN 'MISMATCH'
       WHEN try_strptime(d_open, '%Y-%m-%d')::DATE > DATE '2026-06-04' THEN 'MISMATCH'
       ELSE 'MATCH' END,
  CASE WHEN try_strptime(d_open, '%Y-%m-%d') IS NULL THEN 'unparseable'
       WHEN try_strptime(d_open, '%Y-%m-%d')::DATE > DATE '2026-06-04' THEN 'future_date'
       ELSE 'valid' END
FROM matched
UNION ALL
-- (8) CROSS_FIELD term*install ~= total (+-1). A004 dst 12*900=10800 vs total 99999 -> mismatch
SELECT record_key, 'cross:term_x_install', 'r_cf', 'WARNING',
  CAST(d_total AS VARCHAR), CAST(d_term*d_install AS VARCHAR),
  CASE WHEN d_term IS NULL OR d_install IS NULL OR d_total IS NULL THEN 'EXCEPTION'
       WHEN abs(d_term*d_install - d_total) <= 1 THEN 'MATCH' ELSE 'MISMATCH' END,
  CASE WHEN d_term IS NULL OR d_install IS NULL OR d_total IS NULL THEN 'uncastable'
       ELSE 'diff=' || CAST(abs(d_term*d_install - d_total) AS VARCHAR) END
FROM matched_wide;

-- =====================================================================
-- Step 7 — RECORD ROLLUP
-- =====================================================================
CREATE OR REPLACE TEMP TABLE record_rollup AS
WITH base AS (SELECT DISTINCT record_key FROM src_keyed UNION SELECT DISTINCT record_key FROM dst_keyed),
agg AS (
  SELECT record_key,
         BOOL_OR(category='EXCEPTION') has_exc,
         BOOL_OR(category='MISMATCH' AND severity='ERROR') has_err_mismatch
  FROM field_results GROUP BY record_key)
SELECT b.record_key,
  CASE WHEN m.category IS NOT NULL THEN m.category
       WHEN dk.record_key IS NOT NULL THEN 'DUPLICATE'
       WHEN COALESCE(a.has_exc,false) THEN 'EXCEPTION'
       WHEN COALESCE(a.has_err_mismatch,false) THEN 'MISMATCH'
       ELSE 'MATCH' END category
FROM base b
LEFT JOIN rec_missing m USING (record_key)
LEFT JOIN (SELECT DISTINCT record_key FROM dup_keys) dk USING (record_key)
LEFT JOIN agg a USING (record_key);

-- =====================================================================
-- OUTPUT
-- =====================================================================
.print '=== FIELD-LEVEL RESULTS ==='
SELECT record_key, field_key, category, severity, expected, actual, verdict
FROM field_results ORDER BY record_key, field_key;

.print '=== RECORD ROLLUP ==='
SELECT record_key, category FROM record_rollup ORDER BY record_key;

.print '=== SUMMARY ==='
SELECT
  COUNT(*) total,
  COUNT(*) FILTER (WHERE category='MATCH') match_count,
  COUNT(*) FILTER (WHERE category='MISMATCH') mismatch,
  COUNT(*) FILTER (WHERE category='MISSING_IN_SOURCE') missing_src,
  COUNT(*) FILTER (WHERE category='MISSING_IN_DEST') missing_dst,
  COUNT(*) FILTER (WHERE category='DUPLICATE') duplicate,
  COUNT(*) FILTER (WHERE category='EXCEPTION') exception
FROM record_rollup;
