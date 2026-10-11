"""
The LangGraph workflow — the spine of the whole app.

    ingest → classify → review (HUMAN) → apply

`review` calls LangGraph's `interrupt()`: the graph pauses and hands the pending
proposals out to the caller (the API/UI). The human's verdicts come back via
`Command(resume={message_id: "APPROVED"|"REJECTED"})`, the review node records
them, and the graph continues into `apply`.

Services are injected (store, ingestion, classifier, approval, executor) so the
same graph is driven by the API in production and by fakes in tests.
"""

from typing import Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.classifier.runner import classify_store


class WorkflowState(TypedDict, total=False):
    max_messages: Optional[int]
    reclassify: bool
    sync: dict
    classify: dict
    decisions: dict
    review: dict
    apply: dict


def build_workflow(
    *,
    store,
    ingestion,
    classifier,
    approval,
    executor,
    checkpointer=None,
    progress_holder=None,
):
    # progress_holder is an optional dict like {"cb": callable_or_None}. It lets
    # a long-lived compiled graph emit live progress for whichever run is active
    # (the API sets holder["cb"] for the current run). Default: no-op.
    def _sink(event):
        cb = progress_holder.get("cb") if progress_holder else None
        if cb is not None:
            try:
                cb(event)
            except Exception:
                pass

    def ingest_node(state: WorkflowState) -> dict:
        result = ingestion.run_sync(
            max_messages=state.get("max_messages"), on_progress=_sink
        )
        return {
            "sync": {
                "mode": result.mode,
                "fetched": result.fetched,
                "deleted": result.deleted,
                "history_id": result.history_id,
                "email_address": result.email_address,
            }
        }

    def classify_node(state: WorkflowState) -> dict:
        result = classify_store(
            store,
            classifier,
            reclassify=state.get("reclassify", False),
            on_progress=_sink,
        )
        return {
            "classify": {
                "processed": result.processed,
                "skipped_decided": result.skipped_decided,
                "by_method": result.by_method,
                "by_status": result.by_status,
            }
        }

    def review_node(state: WorkflowState) -> dict:
        # Pause here. The payload is what the UI shows the human.
        decisions = interrupt(
            {
                "pending": approval.pending(),
                "needs_review": approval.needs_review(),
            }
        )
        counts = approval.apply_decisions(decisions or {})
        return {"decisions": decisions or {}, "review": counts}

    def apply_node(state: WorkflowState) -> dict:
        result = executor.apply_approved(on_progress=_sink)
        return {"apply": result.to_dict()}

    builder = StateGraph(WorkflowState)
    builder.add_node("ingest", ingest_node)
    builder.add_node("classify", classify_node)
    builder.add_node("review", review_node)
    builder.add_node("apply", apply_node)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "classify")
    builder.add_edge("classify", "review")
    builder.add_edge("review", "apply")
    builder.add_edge("apply", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
