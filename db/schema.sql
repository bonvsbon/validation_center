-- =====================================================================
-- Validation Center — Database Schema (PostgreSQL 15+)
-- Scope: Phase 0 (Foundation) + Phase 1 (Manual Template + Compare Engine)
-- Forward-compatible with: P2 (Visual Canvas), P3 (PDF AI Extraction),
--                          P4 (AI Suggestion)  -- nullable columns reserved.
--
-- Conventions:
--   * UUID primary keys (gen_random_uuid via pgcrypto).
--   * timestamptz everywhere, UTC.
--   * JSONB for flexible/spec-like structures.
--   * Multi-tenant ready: tenant_id on every business table.
--   * High-volume row-level compare results live in a columnar engine
--     (DuckDB in P1, ClickHouse in P6). Postgres stores metadata + summary
--     + a bounded slice of results for drill-down.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =====================================================================
-- ENUM TYPES
-- =====================================================================
CREATE TYPE tenant_status      AS ENUM ('ACTIVE', 'SUSPENDED');
CREATE TYPE user_status        AS ENUM ('ACTIVE', 'DISABLED', 'INVITED');

CREATE TYPE template_status    AS ENUM ('DRAFT', 'IN_REVIEW', 'PUBLISHED', 'DEPRECATED');
CREATE TYPE field_datatype     AS ENUM ('STRING', 'INTEGER', 'DECIMAL', 'DATE', 'DATETIME', 'BOOLEAN', 'ENUM');
CREATE TYPE review_status      AS ENUM ('SUGGESTED', 'APPROVED', 'REJECTED', 'EDITED');

CREATE TYPE rule_type          AS ENUM (
    'EQUALITY', 'TOLERANCE', 'RANGE', 'REGEX', 'NOT_NULL',
    'LOOKUP', 'DATE_VALID', 'CROSS_FIELD'
);
CREATE TYPE rule_severity      AS ENUM ('ERROR', 'WARNING', 'INFO');

CREATE TYPE mapping_status     AS ENUM ('DRAFT', 'APPROVED', 'ARCHIVED');
CREATE TYPE node_type          AS ENUM ('PDF_FIELD', 'SOURCE_FIELD', 'DEST_FIELD');
CREATE TYPE edge_kind          AS ENUM ('PDF_TO_SOURCE', 'SOURCE_TO_DEST');

CREATE TYPE dataset_role       AS ENUM ('SOURCE', 'DESTINATION');
CREATE TYPE dataset_origin     AS ENUM ('CSV', 'EXCEL');
CREATE TYPE dataset_status     AS ENUM ('UPLOADED', 'PROFILED', 'READY', 'FAILED');

CREATE TYPE run_status         AS ENUM ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED');
CREATE TYPE result_category    AS ENUM (
    'MATCH', 'MISMATCH', 'MISSING_IN_SOURCE', 'MISSING_IN_DEST',
    'DUPLICATE', 'EXCEPTION'
);

-- =====================================================================
-- PHASE 0 — IDENTITY, TENANCY, AUDIT
-- =====================================================================

CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    status      tenant_status NOT NULL DEFAULT 'ACTIVE',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    email            TEXT NOT NULL,
    display_name     TEXT,
    external_idp_sub TEXT,                 -- OIDC subject
    status           user_status NOT NULL DEFAULT 'ACTIVE',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

-- Global role catalog (Author, Mapper, Approver, Operator, Auditor, Admin)
CREATE TABLE roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code        TEXT NOT NULL UNIQUE,      -- e.g. 'AUTHOR'
    name        TEXT NOT NULL,
    description TEXT
);

CREATE TABLE user_roles (
    user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id   UUID NOT NULL REFERENCES roles(id),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    PRIMARY KEY (user_id, role_id, tenant_id)
);

-- Append-only. NEVER UPDATE/DELETE. Enforce via DB role grants + trigger.
CREATE TABLE audit_log (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      UUID,
    actor_user_id  UUID REFERENCES users(id),
    entity_type    TEXT NOT NULL,             -- 'template_version', 'mapping_edge', ...
    entity_id      UUID,
    action         TEXT NOT NULL,             -- 'CREATE','UPDATE','PUBLISH','APPROVE','RUN',...
    before         JSONB,
    after          JSONB,
    correlation_id UUID,                      -- request tracing
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_entity ON audit_log (entity_type, entity_id);
CREATE INDEX idx_audit_tenant_time ON audit_log (tenant_id, created_at DESC);

CREATE TABLE app_setting (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID REFERENCES tenants(id),   -- NULL = global
    key        TEXT NOT NULL,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, key)
);

-- =====================================================================
-- CODE LISTS  (e.g. NCB account_status codes) — used by LOOKUP rules
-- =====================================================================
CREATE TABLE code_lists (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    code        TEXT NOT NULL,             -- e.g. 'NCB_ACCOUNT_STATUS'
    name        TEXT NOT NULL,
    description TEXT,
    version     INT NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code, version)
);

CREATE TABLE code_list_items (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code_list_id UUID NOT NULL REFERENCES code_lists(id) ON DELETE CASCADE,
    item_code    TEXT NOT NULL,            -- '11','13',...
    label        TEXT NOT NULL,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    ordinal      INT,
    UNIQUE (code_list_id, item_code)
);

-- =====================================================================
-- PHASE 1 — TEMPLATE  (the Business Specification)
-- =====================================================================

CREATE TABLE templates (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    key                TEXT NOT NULL,      -- stable slug, e.g. 'ncb-m16'
    name               TEXT NOT NULL,
    description        TEXT,
    current_version_id UUID,               -- FK set after version insert (deferred)
    created_by         UUID REFERENCES users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, key)
);

CREATE TABLE template_versions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id        UUID NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    version            TEXT NOT NULL,            -- semver '1.0.0'
    status             template_status NOT NULL DEFAULT 'DRAFT',
    locked             BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE once PUBLISHED (immutable)
    -- reserved for P3 (AI extraction); NULL in P1 manual templates:
    source_document_id UUID,
    extraction_run_id  UUID,
    created_by         UUID REFERENCES users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_by       UUID REFERENCES users(id),
    published_at       TIMESTAMPTZ,
    UNIQUE (template_id, version)
);

ALTER TABLE templates
    ADD CONSTRAINT fk_template_current_version
    FOREIGN KEY (current_version_id) REFERENCES template_versions(id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE template_fields (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_version_id UUID NOT NULL REFERENCES template_versions(id) ON DELETE CASCADE,
    field_key           TEXT NOT NULL,           -- stable key, e.g. 'outstanding_balance'
    name                TEXT NOT NULL,
    label_th            TEXT,
    datatype            field_datatype NOT NULL,
    required            BOOLEAN NOT NULL DEFAULT FALSE,
    format              TEXT,                    -- e.g. '###,###.##', 'YYYY-MM-DD'
    enum_code_list_id   UUID REFERENCES code_lists(id),  -- for ENUM datatype
    ordinal             INT NOT NULL DEFAULT 0,
    -- reserved for P3 AI extraction (NULL in manual templates):
    citation            JSONB,                   -- { page, bbox:[x,y,w,h], source_text }
    confidence          NUMERIC(4,3),            -- 0.000 - 1.000
    ai_reasoning        TEXT,
    review_status       review_status NOT NULL DEFAULT 'APPROVED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (template_version_id, field_key)
);

CREATE TABLE rules (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_version_id UUID NOT NULL REFERENCES template_versions(id) ON DELETE CASCADE,
    rule_key            TEXT NOT NULL,
    type                rule_type NOT NULL,
    params              JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"tolerance":0.01}
    severity            rule_severity NOT NULL DEFAULT 'ERROR',
    -- reserved for P3:
    citation            JSONB,
    confidence          NUMERIC(4,3),
    review_status       review_status NOT NULL DEFAULT 'APPROVED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (template_version_id, rule_key)
);

-- A rule can target one (most) or many (CROSS_FIELD) fields.
CREATE TABLE rule_target_fields (
    rule_id  UUID NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    field_id UUID NOT NULL REFERENCES template_fields(id) ON DELETE CASCADE,
    role     TEXT,    -- optional semantic role for CROSS_FIELD, e.g. 'term','installment'
    PRIMARY KEY (rule_id, field_id)
);

-- =====================================================================
-- PHASE 1 — DATASETS  (Source / Destination metadata only)
-- Actual rows live in the columnar staging engine (DuckDB), keyed by
-- staging_ref. Postgres holds schema + profile for mapping & display.
-- =====================================================================
CREATE TABLE datasets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    role        dataset_role NOT NULL,
    name        TEXT NOT NULL,
    origin      dataset_origin NOT NULL,
    file_ref    TEXT,                  -- object storage key of original upload
    staging_ref TEXT,                  -- columnar table/partition identifier
    row_count   BIGINT,
    status      dataset_status NOT NULL DEFAULT 'UPLOADED',
    created_by  UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dataset_columns (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id        UUID NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    column_name       TEXT NOT NULL,
    ordinal           INT NOT NULL,
    inferred_datatype field_datatype,
    null_count        BIGINT,
    distinct_count    BIGINT,
    sample_values     JSONB,           -- ["50000.00","49999.99",...]
    UNIQUE (dataset_id, column_name)
);

-- =====================================================================
-- PHASE 1 — MAPPING  (form-based in P1; canvas coords reserved for P2)
-- Graph model: PDF_FIELD -> SOURCE_FIELD -> DEST_FIELD
-- =====================================================================
CREATE TABLE mappings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    template_version_id UUID NOT NULL REFERENCES template_versions(id),
    name                TEXT NOT NULL,
    status              mapping_status NOT NULL DEFAULT 'DRAFT',
    source_dataset_id   UUID REFERENCES datasets(id),
    dest_dataset_id     UUID REFERENCES datasets(id),
    created_by          UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE mapping_nodes (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mapping_id         UUID NOT NULL REFERENCES mappings(id) ON DELETE CASCADE,
    type               node_type NOT NULL,
    -- a node references EITHER a template field OR a dataset column:
    template_field_id  UUID REFERENCES template_fields(id),
    dataset_column_id  UUID REFERENCES dataset_columns(id),
    label              TEXT,
    pos_x              NUMERIC,   -- reserved for P2 canvas (NULL in P1)
    pos_y              NUMERIC,
    CHECK (template_field_id IS NOT NULL OR dataset_column_id IS NOT NULL)
);

CREATE TABLE mapping_edges (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mapping_id    UUID NOT NULL REFERENCES mappings(id) ON DELETE CASCADE,
    from_node_id  UUID NOT NULL REFERENCES mapping_nodes(id) ON DELETE CASCADE,
    to_node_id    UUID NOT NULL REFERENCES mapping_nodes(id) ON DELETE CASCADE,
    kind          edge_kind NOT NULL,
    transform     JSONB,        -- { "op":"round","args":[2] } | null
    is_key        BOOLEAN NOT NULL DEFAULT FALSE,  -- part of record matching key
    rule_id       UUID REFERENCES rules(id),       -- rule bound to this comparison
    -- reserved for P4 AI suggestion (NULL until AI proposes):
    ai_suggested  BOOLEAN NOT NULL DEFAULT FALSE,
    confidence    NUMERIC(4,3),
    ai_reasoning  TEXT,
    review_status review_status NOT NULL DEFAULT 'APPROVED',
    approved_by   UUID REFERENCES users(id),
    approved_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_edge_mapping ON mapping_edges (mapping_id);

-- =====================================================================
-- PHASE 1 — RECONCILIATION RUN + RESULTS
-- =====================================================================
CREATE TABLE recon_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    template_version_id UUID NOT NULL REFERENCES template_versions(id),
    mapping_id          UUID NOT NULL REFERENCES mappings(id),
    source_dataset_id   UUID NOT NULL REFERENCES datasets(id),
    dest_dataset_id     UUID NOT NULL REFERENCES datasets(id),
    status              run_status NOT NULL DEFAULT 'QUEUED',
    rule_engine_version TEXT NOT NULL,          -- stamped for reproducibility
    params              JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message       TEXT,
    triggered_by        UUID REFERENCES users(id),
    queued_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
CREATE INDEX idx_run_tenant_time ON recon_runs (tenant_id, queued_at DESC);

CREATE TABLE recon_summary (
    run_id          UUID PRIMARY KEY REFERENCES recon_runs(id) ON DELETE CASCADE,
    total_records   BIGINT NOT NULL DEFAULT 0,
    match_count     BIGINT NOT NULL DEFAULT 0,
    mismatch_count  BIGINT NOT NULL DEFAULT 0,
    missing_src     BIGINT NOT NULL DEFAULT 0,
    missing_dst     BIGINT NOT NULL DEFAULT 0,
    duplicate_count BIGINT NOT NULL DEFAULT 0,
    exception_count BIGINT NOT NULL DEFAULT 0,
    by_severity     JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"ERROR":n,"WARNING":n}
    by_field        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- breakdown for category view
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Row-level results. Partition by run_id (LIST) for prune + cheap drop.
-- In P1 Postgres is fine; high-volume runs offload to columnar (P6).
CREATE TABLE recon_results (
    id          BIGSERIAL,
    run_id      UUID NOT NULL REFERENCES recon_runs(id) ON DELETE CASCADE,
    record_key  TEXT NOT NULL,            -- business key (e.g. account_no|id_card)
    category    result_category NOT NULL,
    field_key   TEXT,                     -- NULL for record-level (missing/dup)
    rule_id     UUID REFERENCES rules(id),
    severity    rule_severity,
    expected    TEXT,
    actual      TEXT,
    verdict     TEXT,                     -- short machine verdict
    detail      JSONB,                    -- transforms applied, citation ref, etc.
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, id)
) PARTITION BY LIST (run_id);
-- NOTE: create one partition per run at runtime, e.g.:
--   CREATE TABLE recon_results_<run> PARTITION OF recon_results
--     FOR VALUES IN ('<run_uuid>');
CREATE INDEX idx_results_category ON recon_results (run_id, category);
CREATE INDEX idx_results_field ON recon_results (run_id, field_key);

-- =====================================================================
-- IMMUTABILITY GUARD — published template_versions cannot be modified.
-- =====================================================================
CREATE OR REPLACE FUNCTION trg_block_locked_template() RETURNS trigger AS $$
BEGIN
    IF OLD.locked = TRUE AND TG_OP = 'UPDATE'
       AND NEW.status = OLD.status AND NEW.locked = OLD.locked THEN
        -- allow only deprecation transition; block any other change
        IF NEW.* IS DISTINCT FROM OLD.* THEN
            RAISE EXCEPTION 'template_version % is locked (PUBLISHED); clone a new version', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER guard_locked_template
    BEFORE UPDATE ON template_versions
    FOR EACH ROW EXECUTE FUNCTION trg_block_locked_template();

-- =====================================================================
-- SEED — baseline roles
-- =====================================================================
INSERT INTO roles (code, name, description) VALUES
    ('AUTHOR',   'Template Author', 'Creates and edits draft templates'),
    ('MAPPER',   'Mapper/Reviewer', 'Builds and reviews mappings'),
    ('APPROVER', 'Approver',        'Publishes template versions, signs off'),
    ('OPERATOR', 'Operator',        'Triggers reconciliation runs'),
    ('AUDITOR',  'Auditor',         'Read-only access to audit and reports'),
    ('ADMIN',    'Administrator',   'Tenant administration')
ON CONFLICT (code) DO NOTHING;
