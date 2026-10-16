"""
RunManager — drives the LangGraph workflow for the API and turns the graph's
mid-run progress into a stream the UI can watch.

A "run" goes: ingest → classify → (pause at review). We run that in a background
thread so progress events can be streamed live while it works. When it pauses,
we keep the interrupt payload; the client submits decisions, and we resume the
same graph thread to apply.

v1 is single-user: at most one active run. The read-only store is available
immediately (no OAuth); the graph + GmailClient are built lazily on first run
(which triggers OAuth in the server process).
"""

import queue
import threading
import uuid

from langgraph.types import Command

from app.classifier.taxonomy import Taxonomy
from app.graph.factory import build_default_workflow
from app.settings import Settings
from app.store.db import Store


class Run:
    def __init__(self, run_id: str, max_messages=None, mode="batch"):
        self.id = run_id
        self.thread_id = run_id
        self.max_messages = max_messages
        self.mode = mode                 # "batch" (graph) | "full" (background crawl)
        self.status = "running"          # running|paused|applying|done|error
        self.events: queue.Queue = queue.Queue()
        self.interrupt_payload = None
        self.apply_result = None
        self.last_progress = None        # latest progress event (survives refresh)
        self.summary = None
        self.error = None

    def snapshot(self) -> dict:
        counts = None
        if self.interrupt_payload:
            counts = {
                "pending": len(self.interrupt_payload.get("pending", [])),
                "needs_review": len(self.interrupt_payload.get("needs_review", [])),
            }
        return {
            "run_id": self.id,
            "status": self.status,
            "mode": self.mode,
            "counts": counts,
            "progress": self.last_progress,
            "summary": self.summary,
            "apply": self.apply_result,
            "error": self.error,
        }


class RunManager:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.store = Store(settings.db_path)          # reads work without OAuth
        self.taxonomy = Taxonomy.load(settings.taxonomy_path)
        self._client = client                         # inject a fake in tests
        self._graph = None
        self._services = None
        self._progress_holder = {"cb": None}
        self._lock = threading.Lock()
        self.current: Run | None = None

    # -- lazy graph (OAuth happens here, unless a client was injected) --------

    def _ensure_graph(self):
        if self._graph is None:
            self._graph, self._services = build_default_workflow(
                self.settings,
                client=self._client,
                checkpointer=None,
                progress_holder=self._progress_holder,
            )
        return self._graph, self._services

    def is_authenticated(self) -> bool:
        return self._graph is not None

    def llm_available(self) -> bool:
        import os
        return bool(os.environ.get("ANTHROPIC_API_KEY")) and self.settings.use_llm

    def start_classify(self, reclassify: bool = False) -> Run:
        """
        Re-run classification over the EXISTING store — no Gmail fetch, no OAuth.
        With the LLM enabled this labels the NEEDS_REVIEW pile; rule-matched
        PENDING items just re-match (free); APPLIED items are untouched.
        """
        from app.classifier.runner import build_classifier, classify_store

        with self._lock:
            if self.current and self.current.status in ("running", "applying"):
                raise RuntimeError("a run is already in progress")
            run = Run(str(uuid.uuid4()), mode="classify")
            self.current = run

        def worker():
            try:
                classifier, _ = build_classifier(
                    taxonomy_path=self.settings.taxonomy_path,
                    use_llm=self.settings.use_llm,
                    model=self.settings.classifier_model,
                )

                def sink(event):
                    run.last_progress = event
                    run.events.put({"type": "progress", **event})

                result = classify_store(
                    self.store, classifier, reclassify=reclassify, on_progress=sink,
                    verify_rules=self.settings.verify_rules,
                )
                run.summary = {
                    "processed": result.processed,
                    "by_status": result.by_status,
                    "by_method": result.by_method,
                }
                run.status = "done"
                run.events.put({"type": "done", **run.snapshot()})
            except Exception as error:
                run.status = "error"
                run.error = str(error)
                run.events.put({"type": "error", "message": str(error)})
            finally:
                run.events.put(None)

        threading.Thread(target=worker, daemon=True).start()
        return run

    def start_autopilot(self, level: str = "balanced", threshold: float = 0.85) -> Run:
        """
        One click: auto-decide confident proposals per policy, then apply them.
        Leaves only the unsure ones for review. Runs in the background with SSE.
        """
        from app.graph.autopilot import autopilot_decide

        with self._lock:
            if self.current and self.current.status in ("running", "applying"):
                raise RuntimeError("a run is already in progress")
            run = Run(str(uuid.uuid4()), mode="autopilot")
            self.current = run

        def worker():
            try:
                _, services = self._ensure_graph()

                def sink(event):
                    run.last_progress = event
                    run.events.put({"type": "progress", **event})

                sink({"phase": "item", "stage": "deciding", "done": 0, "total": 0})
                decided = autopilot_decide(services.store, services.taxonomy, threshold, level)
                applied = services.executor.apply_approved(on_progress=sink)
                run.summary = {**decided, "apply": applied.to_dict(), "level": level, "threshold": threshold}
                run.status = "done"
                run.events.put({"type": "done", **run.snapshot()})
            except Exception as error:
                run.status = "error"
                run.error = str(error)
                run.events.put({"type": "error", "message": str(error)})
            finally:
                run.events.put(None)

        threading.Thread(target=worker, daemon=True).start()
        return run

    def undo_last(self) -> dict:
        """Reverse the most recent apply run (remove labels, restore inbox, untrash)."""
        _, services = self._ensure_graph()
        return services.executor.undo_last()

    def start_segregate(self, label: str) -> Run:
        """Background: AI-split one label into finer nested sub-labels (no OAuth needed)."""
        from app.classifier.runner import build_classifier
        from app.classifier.segregate import segregate_label

        with self._lock:
            if self.current and self.current.status in ("running", "applying"):
                raise RuntimeError("a run is already in progress")
            run = Run(str(uuid.uuid4()), mode="segregate")
            self.current = run

        def worker():
            try:
                classifier, _ = build_classifier(
                    taxonomy_path=self.settings.taxonomy_path,
                    use_llm=self.settings.use_llm,
                    model=self.settings.classifier_model,
                )

                def sink(event):
                    run.last_progress = event
                    run.events.put({"type": "progress", **event})

                summary = segregate_label(
                    self.store, classifier, label,
                    taxonomy_path=self.settings.taxonomy_path, on_progress=sink,
                )
                run.summary = summary
                run.status = "done"
                run.events.put({"type": "done", **run.snapshot()})
            except Exception as error:
                run.status = "error"
                run.error = str(error)
                run.events.put({"type": "error", "message": str(error)})
            finally:
                run.events.put(None)

        threading.Thread(target=worker, daemon=True).start()
        return run

    def apply_approved(self) -> dict:
        """
        Apply every APPROVED proposal directly (create+attach labels, audit),
        independent of any live run/graph. Builds the client/executor if needed
        (authenticating). This is what the UI's 'Apply' button calls.
        """
        self._ensure_graph()
        result = self._services.executor.apply_approved()
        return result.to_dict()

    def set_dry_run(self, enabled: bool) -> dict:
        """Flip apply-mode at runtime (LIVE ↔ DRY-RUN) from the UI."""
        self.settings.dry_run = bool(enabled)
        if self._services is not None:
            self._services.executor.dry_run = self.settings.dry_run
        return {"dry_run": self.settings.dry_run}

    def set_verify_rules(self, enabled: bool) -> dict:
        """Flip whether the AI vets rule matches (no rules-alone)."""
        self.settings.verify_rules = bool(enabled)
        return {"verify_rules": self.settings.verify_rules}

    def connect(self) -> dict:
        """
        Authenticate with Gmail now (builds the graph → runs OAuth if needed),
        without starting a run. Lets the UI do a one-time 'Connect' step before
        showing the Organize screen. Raises on failure (missing creds, etc.).
        """
        self._ensure_graph()
        email = ""
        try:
            email = self._services.client.get_profile().get("emailAddress", "")
        except Exception:
            pass
        return {"authenticated": True, "email": email}

    # -- starting a run -------------------------------------------------------

    def start(self, max_messages=None) -> Run:
        with self._lock:
            if self.current and self.current.status in ("running", "applying"):
                raise RuntimeError("a run is already in progress")
            run = Run(str(uuid.uuid4()), max_messages=max_messages)
            self.current = run

        def worker():
            try:
                graph, _ = self._ensure_graph()
                self._progress_holder["cb"] = lambda e: run.events.put(
                    {"type": "progress", **e}
                )
                cfg = {"configurable": {"thread_id": run.thread_id}}
                state = graph.invoke(
                    {"max_messages": max_messages, "reclassify": False}, cfg
                )
                if "__interrupt__" in state:
                    run.interrupt_payload = state["__interrupt__"][0].value
                    run.status = "paused"
                    run.events.put({"type": "paused", **run.snapshot()})
                else:
                    run.status = "done"
                    run.apply_result = state.get("apply")
                    run.events.put({"type": "done", **run.snapshot()})
            except Exception as error:  # surface auth/network/etc. to the UI
                run.status = "error"
                run.error = str(error)
                run.events.put({"type": "error", "message": str(error)})
            finally:
                self._progress_holder["cb"] = None
                run.events.put(None)  # sentinel → close the SSE stream

        threading.Thread(target=worker, daemon=True).start()
        return run

    # -- full-inbox background crawl ------------------------------------------

    def start_full(self) -> Run:
        with self._lock:
            if self.current and self.current.status in ("running", "applying"):
                raise RuntimeError("a run is already in progress")
            run = Run(str(uuid.uuid4()), max_messages=None, mode="full")
            self.current = run

        def worker():
            from app.ingestion.full import full_ingest_and_classify
            try:
                _, services = self._ensure_graph()

                def sink(event):
                    run.last_progress = event
                    run.events.put({"type": "progress", **event})

                summary = full_ingest_and_classify(
                    services.client, services.store, services.classifier, on_progress=sink,
                    verify_rules=self.settings.verify_rules,
                )
                run.summary = summary
                run.status = "done"
                run.events.put({"type": "done", **run.snapshot()})
            except Exception as error:
                run.status = "error"
                run.error = str(error)
                run.events.put({"type": "error", "message": str(error)})
            finally:
                run.events.put(None)

        threading.Thread(target=worker, daemon=True).start()
        return run

    # -- resuming with decisions (apply) --------------------------------------

    def submit_decisions(self, run_id: str, decisions: dict) -> dict:
        run = self.current
        if run is None or run.id != run_id:
            raise LookupError("no such run")
        if run.status != "paused":
            raise RuntimeError(f"run is not awaiting decisions (status={run.status})")

        graph, _ = self._ensure_graph()
        run.status = "applying"
        try:
            cfg = {"configurable": {"thread_id": run.thread_id}}
            final = graph.invoke(Command(resume=decisions or {}), cfg)
            run.apply_result = final.get("apply")
            run.status = "done"
            return run.snapshot()
        except Exception as error:
            run.status = "error"
            run.error = str(error)
            raise

    # -- SSE event stream for a run -------------------------------------------

    def stream(self, run_id: str):
        run = self.current
        if run is None or run.id != run_id:
            raise LookupError("no such run")
        while True:
            event = run.events.get()
            if event is None:
                break
            yield event
