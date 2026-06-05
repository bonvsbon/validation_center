# Reconciliation Orchestrator (Python + DuckDB)

Turns an **approved Template + Mapping** into deterministic DuckDB SQL, runs the
comparison, and returns an explainable result set. This is the executable
implementation of `docs/architecture/rule-engine-spec.md`.

> AI suggests · humans approve · **this engine judges** · the report explains.
> No LLM, no network, no machine clock in the compare path.

## Install
```bash
pip install -r requirements.txt    # just duckdb
```

## Run the reference fixture
```bash
python -m orchestrator \
  --template orchestrator/fixtures/template_ncb.json \
  --mapping  orchestrator/fixtures/mapping_ncb.json \
  --code-lists orchestrator/fixtures/code_lists.json \
  --source   orchestrator/fixtures/source.csv \
  --dest     orchestrator/fixtures/dest.csv \
  --as-of    2026-06-04 \
  --out      orchestrator/out \
  --dump-sql orchestrator/out/generated.sql
```
Expected summary: `total 7 · match 2 · mismatch 1 · missing_src 1 · missing_dst 1 · duplicate 1 · exception 1`.

Add `--json` for machine-readable output.

## Verify
```bash
python -m orchestrator.test_smoke      # asserts the validated reference result
# or: pytest orchestrator/test_smoke.py
```

## How it works (data flow)
```
template.json ─┐
mapping.json  ─┼─► resolver.resolve() ─► ResolvedConfig
code_lists.json┘                              │
                                              ▼
                          sql_builder.build_setup_sql()  ──► one DuckDB script
                                              │   (stage→canonize→dup→match→
                                              │    missing→field-eval→rollup→summary)
                                              ▼
                              engine.run()  ──► RunResult { summary, rollup,
                                                            field_results, generated_sql }
                                              │
                                              └─► out/*.parquet  (sink stand-in;
                                                  production writes Postgres
                                                  recon_results + recon_summary)
```

## Module map
| File | Responsibility |
|---|---|
| `resolver.py` | load + validate Template/Mapping → `ResolvedConfig` (fails fast on bad bindings) |
| `transforms.py` | compile transform JSON → DuckDB expressions (trim/round/scale/date_format…) |
| `rules.py` | generate the per-rule SELECT for each of the 8 rule types (mirrors `rule_templates.sql`) |
| `sql_builder.py` | assemble the full pipeline SQL |
| `engine.py` | execute on DuckDB, return results, optional Parquet sink |
| `quoting.py` | identifier/literal quoting + expression whitelist (**injection choke point**) |
| `cli.py` / `__main__.py` | command-line entry |

## Design guarantees carried from the spec
- **Deterministic:** identical inputs → identical output (`test_determinism`).
- **Money = DECIMAL** (never float) in TOLERANCE / CROSS_FIELD.
- **`as_of_date` is a run parameter**, never `current_date` (host clock here is Thai BE!).
- **Missing vs Duplicate** computed from full key presence.
- **Severity:** only ERROR-severity mismatches demote a record; WARNING/INFO are reported but don't.
- **Injection-safe:** every config-derived identifier/literal is quoted; CROSS_FIELD
  expressions pass an arithmetic-only whitelist.

## Swapping the data plane (P6)
`engine.run()` is the only place bound to DuckDB. A ClickHouse/warehouse
implementation can produce the same `RunResult` from the same `ResolvedConfig`
without touching resolver/rules/sql_builder.

## Porting to .NET (later)
The logic is pure SQL-string generation from config. A C# port mirrors
`quoting`/`transforms`/`rules`/`sql_builder` and calls DuckDB.NET — no algorithmic
changes. Keep `quoting.py` semantics exactly (it is the security boundary).
