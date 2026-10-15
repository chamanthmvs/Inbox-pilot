"""
FastAPI app — a thin HTTP layer over the workflow/services. The React UI
(Stage 5) talks only to this.

Run it:  uvicorn app.api.main:app --reload   (from the backend/ directory)

Endpoints:
  GET  /api/health
  GET  /api/status                       auth + counts + current run snapshot
  GET  /api/taxonomy                     the fixed label menu (for dropdowns)
  POST /api/runs                         start a run (ingest→classify→pause)
  GET  /api/runs/{id}/stream             SSE live progress ("ingestion is happening")
  POST /api/runs/{id}/decisions          submit approvals → resume → apply
  GET  /api/proposals[?status=]          list proposals (joined with message info)
  GET  /api/proposals/counts
  POST /api/proposals/{message_id}/label human-set a label (for NEEDS_REVIEW)
  GET  /api/messages/{message_id}
  GET  /api/audit[?limit=]
"""

import json

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.manager import RunManager
from app.graph.approval import ApprovalService
from app.settings import Settings


class StartRunBody(BaseModel):
    max_messages: int | None = 50


class DecisionsBody(BaseModel):
    decisions: dict[str, str]  # {message_id: "APPROVED" | "REJECTED"}


class LabelBody(BaseModel):
    label: str


class DryRunBody(BaseModel):
    enabled: bool


class ClassifyBody(BaseModel):
    reclassify: bool = False


class GroupDecisionBody(BaseModel):
    domain: str
    label: str
    verdict: str  # "APPROVED" | "REJECTED"
    new_label: str | None = None  # optional relabel before approving
    action: str = "label"  # label | archive | trash


def get_manager(request: Request) -> RunManager:
    return request.app.state.manager


def create_app(manager: RunManager | None = None) -> FastAPI:
    app = FastAPI(title="Gmail Organizer API", version="0.1.0")

    # Dev-friendly CORS so the Vite frontend can call us. Tighten for prod.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.manager = manager or RunManager(Settings.from_env())

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/status")
    def status(mgr: RunManager = Depends(get_manager)):
        return {
            "authenticated": mgr.is_authenticated(),
            "dry_run": mgr.settings.dry_run,
            "llm_enabled": mgr.llm_available(),
            "proposal_counts": mgr.store.proposal_counts(),
            "audit_count": mgr.store.audit_count(),
            "run": mgr.current.snapshot() if mgr.current else None,
        }

    @app.post("/api/classify")
    def reclassify(body: ClassifyBody = ClassifyBody(), mgr: RunManager = Depends(get_manager)):
        try:
            run = mgr.start_classify(reclassify=body.reclassify)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error))
        return run.snapshot()

    @app.post("/api/connect")
    def connect(mgr: RunManager = Depends(get_manager)):
        try:
            return mgr.connect()
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error))

    @app.post("/api/settings/dry_run")
    def set_dry_run(body: DryRunBody, mgr: RunManager = Depends(get_manager)):
        return mgr.set_dry_run(body.enabled)

    @app.get("/api/taxonomy")
    def taxonomy(mgr: RunManager = Depends(get_manager)):
        return {
            "labels": [{"name": n, "description": d} for n, d in mgr.taxonomy.menu()],
            "needs_review_label": mgr.taxonomy.needs_review_label,
        }

    @app.post("/api/runs")
    def start_run(body: StartRunBody, mgr: RunManager = Depends(get_manager)):
        try:
            run = mgr.start(max_messages=body.max_messages)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error))
        return run.snapshot()

    @app.post("/api/runs/full")
    def start_full(mgr: RunManager = Depends(get_manager)):
        try:
            run = mgr.start_full()
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error))
        return run.snapshot()

    @app.get("/api/runs/{run_id}/stream")
    def stream_run(run_id: str, mgr: RunManager = Depends(get_manager)):
        try:
            events = mgr.stream(run_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such run")

        def sse():
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    @app.post("/api/runs/{run_id}/decisions")
    def submit_decisions(
        run_id: str, body: DecisionsBody, mgr: RunManager = Depends(get_manager)
    ):
        try:
            return mgr.submit_decisions(run_id, body.decisions)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such run")
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error))

    @app.get("/api/proposals")
    def proposals(
        status: str | None = Query(default=None),
        mgr: RunManager = Depends(get_manager),
    ):
        return {"proposals": mgr.store.list_proposals_detailed(status)}

    @app.get("/api/proposals/counts")
    def proposal_counts(mgr: RunManager = Depends(get_manager)):
        return mgr.store.proposal_counts()

    @app.post("/api/apply")
    def apply_approved(mgr: RunManager = Depends(get_manager)):
        try:
            return mgr.apply_approved()
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error))

    @app.get("/api/groups")
    def groups(
        status: str = Query(default="PENDING"),
        mgr: RunManager = Depends(get_manager),
    ):
        return {"groups": mgr.store.grouped_proposals(status)}

    @app.post("/api/groups/decide")
    def decide_group(body: GroupDecisionBody, mgr: RunManager = Depends(get_manager)):
        if body.new_label and not mgr.taxonomy.is_valid(body.new_label):
            raise HTTPException(status_code=400, detail=f"unknown label: {body.new_label}")
        return ApprovalService(mgr.store).decide_group(
            body.domain, body.label, body.verdict, new_label=body.new_label, action=body.action
        )

    @app.get("/api/learned")
    def learned(mgr: RunManager = Depends(get_manager)):
        return {"learned": mgr.store.list_learned()}

    @app.post("/api/proposals/{message_id}/label")
    def set_label(
        message_id: str, body: LabelBody, mgr: RunManager = Depends(get_manager)
    ):
        if not mgr.taxonomy.is_valid(body.label):
            raise HTTPException(status_code=400, detail=f"unknown label: {body.label}")
        ApprovalService(mgr.store).set_label(message_id, body.label)
        return {"ok": True, "message_id": message_id, "label": body.label}

    @app.get("/api/messages/{message_id}")
    def message(message_id: str, mgr: RunManager = Depends(get_manager)):
        found = mgr.store.get_message(message_id)
        if not found:
            raise HTTPException(status_code=404, detail="no such message")
        return found.model_dump()

    @app.get("/api/audit")
    def audit(limit: int = 100, mgr: RunManager = Depends(get_manager)):
        return {"audit": mgr.store.list_audit(limit)}

    return app


app = create_app()
