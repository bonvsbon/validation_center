# ADR-001 — Database / Storage Choices

**Status:** proposed · **Context:** Phase 1 deterministic core, regulated (NCB-style)
hire-purchase data, Windows/.NET-leaning team.

## Key idea: there are TWO database roles — don't conflate them

| Role | What it stores | Access pattern | Winner |
|---|---|---|---|
| **Metadata / OLTP** | templates, versions, mappings, audit, run meta, summary | many small txns, strong consistency | **PostgreSQL** (or SQL Server) |
| **Data plane / compute** | the actual source & destination rows being compared | few huge scans, columnar set-ops | **DuckDB** (→ ClickHouse at scale) |

Using one engine for both is the common mistake: an OLTP row-store crawls on
million-row column-wise compares, and an OLAP engine is poor at transactional metadata.

---

## A. Metadata / OLTP store

| Option | Pros | Cons | When to pick |
|---|---|---|---|
| **PostgreSQL** ✅ default | rich `JSONB`, partitioning, mature, free, great tooling | another stack if you're all-Microsoft | OSS-leaning teams; best JSON ergonomics for our spec schema |
| **SQL Server** ✅ if MS shop | AD/SSO integration, SSMS, first-class EF Core/.NET, you may already be licensed | JSON support weaker than JSONB; license cost | Windows/.NET enterprise (likely your case) |
| MySQL / MariaDB | ubiquitous, cheap | weaker JSON, weaker partitioning | existing MySQL ops |
| SQLite | zero-ops, embedded | single-writer, not for multi-user server | local dev / demo only |
| CockroachDB / Yugabyte | distributed, multi-region HA | operational complexity, overkill now | P6 multi-region |

**Note:** our DDL uses Postgres types (`JSONB`, native enums, `BIGSERIAL`, partitioning,
trigger). Porting to SQL Server means: `JSONB`→`NVARCHAR(MAX)` + `JSON_VALUE`/`ISJSON`,
enums→lookup tables or `CHECK`, `BIGSERIAL`→`IDENTITY`, partial-index nuances. Doable, ~1 day.

## B. Data plane / compute (the compare engine)

| Option | Pros | Cons | When |
|---|---|---|---|
| **DuckDB** ✅ start | embedded, columnar, very fast < ~10M rows, reads CSV/Excel/Parquet, can `ATTACH` Postgres, zero-ops, language-agnostic | single-node, in-process memory bound | P1–P5 default |
| **ClickHouse** | scales to 10⁸–10⁹ rows, distributed, blazing scans | server to run/operate, eventual-consistency quirks | P6 scale |
| **Polars** | fast embedded dataframes (Rust), Python-native | less SQL-native than DuckDB (has a SQL ctx though) | if pipeline is Python-heavy |
| Apache DataFusion | embeddable Rust SQL engine | younger ecosystem | Rust services |
| Spark | massive scale, ecosystem | heavy infra, slow startup, overkill early | only at big-data scale |
| **Warehouse pushdown** (Snowflake / BigQuery / Databricks / **MS Fabric–Synapse**) | run the compare SQL *where the data already lives*; no data movement | cost per query; couples engine to a vendor | when source/dest already in a warehouse |
| **SQL Server columnstore** | one fewer moving part if already licensed; clustered columnstore handles set-based compare decently | tempdb pressure at high volume; not as fast as DuckDB/ClickHouse | MS shop wanting zero new tech at moderate volume |

---

## Recommendation (two viable stacks)

**Stack 1 — OSS default (recommended overall)**
- Metadata: **PostgreSQL** · Compute: **DuckDB** (P1–P5) → **ClickHouse** (P6)
- Lowest cost, best fit for our JSONB-heavy spec schema, cleanest scaling path.

**Stack 2 — Microsoft-aligned (if you're a licensed SQL Server / .NET shop)**
- Metadata: **SQL Server** · Compute: **DuckDB embedded in the worker**
  (keep DuckDB for the heavy compare — it's language-agnostic and you call it from the
  .NET worker), or **SQL Server clustered columnstore** if you want zero new tech and
  volumes are moderate (< a few million rows/run).
- Best when AD/SSO, existing DBA skills, and licensing already favor SQL Server.

> **Either way, keep the `CompareEngine` interface** (see `rule-engine-spec.md §7`).
> The data plane is an implementation detail behind it — DuckDB today, ClickHouse or a
> warehouse later — without touching Template / Mapping / Orchestrator / Report services.

## Decision drivers to confirm with the team
1. Are you already licensed for **SQL Server / Azure**? → pushes Stack 2.
2. Does source/dest data **already live in a warehouse** (Synapse/Fabric/BigQuery)? →
   prefer pushdown, skip moving data.
3. Realistic **max rows per run**? < 10M → DuckDB is plenty; > 50M sustained → plan ClickHouse.
4. **Multi-region / HA** needed soon? → influences metadata store (Postgres HA vs managed cloud SQL).
