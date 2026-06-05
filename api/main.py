"""Validation Center REST API (FastAPI) — implements the runnable core of
docs/api/openapi.yaml over the orchestrator + store + Excel reporting.

Flow: register template + mapping + code lists + datasets, then trigger a run.
The run resolves the config, executes the DuckDB rule engine, persists the
RunResult, and returns the summary. Results are drillable and exportable to xlsx.

In-memory registries keep the demo self-contained; swap `InMemoryStore` for
`PostgresStore` and the registries for DB-backed services in production.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import json
import os
import tempfile

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import duckdb

from orchestrator.resolver import resolve
from orchestrator.engine import run as engine_run
from persistence import InMemoryStore, ReconStore, ReconRunRecord, new_id
from reporting import build_report
from extraction import (load_pdf, MockExtractor, Extractor, build_sample_pdf,
                       new_extraction_run, to_draft_template, approval)
from suggester import (MockSuggester, Suggester, pending_summary,
                       approve_edges, to_mapping)
from suggester.core import SuggestedMapping, Node, Edge


# ---------------- request models ----------------
class RunRequest(BaseModel):
    template_id: str
    mapping_id: str
    code_lists_id: Optional[str] = None
    source_dataset_id: str
    dest_dataset_id: str
    as_of_date: str = "2026-06-04"
    engine_version: str = "rule-engine/1.0.0"
    background: bool = False     # True -> return QUEUED immediately, run in background


def _execute_run(store, run_id, tmpl, mp, cl, src_path, dst_path, as_of, engine_version):
    """The actual reconciliation, shared by the sync and background paths."""
    store.mark_running(run_id)
    try:
        cfg = resolve(tmpl, mp, cl, as_of_date=as_of, rule_engine_version=engine_version)
        result = engine_run(cfg, src_path, dst_path)
        store.save_results(run_id, result.field_results, result.record_rollup)
        store.mark_completed(run_id, result.summary)
    except Exception as e:  # noqa
        store.mark_failed(run_id, str(e))


class RegisterJson(BaseModel):
    name: Optional[str] = None
    body: Dict[str, Any]


class SuggestRequest(BaseModel):
    template_id: str
    source_dataset_id: str
    dest_dataset_id: str


class ApproveEdgesRequest(BaseModel):
    reject_edge_ids: List[str] = []
    approver: str = "system"
    name: str = "ai-suggested"


def _profile_csv(path: str) -> List[Dict[str, Any]]:
    con = duckdb.connect()
    p = path.replace("\\", "/")
    rel = con.execute(
        f"SELECT * FROM read_csv_auto('{p}', header=true, all_varchar=true) LIMIT 50")
    cols = [d[0] for d in rel.description]
    rows = rel.fetchall()
    out = []
    for i, name in enumerate(cols):
        samples = [r[i] for r in rows[:5]]
        out.append({"column_name": name, "ordinal": i, "sample_values": samples})
    con.close()
    return out


def create_app(store: Optional[ReconStore] = None, work_dir: Optional[str] = None,
               extractor: Optional[Extractor] = None,
               suggester: Optional[Suggester] = None) -> FastAPI:
    app = FastAPI(title="Validation Center API", version="0.1.0")
    app.state.store = store or InMemoryStore()
    app.state.work_dir = work_dir or tempfile.mkdtemp(prefix="vc_")
    app.state.extractor = extractor or MockExtractor()
    app.state.suggester = suggester or MockSuggester()
    app.state.templates: Dict[str, Dict[str, Any]] = {}
    app.state.mappings: Dict[str, Dict[str, Any]] = {}
    app.state.code_lists: Dict[str, Dict[str, Any]] = {}
    app.state.datasets: Dict[str, Dict[str, Any]] = {}
    app.state.documents: Dict[str, Dict[str, Any]] = {}
    app.state.extraction_runs: Dict[str, Dict[str, Any]] = {}
    app.state.suggestions: Dict[str, SuggestedMapping] = {}

    # dev CORS (Vite serves the canvas; the dev proxy makes this same-origin, but
    # allow cross-origin too so a separately-served build can call the API).
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    _FX = os.path.join(os.path.dirname(__file__), "..", "orchestrator", "fixtures")

    # ---------- demo bootstrap: full pipeline -> a ready-to-review suggestion ----------
    @app.post("/api/v1/demo/bootstrap", status_code=201)
    def demo_bootstrap():
        """One call the canvas can use: sample PDF -> extract -> (auto-approve template)
        -> register datasets + code list -> AI suggest mapping. Returns the suggestion
        graph plus the ids the canvas needs to approve edges and reconcile."""
        def reg_ds(role, name, fname):
            did = new_id()
            path = os.path.join(_FX, fname)
            app.state.datasets[did] = {"id": did, "role": role, "name": name,
                                       "path": path, "columns": _profile_csv(path)}
            return did
        sid = reg_ds("SOURCE", "source.csv", "source.csv")
        ddid = reg_ds("DESTINATION", "dest.csv", "dest.csv")
        cid = new_id()
        with open(os.path.join(_FX, "code_lists.json"), encoding="utf-8") as f:
            app.state.code_lists[cid] = json.load(f)

        pdf = os.path.join(app.state.work_dir, "demo_spec.pdf")
        build_sample_pdf(pdf)
        doc = load_pdf(pdf)
        result = app.state.extractor.extract(doc)
        erun = new_extraction_run(result, doc)
        tmpl = to_draft_template(result, doc, erun.run_id, template_key="ncb-m16")
        approval.approve_all(tmpl, approver="demo")   # template approved; canvas approves the MAPPING
        tid = new_id()
        app.state.templates[tid] = tmpl

        sm = app.state.suggester.suggest(
            tmpl, app.state.datasets[sid]["columns"],
            app.state.datasets[ddid]["columns"], sid, ddid)
        sug_id = new_id()
        app.state.suggestions[sug_id] = sm
        return {"suggestion_id": sug_id, "template_id": tid, "code_lists_id": cid,
                "source_dataset_id": sid, "dest_dataset_id": ddid,
                "as_of_date": "2026-06-04",
                "pending": pending_summary(sm), "graph": sm.to_dict()}

    # ---------- registration ----------
    @app.post("/api/v1/templates", status_code=201)
    def create_template(payload: RegisterJson):
        tid = new_id()
        app.state.templates[tid] = payload.body
        return {"id": tid, "version": payload.body.get("version")}

    @app.post("/api/v1/mappings", status_code=201)
    def create_mapping(payload: RegisterJson):
        mid = new_id()
        app.state.mappings[mid] = payload.body
        return {"id": mid}

    @app.post("/api/v1/code-lists", status_code=201)
    def create_code_lists(payload: RegisterJson):
        cid = new_id()
        app.state.code_lists[cid] = payload.body
        return {"id": cid, "lists": list(payload.body.keys())}

    @app.post("/api/v1/datasets", status_code=202)
    async def upload_dataset(role: str = Form(...), name: str = Form(...),
                             file: UploadFile = File(...)):
        if role not in ("SOURCE", "DESTINATION"):
            raise HTTPException(400, "role must be SOURCE or DESTINATION")
        did = new_id()
        dest = os.path.join(app.state.work_dir, f"{did}.csv")
        with open(dest, "wb") as f:
            f.write(await file.read())
        columns = _profile_csv(dest)
        app.state.datasets[did] = {"id": did, "role": role, "name": name,
                                   "path": dest, "columns": columns}
        return {"id": did, "role": role, "name": name, "status": "READY",
                "columns": columns}

    @app.get("/api/v1/datasets/{dataset_id}/schema")
    def dataset_schema(dataset_id: str):
        ds = app.state.datasets.get(dataset_id)
        if not ds:
            raise HTTPException(404, "dataset not found")
        return ds["columns"]

    # ---------- P3: PDF extraction -> draft template ----------
    @app.post("/api/v1/documents", status_code=201)
    async def upload_document(name: str = Form(...), file: UploadFile = File(...)):
        doc_id = new_id()
        dest = os.path.join(app.state.work_dir, f"{doc_id}.pdf")
        with open(dest, "wb") as f:
            f.write(await file.read())
        doc = load_pdf(dest)
        app.state.documents[doc_id] = {"id": doc_id, "name": name, "path": dest,
                                       "file_hash": doc.file_hash,
                                       "pages": doc.page_count,
                                       "is_scanned": doc.is_scanned}
        return {"id": doc_id, "pages": doc.page_count, "is_scanned": doc.is_scanned,
                "file_hash": doc.file_hash}

    @app.post("/api/v1/documents/{document_id}/extract", status_code=201)
    def extract_document(document_id: str, template_key: str = "extracted"):
        d = app.state.documents.get(document_id)
        if not d:
            raise HTTPException(404, "document not found")
        doc = load_pdf(d["path"])
        try:
            result = app.state.extractor.extract(doc)
        except ValueError as e:        # e.g. scanned PDF needs OCR
            raise HTTPException(422, str(e))
        erun = new_extraction_run(result, doc)
        tmpl = to_draft_template(result, doc, erun.run_id, template_key=template_key)
        tid = new_id()
        app.state.templates[tid] = tmpl
        app.state.extraction_runs[erun.run_id] = erun.to_dict()
        return {
            "template_id": tid,
            "status": tmpl["status"],                       # DRAFT
            "extraction_run": erun.to_dict(),
            "pending_review": approval.pending_review(tmpl),  # AI suggested, awaiting human
        }

    @app.get("/api/v1/templates/{template_id}/review")
    def template_review(template_id: str):
        tmpl = app.state.templates.get(template_id)
        if not tmpl:
            raise HTTPException(404, "template not found")
        return {"status": tmpl.get("status"),
                "pending_review": approval.pending_review(tmpl),
                "fields": tmpl.get("fields"), "rules": tmpl.get("rules")}

    @app.post("/api/v1/templates/{template_id}/approve")
    def approve_template(template_id: str, approver: str = "system"):
        tmpl = app.state.templates.get(template_id)
        if not tmpl:
            raise HTTPException(404, "template not found")
        approval.approve_all(tmpl, approver=approver, publish=True)
        return {"status": tmpl["status"], "pending_review": approval.pending_review(tmpl)}

    # ---------- P4: AI mapping suggestion ----------
    @app.post("/api/v1/mappings/suggest", status_code=201)
    def suggest_mapping(req: SuggestRequest):
        tmpl = app.state.templates.get(req.template_id)
        src = app.state.datasets.get(req.source_dataset_id)
        dst = app.state.datasets.get(req.dest_dataset_id)
        if not tmpl or not src or not dst:
            raise HTTPException(404, "template/dataset not found")
        sm = app.state.suggester.suggest(
            tmpl, src["columns"], dst["columns"],
            req.source_dataset_id, req.dest_dataset_id)
        sug_id = new_id()
        app.state.suggestions[sug_id] = sm
        return {"suggestion_id": sug_id, "pending": pending_summary(sm),
                "graph": sm.to_dict()}

    @app.get("/api/v1/mappings/suggestions/{suggestion_id}")
    def get_suggestion(suggestion_id: str):
        sm = app.state.suggestions.get(suggestion_id)
        if not sm:
            raise HTTPException(404, "suggestion not found")
        return {"pending": pending_summary(sm), "graph": sm.to_dict()}

    @app.post("/api/v1/mappings/suggestions/{suggestion_id}/approve")
    def approve_suggestion(suggestion_id: str, req: ApproveEdgesRequest):
        sm = app.state.suggestions.get(suggestion_id)
        if not sm:
            raise HTTPException(404, "suggestion not found")
        approve_edges(sm, reject_ids=req.reject_edge_ids, approver=req.approver)
        mapping = to_mapping(sm, name=req.name)
        if not mapping["key_pairs"]:
            raise HTTPException(422, "approved mapping has no key fields")
        mid = new_id()
        app.state.mappings[mid] = mapping
        return {"mapping_id": mid, "mapping": mapping}

    # ---------- reconciliation ----------
    @app.post("/api/v1/recon/runs", status_code=202)
    def trigger_run(req: RunRequest, background_tasks: BackgroundTasks):
        tmpl = app.state.templates.get(req.template_id)
        mp = app.state.mappings.get(req.mapping_id)
        cl = app.state.code_lists.get(req.code_lists_id, {}) if req.code_lists_id else {}
        src = app.state.datasets.get(req.source_dataset_id)
        dst = app.state.datasets.get(req.dest_dataset_id)
        if not tmpl or not mp or not src or not dst:
            raise HTTPException(404, "template/mapping/dataset not found")
        # human-approval invariant: AI-suggested templates cannot run until approved
        try:
            approval.require_approved(tmpl)
        except ValueError as e:
            raise HTTPException(422, str(e))

        meta = ReconRunRecord(
            run_id=new_id(),
            template_version_id=req.template_id,
            mapping_id=req.mapping_id,
            source_dataset_id=req.source_dataset_id,
            dest_dataset_id=req.dest_dataset_id,
            rule_engine_version=req.engine_version,
            as_of_date=req.as_of_date)
        st: ReconStore = app.state.store
        st.create_run(meta)
        args = (st, meta.run_id, tmpl, mp, cl, src["path"], dst["path"],
                req.as_of_date, req.engine_version)
        if req.background:
            # return QUEUED immediately; the worker runs after the response is sent.
            background_tasks.add_task(_execute_run, *args)
            return st.get_run(meta.run_id).to_dict()      # status = QUEUED
        _execute_run(*args)                                # synchronous (default)
        rec = st.get_run(meta.run_id)
        if rec.status == "FAILED":
            raise HTTPException(422, f"run failed: {rec.error_message}")
        return rec.to_dict()

    @app.get("/api/v1/recon/runs")
    def list_runs():
        return [r.to_dict() for r in app.state.store.list_runs()]

    @app.get("/api/v1/recon/runs/{run_id}")
    def get_run(run_id: str):
        r = app.state.store.get_run(run_id)
        if not r:
            raise HTTPException(404, "run not found")
        return r.to_dict()

    @app.get("/api/v1/recon/runs/{run_id}/results")
    def get_results(run_id: str,
                    category: Optional[str] = None,
                    field_key: Optional[str] = None,
                    page: int = Query(1, ge=1),
                    page_size: int = Query(50, ge=1, le=1000)):
        if not app.state.store.get_run(run_id):
            raise HTTPException(404, "run not found")
        total, rows = app.state.store.get_results(
            run_id, category=category, field_key=field_key,
            offset=(page - 1) * page_size, limit=page_size)
        return {"total": total, "page": page, "page_size": page_size, "items": rows}

    @app.get("/api/v1/recon/runs/{run_id}/export")
    def export_run(run_id: str, format: str = "xlsx"):
        run = app.state.store.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        _, rows = app.state.store.get_results(run_id, limit=10_000_000)
        rollup = app.state.store.get_rollup(run_id)
        out = os.path.join(app.state.work_dir, f"report_{run_id}.xlsx")
        build_report(run.to_dict(), rows, rollup, out)
        return FileResponse(
            out, filename=f"reconciliation_{run_id}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/health")
    def health():
        return {"status": "ok", "store": type(app.state.store).__name__}

    return app


def _default_store():
    """The runnable server persists to a LOCAL DuckDB file (no server needed).
    Override the path with VC_DB_PATH; tests call create_app() -> InMemoryStore."""
    from persistence import DuckDBStore
    return DuckDBStore(os.environ.get("VC_DB_PATH", "./data/vc.duckdb"))


# Module-level app for `uvicorn api.main:app` — durable local persistence.
app = create_app(store=_default_store())
