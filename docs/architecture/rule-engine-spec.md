# Rule Engine Specification (Deterministic Compare Engine)

**Status:** Phase 1 design · **Engine version tag:** `rule-engine/1.0.0`
**Data plane:** DuckDB (embedded columnar). Swappable behind the engine interface.

> **Non-negotiables**
> 1. Pure & deterministic — **no LLM / no network** during evaluation.
> 2. Set-based SQL on DuckDB — never per-row loops in app code.
> 3. Every verdict is self-explaining: `rule_id`, `expected`, `actual`, `severity`.
> 4. `rule_engine_version` is stamped on every run. Same inputs ⇒ identical output.

---

## 0. Vocabulary & verdict model

| Term | Meaning |
|---|---|
| **record_key** | business key built from `is_key` edges (e.g. `account_no | id_card`) |
| **expected** | the **Source** value (system of record) — configurable per run |
| **actual** | the **Destination** value (being verified) |
| **field rule** | a rule bound to a `SOURCE_TO_DEST` edge that judges one field |
| **EXCEPTION** | the rule **could not be evaluated** (cast failure, missing code list, bad param) |

### Result categories (refined from the overview doc)
| Category | Level | Meaning |
|---|---|---|
| `MATCH` | field & record | rule evaluated → passed |
| `MISMATCH` | field & record | rule evaluated → **failed** (incl. NOT_NULL violation, invalid date) |
| `MISSING_IN_DEST` | record | key exists in Source, not in Destination |
| `MISSING_IN_SOURCE` | record | key exists in Destination, not in Source |
| `DUPLICATE` | record | key appears >1 time on either side |
| `EXCEPTION` | field & record | rule **could not run** (engine could not decide) |

> Key correction vs. first draft: a *NOT_NULL violation* or *unparseable date* is a
> **MISMATCH** (the rule ran and the value failed), **not** an EXCEPTION. EXCEPTION is
> reserved strictly for "the engine could not produce a verdict."

### Record-level rollup (priority order)
For each `record_key`, the record category is the **first** that applies:
```
1. key missing on one side        → MISSING_IN_DEST / MISSING_IN_SOURCE
2. key duplicated on either side   → DUPLICATE
3. any field rule = EXCEPTION      → EXCEPTION
4. any ERROR-severity field MISMATCH → MISMATCH
5. otherwise                       → MATCH
```
WARNING/INFO mismatches **do not** demote a record from MATCH; they are reported
separately (`by_severity`) for visibility.

---

## 1. Execution pipeline (one run)

```
INPUT: template_version, mapping(approved), source_dataset, dest_dataset
       + resolved config (key_pairs[], compare_fields[], code_lists[])

 Step 1  Stage      → load src/dst rows into DuckDB (src_raw, dst_raw)
 Step 2  Canonize   → apply edge transforms, build record_key (src_keyed, dst_keyed)
 Step 3  Duplicates → keys with COUNT>1 per side          (dup_keys)
 Step 4  Match set  → unique keys present on BOTH sides    (matched)
 Step 5  Missing    → anti-joins                           (missing_dest, missing_src)
 Step 6  Field eval → run one rule-template per compared field over `matched`
 Step 7  Roll up    → record-level category per rollup priority
 Step 8  Persist    → recon_results (partition) + recon_summary
OUTPUT: deterministic, explainable result set
```

**Idempotency:** every step uses `CREATE OR REPLACE TEMP TABLE`; a re-run with identical
inputs reproduces identical tables byte-for-byte. Results are written to a **new**
`recon_run` — old runs are never mutated.

The engine **generates concrete SQL** by substituting each compared field's
`{{placeholders}}` into the rule template for its `rule_type`. The templates live in
`db/rule-engine/`. Pipeline scaffold: `db/rule-engine/pipeline.sql`.

---

## 2. Config resolved from the mapping (engine input)

The orchestrator flattens the approved mapping graph into this config before generating SQL:

```jsonc
{
  "expected_side": "SOURCE",          // which side is "expected"
  "key_pairs": [
    { "field_key": "account_no", "src_col": "ACCNO", "dst_col": "account_no",
      "src_transform": null, "dst_transform": { "op": "trim" } }
  ],
  "compare_fields": [
    { "field_key": "outstanding_balance",
      "src_col": "outstanding", "dst_col": "OUTSTANDING_BAL",
      "datatype": "DECIMAL",
      "src_transform": null,
      "dst_transform": { "op": "round", "args": [2] },
      "rule_id": "r_001", "rule_type": "TOLERANCE",
      "params": { "tolerance": 0.01 }, "severity": "ERROR" }
  ],
  "cross_field_rules": [
    { "rule_id": "r_009", "rule_key": "term_x_installment",
      "rule_type": "CROSS_FIELD",
      "params": { "lhs": "f_term * f_installment", "rhs": "f_total", "tolerance": 1 },
      "severity": "WARNING" }
  ],
  "code_lists": { "NCB_ACCOUNT_STATUS": ["11","13","20", "..."] }
}
```

---

## 3. Transform vocabulary (applied in Step 2, BEFORE comparison)

Transforms make two sides comparable; they are **not** judgments. Compiled to DuckDB exprs:

| `op` | DuckDB expression | Use |
|---|---|---|
| `trim` | `trim({col})` | strip whitespace |
| `upper` / `lower` | `upper({col})` / `lower({col})` | case-normalize |
| `round` | `round(TRY_CAST({col} AS DOUBLE), {args[0]})` | numeric scale |
| `scale` | `TRY_CAST({col} AS DOUBLE) / {args[0]}` | unit fix (e.g. /100) |
| `date_format` | `strftime(try_strptime({col}, '{from}'), '{to}')` | date normalize |
| `substr` | `substr({col}, {args[0]}, {args[1]})` | slice |
| `pad_left` | `lpad({col}, {args[0]}, '{args[1]}')` | zero-pad keys |
| `replace` | `replace({col}, '{from}', '{to}')` | remove separators |
| `null_if` | `nullif({col}, '{args[0]}')` | treat sentinel as null |

Transforms are recorded in `recon_results.detail.transforms` for explainability.

---

## 4. The 8 rule templates (DuckDB SQL)

Each template selects from the per-field matched view and emits rows shaped as:
`(record_key, field_key, category, rule_id, severity, expected, actual, verdict)`.

Placeholders: `{{field_key}}`, `{{rule_id}}`, `{{severity}}`, `{{src}}`/`{{dst}}` (canonical
exprs), plus rule params. Full files in `db/rule-engine/rule_templates.sql`.

### 4.1 EQUALITY
```sql
SELECT record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
       {{src}} AS expected, {{dst}} AS actual,
       CASE WHEN {{src}} IS NOT DISTINCT FROM {{dst}} THEN 'MATCH' ELSE 'MISMATCH' END AS category,
       CASE WHEN {{src}} IS NOT DISTINCT FROM {{dst}} THEN 'equal' ELSE 'not_equal' END AS verdict
FROM matched;
```
*NULL=NULL counts as MATCH (use NOT_NULL to forbid nulls).*

### 4.2 TOLERANCE (numeric)
```sql
WITH e AS (
  SELECT record_key, TRY_CAST({{src}} AS DOUBLE) s, TRY_CAST({{dst}} AS DOUBLE) d,
         {{src}} ev, {{dst}} av FROM matched)
SELECT record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}', ev, av,
  CASE WHEN s IS NULL OR d IS NULL THEN 'EXCEPTION'
       WHEN abs(s - d) <= {{tolerance}}  THEN 'MATCH'
       ELSE 'MISMATCH' END AS category,
  CASE WHEN s IS NULL OR d IS NULL THEN 'uncastable'
       ELSE 'diff=' || CAST(abs(s-d) AS VARCHAR) END AS verdict
FROM e;
```

### 4.3 RANGE (validate destination within [min,max])
```sql
WITH e AS (SELECT record_key, TRY_CAST({{dst}} AS DOUBLE) d, {{dst}} av FROM matched)
SELECT record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  '[{{min}},{{max}}]' AS expected, av,
  CASE WHEN d IS NULL THEN 'EXCEPTION'
       WHEN d BETWEEN {{min}} AND {{max}} THEN 'MATCH' ELSE 'MISMATCH' END,
  CASE WHEN d IS NULL THEN 'uncastable' ELSE 'in_range' END
FROM e;
```

### 4.4 REGEX / FORMAT (validate destination format)
```sql
SELECT record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  '{{pattern}}' AS expected, {{dst}} AS actual,
  CASE WHEN {{dst}} IS NULL THEN 'MISMATCH'                 -- empty fails the format
       WHEN regexp_full_match({{dst}}, '{{pattern}}') THEN 'MATCH' ELSE 'MISMATCH' END,
  'regex' AS verdict
FROM matched;
```

### 4.5 NOT_NULL (destination must be present)
```sql
SELECT record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  'NOT NULL' AS expected, {{dst}} AS actual,
  CASE WHEN {{dst}} IS NULL OR trim(CAST({{dst}} AS VARCHAR)) = '' THEN 'MISMATCH'
       ELSE 'MATCH' END,
  'presence' AS verdict
FROM matched;
```

### 4.6 LOOKUP (value must exist in a code list)
Code list is pre-loaded into temp table `cl_{{code_list}}(item_code, active)`.
```sql
SELECT m.record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  'in:{{code_list}}' AS expected, {{dst}} AS actual,
  CASE WHEN {{dst}} IS NULL THEN 'MISMATCH'
       WHEN cl.item_code IS NOT NULL THEN 'MATCH' ELSE 'MISMATCH' END,
  CASE WHEN cl.item_code IS NOT NULL THEN 'found' ELSE 'not_in_list' END
FROM matched m
LEFT JOIN cl_{{code_list}} cl
       ON cl.item_code = CAST({{dst}} AS VARCHAR) AND cl.active;
```
*If the code-list temp table is absent → the orchestrator emits EXCEPTION for the whole field (cannot evaluate).*

### 4.7 DATE_VALID (parseable; optional not-future)
```sql
WITH e AS (SELECT record_key, try_strptime(CAST({{dst}} AS VARCHAR), '{{format}}') d,
                  {{dst}} av FROM matched)
SELECT record_key, '{{field_key}}', '{{rule_id}}', '{{severity}}',
  'valid_date({{format}})' AS expected, av,
  CASE WHEN d IS NULL THEN 'MISMATCH'                           -- unparseable = invalid
       WHEN {{not_future}} AND d::DATE > current_date THEN 'MISMATCH'
       ELSE 'MATCH' END,
  CASE WHEN d IS NULL THEN 'unparseable'
       WHEN {{not_future}} AND d::DATE > current_date THEN 'future_date'
       ELSE 'valid' END
FROM e;
```

### 4.8 CROSS_FIELD (multi-field expression over the wide row)
Operates on `matched_wide` (canonical `f_<field>` columns on the chosen side).
```sql
WITH e AS (
  SELECT record_key,
         TRY_CAST(({{lhs}}) AS DOUBLE) lhs, TRY_CAST(({{rhs}}) AS DOUBLE) rhs
  FROM matched_wide)
SELECT record_key, 'cross:{{rule_key}}', '{{rule_id}}', '{{severity}}',
  '{{rhs}}' AS expected, CAST(lhs AS VARCHAR) AS actual,
  CASE WHEN lhs IS NULL OR rhs IS NULL THEN 'EXCEPTION'
       WHEN abs(lhs - rhs) <= {{tolerance}} THEN 'MATCH' ELSE 'MISMATCH' END,
  CASE WHEN lhs IS NULL OR rhs IS NULL THEN 'uncastable'
       ELSE 'diff=' || CAST(abs(lhs-rhs) AS VARCHAR) END
FROM e;
```

---

## 5. Edge cases & rules of thumb
- **Whitespace / case** → handle with transforms, not rules (keep rules pure).
- **Duplicate keys** → excluded from field comparison; reported as `DUPLICATE`. Decide
  policy explicitly (P1 default: do not field-compare duplicates).
- **Type coercion** → always `TRY_CAST`; a failed cast in a numeric rule = `EXCEPTION`,
  never a silent MISMATCH.
- **Money = DECIMAL, never float** → `TOLERANCE`/`CROSS_FIELD` cast to `DECIMAL(38,scale)`.
  Using `DOUBLE`, `abs(50000.01 - 50000.00)` evaluates to `0.010000000002…` and a
  `<= 0.01` check wrongly fails. (Caught in the reference run.)
- **Missing vs Duplicate** → `MISSING_*` is computed from *full* key presence on each side,
  not the de-duplicated set; otherwise a key duplicated on one side is misread as missing.
- **Empty vs NULL** → normalize with `null_if`/`trim` transforms up front so rules see one form.
- **Multiple rules per field** → allowed; each emits its own result row. Record rollup
  considers all of them.
- **Determinism** → no `random()`, no `now()`, and **never `current_date`** inside verdicts.
  `DATE_VALID` compares against an `as_of_date` **run parameter**, captured on the run.
  (The host clock here is a Thai-BE locale that returns `2569-…(BC)`, which would break
  every not-future check — exactly why the clock must not be trusted.)

## 5a. Validated reference run
`db/rule-engine/example_run.sql` is an executable, self-contained proof (DuckDB) that
exercises all 8 rule types and all 6 categories. Expected record-level output:

| record | category | why |
|---|---|---|
| A001 | MATCH | balance differs 0.01 = exactly within tolerance (DECIMAL) |
| A002 | MISMATCH | status `99` not in `NCB_ACCOUNT_STATUS` (ERROR) |
| A003 | MATCH | rate out of range but severity `WARNING` → does not demote |
| A004 | DUPLICATE | key duplicated in destination |
| A005 | MISSING_IN_DEST | source-only |
| A006 | MISSING_IN_SOURCE | destination-only |
| A007 | EXCEPTION | balance `'ABC'` uncastable → rule cannot evaluate |

Summary: total 7 · match 2 · mismatch 1 · missing_src 1 · missing_dst 1 · duplicate 1 · exception 1.

---

## 6. Persistence (Step 8)
- Field-level rows → `recon_results` (partition `recon_results_<run>`), columns:
  `record_key, category, field_key, rule_id, severity, expected, actual, verdict, detail`.
- Record-level rollup rows (MISSING/DUPLICATE/record category) → same table, `field_key = NULL`.
- Aggregates → `recon_summary` (headline counts + `by_severity` + `by_field`).
- Large runs (P6): write `recon_results` to columnar store instead of Postgres; Postgres
  keeps `recon_summary` + a capped sample for drill-down.

---

## 7. Engine interface (so the data plane is swappable)
```
interface CompareEngine {
  stage(dataset)            -> staging_ref
  run(config, engineVer)    -> { summary, resultsRef }
}
```
DuckDB is one implementation. ClickHouse/DataFusion can implement the same interface in
P6 without changing Template/Mapping/Orchestrator/Report services.
