# Validation Center — System Design (Phase 0 + Phase 1)

**Scope of this document:** the deterministic core — identity/audit foundation (P0)
and manual-template + compare-engine (P1). No AI, no visual canvas yet; those layers
(P2 canvas, P3 PDF extraction, P4 AI suggestion) plug into the seams defined here.

**Guiding principle:** *AI suggests, humans approve, the rule engine judges.*
The compare path is 100% deterministic and reproducible — no LLM in the data plane.

---

## 1. Requirements

### Functional (P0 + P1)
- SSO login, RBAC (Author, Mapper, Approver, Operator, Auditor, Admin).
- Append-only audit log for every state-changing action.
- Create/edit a **Template** (fields + rules) manually; publish immutable versions.
- Upload Source & Destination datasets (CSV/Excel); profile their schema.
- Build a **form-based mapping** PDF/spec field → source field → dest field.
- Run a **batch reconciliation**; classify each record into
  Match / Mismatch / Missing(src) / Missing(dst) / Duplicate / Exception.
- Produce a report (summary + drill-down) with Excel export.

### Non-Functional
| Attribute | Target (P1) | Notes |
|---|---|---|
| Data volume | up to ~1M rows / run | columnar engine; scale to 10M+ in P6 |
| Run latency | minutes (batch), async | not realtime |
| Reproducibility | exact, byte-stable verdicts | stamp all versions |
| Availability | single-region, business hours | HA deferred to P6 |
| Security | encryption at rest/in transit, RBAC, PII never sent to LLM | regulated-data posture |
| Auditability | every mutation logged, immutable published specs | hard requirement |

### Constraints / Assumptions
- A1. Source/Dest are structured (CSV/Excel in P1; DB/API connectors in P6). PDF = spec only.
- A2. Modular monolith first; split along module seams later.
- A3. Single tenant in practice, but `tenant_id` carried everywhere for future isolation.

---

## 2. High-Level Architecture

```
                    ┌──────────────── API Gateway / BFF ────────────────┐
                    │  OIDC AuthN · RBAC AuthZ · correlation-id tracing  │
                    └───────────────────────┬───────────────────────────┘
                                             │
   ┌──────────────┬──────────────┬──────────┴───────┬──────────────┬──────────────┐
   │ Template Svc │  Mapping Svc │  Dataset Svc      │ Recon Orchestr│  Report Svc  │
   │ (versioning) │ (graph+form) │ (upload+profile)  │  (batch jobs) │ (summary)    │
   └──────┬───────┴──────┬───────┴────────┬──────────┴──────┬───────┴──────┬───────┘
          │              │                │                 │              │
          │              │                │          ┌──────┴───────┐      │
          │              │                │          │ Rule Engine  │      │
          │              │                │          │(deterministic)│     │
          │              │                │          └──────┬───────┘      │
   ┌──────┴──────────────┴────────────────┴─────────────────┴─────────────┴──────┐
   │  PostgreSQL  (identity, audit, templates, mappings, run meta, summary)        │
   │  Object Storage (original CSV/Excel uploads, report exports)                  │
   │  DuckDB columnar staging (dataset rows + set-based compare)  ← data plane      │
   │  Queue + Worker pool (ingestion profiling, reconciliation batches)            │
   └───────────────────────────────────────────────────────────────────────────────┘
```

### Component responsibilities
| Component | Responsibility | Talks to |
|---|---|---|
| **API Gateway / BFF** | auth, RBAC enforcement, request envelope, tracing | all services |
| **Template Service** | CRUD template/fields/rules, version lifecycle, immutability | Postgres |
| **Mapping Service** | nodes/edges, transforms, rule binding, key flagging | Postgres |
| **Dataset Service** | upload to object store, parse, infer schema, load to DuckDB | Object store, DuckDB |
| **Recon Orchestrator** | validate inputs, enqueue/track runs, assemble results | Queue, Rule Engine, Postgres |
| **Rule Engine** | deterministic set-based compare per rule type | DuckDB, Postgres |
| **Report Service** | aggregate summary, drill-down query, Excel export | Postgres, Object store |
| **Audit Service** | append-only writes for every mutation | Postgres |

---

## 3. Data Flow — one reconciliation run

```
1. Operator triggers run(template_version, mapping, source_ds, dest_ds)
2. Orchestrator validates:
     - template_version.status = PUBLISHED
     - mapping.status = APPROVED  (all key/compare edges review_status=APPROVED)
     - datasets.status = READY
   → reject early with explicit error if not.
3. Orchestrator creates recon_run (status=QUEUED, stamps rule_engine_version).
4. Worker picks job:
     a. Load both datasets from DuckDB staging.
     b. Apply edge transforms → canonical columns on each side.
     c. Build matching KEY from edges where is_key=TRUE.
     d. FULL OUTER JOIN on key → bucket records:
          matched-pair | missing_in_dest | missing_in_source | duplicate
     e. For matched pairs: evaluate each bound rule (EQUALITY, TOLERANCE, ...).
          → MATCH or MISMATCH per (record, field, rule), with expected/actual.
     f. Rule that cannot run (bad cast, null where NOT_NULL, lookup miss) → EXCEPTION.
     g. Write recon_results (partition for this run) + recon_summary.
5. Run marked COMPLETED. Report Service serves summary + drill-down.
```

**Idempotency:** same datasets + same template/mapping versions + same engine version
⇒ identical results. Re-running creates a new `recon_run` (history preserved), never mutates an old one.

---

## 4. Rule Engine Design (the Judge)

**Non-negotiables**
- Pure, deterministic, **no network/LLM calls** during evaluation.
- Set-based on DuckDB (no per-row app loops) for throughput.
- Each verdict is *self-explaining*: carries `rule_id`, `expected`, `actual`, `severity`, and (via the rule) a PDF citation reference.
- `rule_engine_version` is stamped on every run.

**Evaluation order per field**
1. Resolve canonical values (after transforms) on both sides.
2. Pre-checks: `NOT_NULL`, datatype cast → on failure emit `EXCEPTION`.
3. Apply the bound rule:

| Rule type | Semantics | `params` example |
|---|---|---|
| `EQUALITY` | normalized values must be identical | `{}` |
| `TOLERANCE` | `abs(a-b) <= tolerance` (numeric) | `{"tolerance":0.01}` |
| `RANGE` | value within `[min,max]` | `{"min":0,"max":36}` |
| `REGEX` | value matches pattern | `{"pattern":"^[0-9]{13}$"}` |
| `NOT_NULL` | value present | `{}` |
| `LOOKUP` | value ∈ code_list items (active) | `{"code_list":"NCB_ACCOUNT_STATUS"}` |
| `DATE_VALID` | parseable, optional not-future | `{"not_future":true}` |
| `CROSS_FIELD` | expression over multiple fields | `{"expr":"term*installment ~= total","tolerance":1}` |

**Verdict mapping → result_category**
- field rule passes on a matched pair → contributes to `MATCH`
- field rule fails → `MISMATCH`
- rule cannot evaluate → `EXCEPTION`
- record-level (no pair / duplicate key) → `MISSING_IN_*` / `DUPLICATE`
- A record is `MATCH` overall only if **all ERROR-severity** field rules pass.

---

## 5. API Surface (P0 + P1)
Full contract in [`docs/api/openapi.yaml`](../api/openapi.yaml). Highlights:

- `POST /templates`, `POST /templates/{id}/versions`, `POST /template-versions/{id}/publish`
- `POST /datasets` (upload), `GET /datasets/{id}/schema`
- `POST /mappings`, `PATCH /mappings/{id}/edges/{edgeId}` (transform/rule/key/approve)
- `POST /recon/runs`, `GET /recon/runs/{id}`, `GET /recon/runs/{id}/results`, `.../export`
- `GET /audit?entityType=&entityId=`

---

## 6. Storage Choices & Trade-offs

| Decision | Choice (P1) | Why | Revisit when |
|---|---|---|---|
| Spec/metadata store | PostgreSQL + JSONB | relational integrity + flexible spec | — |
| Compare data plane | **DuckDB** (embedded columnar) | fast set-based ops, zero-ops, great < 10M rows | volume/concurrency → ClickHouse (P6) |
| Row results store | Postgres partitioned by run | simple drill-down, cheap drop | huge results → columnar (P6) |
| Architecture | Modular monolith | low ops cost, fast iteration | clear scaling seams → split services (P6) |
| Async work | Queue + workers | isolate slow batch from API | — |
| Object storage | S3-compatible | originals + exports, cheap, durable | — |

**Explicit trade-offs**
- *DuckDB embedded* keeps P1 simple but is single-node; we isolate it behind the
  Rule Engine interface so swapping to ClickHouse later is a data-plane change only.
- *Partition-per-run results* makes retention/drop trivial but creates many partitions;
  acceptable at P1 volumes, revisited at scale.
- *Modular monolith* trades independent deploys for speed; module boundaries (Template,
  Mapping, Dataset, Recon, Report, Rule Engine) are real seams ready to extract.

---

## 7. Security & Compliance Notes
- PII (id_card, balances) **never** leaves to the LLM — only PDF *spec text* will (P3).
- Mask PII in UI and logs; encrypt at rest (DB + object store) and in transit (TLS).
- Published template versions are immutable (DB trigger guard) → tamper-evident specs.
- `audit_log` is insert-only; grant no UPDATE/DELETE to the app DB role.

---

## 8. What I'd revisit as it grows
- Compare engine: DuckDB → ClickHouse; partition strategy for results.
- Monolith → service split (Rule Engine and Extraction first — different scaling profiles).
- Mapping graph stored relationally (nodes/edges tables) is fine now; a JSONB graph
  snapshot per mapping version may be added for fast canvas load (P2).
- Multi-region HA, per-tenant quotas, connector framework (P6).
