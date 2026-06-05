-- =====================================================================
-- Rule Engine — 8 rule-type SQL templates (DuckDB)
-- engine version: rule-engine/1.0.0
--
-- Each template is a parameterized SELECT. The orchestrator substitutes
-- {{placeholders}} per compared field and concatenates the results with
-- UNION ALL into Step 6 (field_results) of pipeline.sql.
--
-- Output contract (every template emits exactly these columns, in order):
--   record_key, field_key, rule_id, severity, expected, actual, category, verdict
--
-- Source of {{src}} / {{dst}}: the canonical (post-transform) expressions for
-- the field, e.g.  s.f_outstanding_balance  /  d.f_outstanding_balance
-- referenced from a per-field `matched` view:
--   matched(record_key, <src expr> , <dst expr>)
--
-- Convention: expected = SOURCE value, actual = DESTINATION value.
-- =====================================================================


-- =====================================================================
-- 1) EQUALITY  — values must be identical (NULL == NULL counts as MATCH)
-- params: (none)
-- =====================================================================
SELECT
  record_key,
  '{{field_key}}'                                  AS field_key,
  '{{rule_id}}'                                    AS rule_id,
  '{{severity}}'                                   AS severity,
  CAST({{src}} AS VARCHAR)                         AS expected,
  CAST({{dst}} AS VARCHAR)                         AS actual,
  CASE WHEN {{src}} IS NOT DISTINCT FROM {{dst}}
       THEN 'MATCH' ELSE 'MISMATCH' END            AS category,
  CASE WHEN {{src}} IS NOT DISTINCT FROM {{dst}}
       THEN 'equal' ELSE 'not_equal' END           AS verdict
FROM matched;


-- =====================================================================
-- 2) TOLERANCE  — abs(src - dst) <= tolerance  (numeric)
-- params: tolerance -> {{tolerance}}, scale -> {{scale}} (default 6)
-- IMPORTANT: money MUST use DECIMAL (exact), never DOUBLE/float, otherwise
--            0.01 differences fail due to binary floating-point error.
-- cast failure => EXCEPTION
-- =====================================================================
WITH e AS (
  SELECT record_key,
         TRY_CAST({{src}} AS DECIMAL(38,{{scale}})) AS s,
         TRY_CAST({{dst}} AS DECIMAL(38,{{scale}})) AS d,
         CAST({{src}} AS VARCHAR)    AS ev,
         CAST({{dst}} AS VARCHAR)    AS av
  FROM matched
)
SELECT
  record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}', ev AS expected, av AS actual,
  CASE WHEN s IS NULL OR d IS NULL          THEN 'EXCEPTION'
       WHEN abs(s - d) <= {{tolerance}}     THEN 'MATCH'
       ELSE 'MISMATCH' END                  AS category,
  CASE WHEN s IS NULL OR d IS NULL          THEN 'uncastable'
       ELSE 'diff=' || CAST(abs(s - d) AS VARCHAR) END AS verdict
FROM e;


-- =====================================================================
-- 3) RANGE  — destination value within [min, max]
-- params: min -> {{min}}, max -> {{max}}
-- cast failure => EXCEPTION
-- =====================================================================
WITH e AS (
  SELECT record_key, TRY_CAST({{dst}} AS DOUBLE) AS d, CAST({{dst}} AS VARCHAR) AS av
  FROM matched
)
SELECT
  record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  '[{{min}},{{max}}]' AS expected, av AS actual,
  CASE WHEN d IS NULL                          THEN 'EXCEPTION'
       WHEN d BETWEEN {{min}} AND {{max}}       THEN 'MATCH'
       ELSE 'MISMATCH' END                      AS category,
  CASE WHEN d IS NULL                          THEN 'uncastable'
       WHEN d BETWEEN {{min}} AND {{max}}       THEN 'in_range'
       ELSE 'out_of_range' END                  AS verdict
FROM e;


-- =====================================================================
-- 4) REGEX / FORMAT  — destination matches pattern (full match)
-- params: pattern -> {{pattern}}   (NULL/empty value fails the format)
-- =====================================================================
SELECT
  record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  '{{pattern}}'             AS expected,
  CAST({{dst}} AS VARCHAR)  AS actual,
  CASE WHEN {{dst}} IS NULL                              THEN 'MISMATCH'
       WHEN regexp_full_match(CAST({{dst}} AS VARCHAR), '{{pattern}}') THEN 'MATCH'
       ELSE 'MISMATCH' END  AS category,
  CASE WHEN {{dst}} IS NULL THEN 'empty'
       WHEN regexp_full_match(CAST({{dst}} AS VARCHAR), '{{pattern}}') THEN 'format_ok'
       ELSE 'format_bad' END AS verdict
FROM matched;


-- =====================================================================
-- 5) NOT_NULL  — destination must be present (non-null, non-blank)
-- params: (none)
-- =====================================================================
SELECT
  record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  'NOT_NULL'                AS expected,
  CAST({{dst}} AS VARCHAR)  AS actual,
  CASE WHEN {{dst}} IS NULL OR trim(CAST({{dst}} AS VARCHAR)) = ''
       THEN 'MISMATCH' ELSE 'MATCH' END  AS category,
  CASE WHEN {{dst}} IS NULL OR trim(CAST({{dst}} AS VARCHAR)) = ''
       THEN 'missing' ELSE 'present' END AS verdict
FROM matched;


-- =====================================================================
-- 6) LOOKUP  — destination value must be an active item of a code list
-- prerequisite: temp table cl_{{code_list}}(item_code VARCHAR, active BOOLEAN)
--               loaded by the orchestrator from Postgres code_list_items.
-- If that temp table is missing, the orchestrator emits EXCEPTION for the field.
-- params: code_list -> {{code_list}}
-- =====================================================================
SELECT
  m.record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  'in:{{code_list}}'         AS expected,
  CAST({{dst}} AS VARCHAR)   AS actual,
  CASE WHEN {{dst}} IS NULL          THEN 'MISMATCH'
       WHEN cl.item_code IS NOT NULL THEN 'MATCH'
       ELSE 'MISMATCH' END           AS category,
  CASE WHEN {{dst}} IS NULL          THEN 'empty'
       WHEN cl.item_code IS NOT NULL THEN 'found'
       ELSE 'not_in_list' END        AS verdict
FROM matched m
LEFT JOIN cl_{{code_list}} cl
       ON cl.item_code = CAST({{dst}} AS VARCHAR)
      AND cl.active;


-- =====================================================================
-- 7) DATE_VALID  — destination parses as a date; optional not-future
-- params: format -> {{format}} (strptime), not_future -> {{not_future}} (true/false),
--         as_of_date -> {{as_of_date}}  (run parameter, e.g. DATE '2026-06-04')
-- IMPORTANT: use the run's {{as_of_date}}, NEVER current_date — the machine clock is
--            non-deterministic and may be a non-Gregorian locale (Thai BE) that
--            silently breaks "not-future" checks.
-- unparseable => MISMATCH (invalid), NOT exception
-- =====================================================================
WITH e AS (
  SELECT record_key,
         try_strptime(CAST({{dst}} AS VARCHAR), '{{format}}') AS d,
         CAST({{dst}} AS VARCHAR) AS av
  FROM matched
)
SELECT
  record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  'valid_date({{format}})' AS expected, av AS actual,
  CASE WHEN d IS NULL                                       THEN 'MISMATCH'
       WHEN {{not_future}} AND d::DATE > {{as_of_date}}        THEN 'MISMATCH'
       ELSE 'MATCH' END                                      AS category,
  CASE WHEN d IS NULL                                       THEN 'unparseable'
       WHEN {{not_future}} AND d::DATE > {{as_of_date}}        THEN 'future_date'
       ELSE 'valid' END                                      AS verdict
FROM e;


-- =====================================================================
-- 8) CROSS_FIELD  — expression over multiple canonical fields in one row
-- runs on matched_wide; params: lhs -> {{lhs}}, rhs -> {{rhs}}, tolerance -> {{tolerance}}
-- e.g. lhs = "f_term * f_installment", rhs = "f_total"
-- =====================================================================
WITH e AS (
  SELECT record_key,
         TRY_CAST(({{lhs}}) AS DOUBLE) AS lhs,
         TRY_CAST(({{rhs}}) AS DOUBLE) AS rhs
  FROM matched_wide
)
SELECT
  record_key, 'cross:{{rule_key}}' AS field_key, '{{rule_id}}', '{{severity}}',
  '{{rhs}}'              AS expected,
  CAST(lhs AS VARCHAR)  AS actual,
  CASE WHEN lhs IS NULL OR rhs IS NULL       THEN 'EXCEPTION'
       WHEN abs(lhs - rhs) <= {{tolerance}}  THEN 'MATCH'
       ELSE 'MISMATCH' END                   AS category,
  CASE WHEN lhs IS NULL OR rhs IS NULL       THEN 'uncastable'
       ELSE 'diff=' || CAST(abs(lhs - rhs) AS VARCHAR) END AS verdict
FROM e;
