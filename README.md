# Validation Center

**AI-assisted Data Comparison / Reconciliation Platform** — a spec-driven
reconciliation engine where a PDF template is the single source of truth for the
business specification.

> **Core principle:** *AI suggests · humans approve · the rule engine judges · the report explains.*
> AI never decides match/mismatch. The compare path is 100% deterministic and reproducible.

## Roles in the system
| Actor | Job |
|---|---|
| **PDF Template** | the Business Specification (fields + rules) |
| **AI** | extractor + suggestion assistant (P3/P4) — never the judge |
| **User** | approver + mapper (human-in-the-loop at every gate) |
| **Rule Engine** | deterministic comparison judge |
| **Canvas** | visual template / mapping builder (P2) |
| **Report** | explainable, auditable result |

## This repository — current state (Phase 0 + Phase 1 design)

```
validation_center/
├─ db/
│  └─ schema.sql                    # PostgreSQL DDL (P0 identity/audit + P1 core)
├─ docs/
│  ├─ architecture/
│  │  ├─ system-design.md           # components, data flow, rule engine, trade-offs
│  │  └─ er-diagram.md              # Mermaid ER diagram + entity cheat-sheet
│  └─ api/
│     └─ openapi.yaml               # REST contract (P0 + P1)
└─ README.md
```

## Phase roadmap (build order)
- **P0 Foundation** — SSO/RBAC, append-only audit, contracts, CI/CD.
- **P1 Manual Template + Compare Engine** ← *designed here* — manual templates,
  CSV/Excel datasets, form mapping, deterministic Rule Engine (8 rule types),
  batch reconciliation, report + Excel export.
- **P2 Visual Mapping Canvas** — React Flow BOM-style node/edge editor.
- **P3 PDF Template Extraction (AI)** — PDF → Draft Template with citation + confidence.
- **P4 AI Suggestion + Human Approval** — AI proposes mapping edges; users approve.
- **P5 Report / Audit / Versioning / Scheduling** — explainable drill-down, sign-off, cron.
- **P6 Enterprise Scale** — ClickHouse, multi-tenant, connectors, governance.

The P1 schema is **forward-compatible**: reserved nullable columns
(`citation`, `confidence`, `ai_reasoning`, `review_status`, canvas `pos_x/pos_y`,
`source_document_id`, `extraction_run_id`) let P2–P4 plug in without migrations that
break the core.

## Key design guarantees
1. **Reproducibility** — every `recon_run` stamps `template_version`, `mapping`, and
   `rule_engine_version`. Same inputs ⇒ identical verdicts.
2. **Immutability** — published template versions are locked by a DB trigger; edits
   require cloning a new version.
3. **Auditability** — `audit_log` is insert-only; every mutation is recorded.
4. **Explainability** — each result row carries `rule_id`, `expected`, `actual`,
   `severity`, traceable back to the rule and (in P3) its PDF citation.

## Built so far (all validated end-to-end)
- ✅ **Rule Engine** (8 rule types) on DuckDB — spec + runnable SQL templates +
  validated worked example (`db/rule-engine/`).
- ✅ **Reconciliation orchestrator** (`orchestrator/`, Python + DuckDB) — generates
  deterministic SQL from a real Template + Mapping, runs it, returns an explainable
  result set. `python -m orchestrator.test_smoke` → PASS.
- ✅ **Persistence** (`persistence/`) — `ReconStore` interface with `InMemoryStore`
  (runnable) + `PostgresStore` (writes `recon_runs` / partitioned `recon_results` /
  `recon_summary` per `db/schema.sql`).
- ✅ **REST API** (`api/`, FastAPI) — register template/mapping/datasets, trigger run,
  drill-down results, export xlsx. `python -m api.test_api` → PASS.
- ✅ **Excel report** (`reporting/`) — Summary + By-Field + Drilldown sheets with
  reproducibility stamps, sign-off block, and a PDF-Ref column (filled in P3).
- ✅ **.NET port** (`dotnet/`) — C# SQL generator; output is **identical** to the
  Python orchestrator's (cross-validated), `dotnet build` clean.
- ✅ **PDF AI Extraction — P3** (`extraction/`) — upload a PDF spec → AI proposes a
  **Draft Template** (fields + rules) with citations + confidence, all `SUGGESTED`.
  A human must approve before it can run (enforced). `MockExtractor` (offline) +
  `AnthropicExtractor` (Claude, structured output). Full loop PDF → draft → approve →
  reconcile verified end-to-end.

### Run everything
```bash
pip install -r requirements.txt
python -m pytest orchestrator api extraction -q          # all Python suites
cd dotnet && dotnet build ValidationCenter.slnx          # .NET parity
```

## The full loop now works
```
PDF spec ─►(AI)─► Draft Template ─►(human approve)─► Mapping ─►(Rule Engine)─► Report
   P3            SUGGESTED+citation      gate            P1/P2        deterministic   explainable
```

## Next steps
- Wire `PostgresStore` against a real database (apply `db/schema.sql`, set DSN).
- Async run queue + worker (current API runs synchronously for the demo).
- **P2 Visual Mapping Canvas** (React Flow) over the existing mapping schema.
- **P4 AI mapping suggestion** (edge proposals + confidence, human approval).
- OCR path for scanned PDFs; real Anthropic extraction on free-form specs.
