"""
Assemble the real services (and the workflow) from Settings.

Kept separate from `workflow.py` so the graph stays dependency-injected and
testable with fakes. The GmailClient is constructed lazily because building it
triggers OAuth — callers that only need the store/approval shouldn't authenticate.
"""

from dataclasses import dataclass

from app.classifier.runner import build_classifier
from app.graph.approval import ApprovalService
from app.graph.executor import LabelExecutor
from app.graph.workflow import build_workflow
from app.ingestion.service import IngestionService
from app.settings import Settings
from app.store.db import Store


@dataclass
class Services:
    settings: Settings
    store: Store
    client: object
    ingestion: IngestionService
    classifier: object
    taxonomy: object
    approval: ApprovalService
    executor: LabelExecutor


def build_services(settings: Settings, client=None) -> Services:
    """
    Build the service graph. If `client` is None, a real GmailClient is created
    (which authenticates) — pass a client explicitly to avoid that (e.g. tests).
    """
    store = Store(settings.db_path)

    if client is None:
        from app.gmail.client import GmailClient  # local import → lazy OAuth
        client = GmailClient()

    classifier, taxonomy = build_classifier(
        taxonomy_path=settings.taxonomy_path,
        use_llm=settings.use_llm,
        model=settings.classifier_model,
    )

    return Services(
        settings=settings,
        store=store,
        client=client,
        ingestion=IngestionService(client, store),
        classifier=classifier,
        taxonomy=taxonomy,
        approval=ApprovalService(store),
        executor=LabelExecutor(client, store, dry_run=settings.dry_run),
    )


def build_default_workflow(
    settings: Settings, client=None, checkpointer=None, progress_holder=None
):
    """Return (compiled_graph, services) ready to invoke with a thread_id."""
    services = build_services(settings, client=client)
    graph = build_workflow(
        store=services.store,
        ingestion=services.ingestion,
        classifier=services.classifier,
        approval=services.approval,
        executor=services.executor,
        checkpointer=checkpointer,
        progress_holder=progress_holder,
    )
    return graph, services
