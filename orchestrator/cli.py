"""CLI: generate + run a reconciliation from template/mapping/datasets.

Usage:
  python -m orchestrator \
      --template fixtures/template_ncb.json \
      --mapping  fixtures/mapping_ncb.json \
      --code-lists fixtures/code_lists.json \
      --source   fixtures/source.csv \
      --dest     fixtures/dest.csv \
      --as-of    2026-06-04 \
      --out      out/ \
      [--dump-sql out/generated.sql]
"""
from __future__ import annotations
import argparse
import json
import sys

from .resolver import resolve, load_json
from .engine import run, SUMMARY_COLS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orchestrator")
    ap.add_argument("--template", required=True)
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--code-lists", default=None)
    ap.add_argument("--source", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--as-of", default="2026-06-04")
    ap.add_argument("--engine-version", default="rule-engine/1.0.0")
    ap.add_argument("--out", default=None, help="dir for parquet outputs")
    ap.add_argument("--dump-sql", default=None, help="write generated SQL to a file")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    template = load_json(args.template)
    mapping = load_json(args.mapping)
    code_lists = load_json(args.code_lists) if args.code_lists else {}

    cfg = resolve(template, mapping, code_lists,
                  as_of_date=args.as_of, rule_engine_version=args.engine_version)
    result = run(cfg, args.source, args.dest, out_dir=args.out)

    if args.dump_sql:
        with open(args.dump_sql, "w", encoding="utf-8") as f:
            f.write(result.generated_sql)

    if args.json:
        print(json.dumps({
            "rule_engine_version": result.rule_engine_version,
            "as_of_date": result.as_of_date,
            "summary": result.summary,
            "record_rollup": [{"record_key": k, "category": c}
                              for k, c in result.record_rollup],
            "field_results": result.field_results,
        }, ensure_ascii=False, indent=2))
        return 0

    # Human-readable
    print(f"engine={result.rule_engine_version}  as_of={result.as_of_date}\n")
    print("=== SUMMARY ===")
    for c in SUMMARY_COLS:
        print(f"  {c:16} {result.summary[c]}")
    print("\n=== RECORD ROLLUP ===")
    for k, c in result.record_rollup:
        print(f"  {k:24} {c}")
    print("\n=== NON-MATCH FIELD RESULTS ===")
    any_nm = False
    for r in result.field_results:
        if r["category"] != "MATCH":
            any_nm = True
            print(f"  {r['record_key']:24} {r['field_key']:20} "
                  f"{r['category']:10} {r['severity']:8} "
                  f"actual={r['actual']!r:14} {r['verdict']}")
    if not any_nm:
        print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
