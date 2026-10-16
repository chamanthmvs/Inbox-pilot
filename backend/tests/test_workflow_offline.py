"""
Offline Stage-3 verification — no real Gmail, no Claude, no network.

Covers the LabelExecutor (real + dry-run + audit + label cache), and the full
LangGraph workflow through a real interrupt/resume cycle with a fake Gmail
client. Run from backend/:  python tests/test_workflow_offline.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.classifier.runner import build_classifier
from app.classifier.taxonomy import Taxonomy
from app.graph.approval import ApprovalService
from app.graph.autopilot import autopilot_decide
from app.graph.executor import LabelExecutor
from app.graph.workflow import build_workflow
from app.ingestion.service import IngestionService
from app.models.message import MessageMeta
from app.store.db import Store

TAX_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "taxonomy.yaml")


def check(name, condition):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        raise SystemExit(f"Test failed: {name}")


def meta(mid, frm="", subject=""):
    return MessageMeta(id=mid, thread_id="t", from_addr=frm, subject=subject)


# --- fake Gmail --------------------------------------------------------------

def _raw(mid, frm, subject):
    return {
        "id": mid,
        "threadId": "t" + mid,
        "labelIds": ["INBOX"],
        "snippet": subject,
        "internalDate": "1700000000000",
        "sizeEstimate": 1000,
        "historyId": "100",
        "payload": {"headers": [
            {"name": "From", "value": frm},
            {"name": "To", "value": "me@example.com"},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Fri, 2 Oct 2026 09:08:19 +0530"},
        ]},
    }


class FakeGmail:
    def __init__(self, messages):
        self._messages = messages  # id -> (from, subject)
        self.label_ids = {}
        self.create_calls = 0
        self.modify_calls = []  # (message_id, add_label_ids)

    def get_profile(self):
        return {"emailAddress": "me@example.com", "historyId": "100"}

    def list_messages(self, max_results=100, page_token=None):
        return [{"id": i} for i in self._messages], None

    def get_message_metadata(self, message_id, headers=None):
        frm, subject = self._messages[message_id]
        return _raw(message_id, frm, subject)

    def get_or_create_label(self, name):
        if name not in self.label_ids:
            self.create_calls += 1
            self.label_ids[name] = f"Label_{len(self.label_ids) + 1}"
        return self.label_ids[name]

    def modify_message_labels(self, message_id, add_label_ids=None, remove_label_ids=None):
        self.modify_calls.append((message_id, tuple(add_label_ids or [])))
        return {"id": message_id, "labelIds": list(add_label_ids or [])}

    def batch_modify_labels(self, message_ids, add_label_ids=None, remove_label_ids=None, chunk=1000):
        for mid in message_ids:
            self.modify_calls.append((mid, tuple(add_label_ids or []), tuple(remove_label_ids or [])))


# --- executor tests ----------------------------------------------------------

def _seed_approved(store, items):
    for mid, label, status in items:
        store.upsert_message(meta(mid))
        store.upsert_proposal(mid, label, "rules", 1.0, "r", "PENDING")
        store.set_proposal_status(mid, status)


def test_executor_real():
    print("LabelExecutor (real mode + cache + audit):")
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        client = FakeGmail({})
        _seed_approved(store, [
            ("a1", "Personal", "APPROVED"),
            ("a2", "Personal", "APPROVED"),
            ("a3", "Finance/Statements", "APPROVED"),
            ("a4", "Shopping/Orders & Delivery", "PENDING"),  # not approved
        ])
        res = LabelExecutor(client, store).apply_approved()
        check("applied 3", res.applied == 3 and res.errors == 0)
        check("modify called 3 times", len(client.modify_calls) == 3)
        check("label cache → only 2 creates", client.create_calls == 2)
        check("a1 now APPLIED", store.get_proposal("a1")["status"] == "APPLIED")
        check("a4 untouched (PENDING)", store.get_proposal("a4")["status"] == "PENDING")
        check("audit has 3 applied rows", store.audit_count() == 3)
        store.close()


def test_executor_dry_run():
    print("LabelExecutor (dry-run changes nothing):")
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        client = FakeGmail({})
        _seed_approved(store, [("a1", "Personal", "APPROVED")])
        res = LabelExecutor(client, store, dry_run=True).apply_approved()
        check("counted as dry_run", res.dry_run == 1 and res.applied == 0)
        check("no Gmail calls", len(client.modify_calls) == 0 and client.create_calls == 0)
        check("still APPROVED (not applied)", store.get_proposal("a1")["status"] == "APPROVED")
        check("audit outcome is dry_run", store.list_audit()[0]["outcome"] == "dry_run")
        store.close()


# --- full workflow test ------------------------------------------------------

def test_executor_actions():
    print("LabelExecutor (label / archive / trash actions):")
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        client = FakeGmail({})
        for mid, label, action in [("a1", "Personal", "label"),
                                    ("a2", "Promotions", "archive"),
                                    ("a3", "Promotions", "trash")]:
            store.upsert_message(meta(mid))
            store.upsert_proposal(mid, label, "rules", 1.0, "r", "PENDING")
            store.set_proposals_action([mid], action)
            store.set_proposal_status(mid, "APPROVED")

        res = LabelExecutor(client, store).apply_approved()
        check("applied 3", res.applied == 3 and res.errors == 0)

        calls = {c[0]: c for c in client.modify_calls}
        check("label: a1 adds a label, removes nothing",
              calls["a1"][1] and calls["a1"][2] == ())
        check("archive: a2 removes INBOX", "INBOX" in calls["a2"][2])
        check("trash: a3 adds TRASH", "TRASH" in calls["a3"][1])

        actions = {r["action"] for r in store.list_audit()}
        check("audit records all three actions", actions == {"label", "archive", "trash"})
        check("all three APPLIED",
              all(store.get_proposal(m)["status"] == "APPLIED" for m in ["a1", "a2", "a3"]))
        store.close()


def test_autopilot_and_undo():
    print("Auto-Pilot (balanced) + Undo:")
    tax = Taxonomy.load(TAX_PATH)
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        client = FakeGmail({})
        seed = [
            ("p1", "Personal", "rules", 1.0, "PENDING"),      # confident keep → label
            ("p2", "Promotions", "llm", 0.95, "PENDING"),     # confident noise → archive
            ("p3", "Personal", "llm", 0.50, "PENDING"),       # not confident → review
            ("p4", "Shopping/Orders & Delivery", "needs_review", 0.0, "NEEDS_REVIEW"),
        ]
        for mid, label, method, conf, status in seed:
            store.upsert_message(meta(mid))
            store.upsert_proposal(mid, label, method, conf, "r", status)

        decided = autopilot_decide(store, tax, threshold=0.85, level="balanced")
        check("auto-labeled 1 (p1)", decided["auto_label"] == 1)
        check("auto-archived 1 (p2)", decided["auto_archive"] == 1)
        check("left 1 for review (p3)", decided["left_for_review"] == 1)

        res = LabelExecutor(client, store).apply_approved()
        check("applied 2", res.applied == 2)
        check("p3 still pending", store.get_proposal("p3")["status"] == "PENDING")
        calls = {c[0]: c for c in client.modify_calls}
        check("p2 archived (removes INBOX)", "INBOX" in calls["p2"][2])
        check("batch_id recorded", bool(res.batch_id))

        undo = LabelExecutor(client, store).undo_last()
        check("undo reversed 2", undo["undone"] == 2)
        check("p1 back to PENDING", store.get_proposal("p1")["status"] == "PENDING")
        check("p2 back to PENDING", store.get_proposal("p2")["status"] == "PENDING")
        store.close()


def test_workflow_interrupt_resume():
    print("Workflow (ingest → classify → interrupt → resume → apply):")
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        client = FakeGmail({
            "m1": ("Naukri <alerts@naukri.com>", "Top openings for you"),  # rule: Job/Applications & Alerts
            "m2": ("Bank <no-reply@bank.com>", "Your monthly statement is ready"),  # rule: Finance/Statements
            "m3": ("Friend <friend@example.com>", "dinner sunday?"),  # no rule, LLM off → NEEDS_REVIEW
        })
        classifier, _ = build_classifier(taxonomy_path=TAX_PATH, use_llm=False)
        graph = build_workflow(
            store=store,
            ingestion=IngestionService(client, store),
            classifier=classifier,
            approval=ApprovalService(store),
            executor=LabelExecutor(client, store),
            checkpointer=MemorySaver(),
        )
        cfg = {"configurable": {"thread_id": "t1"}}

        state = graph.invoke({"max_messages": 10, "reclassify": False}, cfg)
        check("paused at review (interrupt)", "__interrupt__" in state)
        payload = state["__interrupt__"][0].value
        check("2 pending surfaced", len(payload["pending"]) == 2)
        check("1 needs_review surfaced", len(payload["needs_review"]) == 1)
        check("store has 3 messages", store.count_messages() == 3)

        # Human approves m1, rejects m2.
        final = graph.invoke(Command(resume={"m1": "APPROVED", "m2": "REJECTED"}), cfg)
        check("apply applied exactly 1", final["apply"]["applied"] == 1)
        check("m1 APPLIED", store.get_proposal("m1")["status"] == "APPLIED")
        check("m2 REJECTED", store.get_proposal("m2")["status"] == "REJECTED")
        check("m3 still NEEDS_REVIEW", store.get_proposal("m3")["status"] == "NEEDS_REVIEW")
        check("only m1 was labelled in Gmail",
              len(client.modify_calls) == 1 and client.modify_calls[0][0] == "m1")
        # (archive/trash actions are covered in the classifier/executor tests)
        check("audit recorded the apply", store.audit_count() == 1)
        store.close()


if __name__ == "__main__":
    test_executor_real()
    test_executor_dry_run()
    test_executor_actions()
    test_autopilot_and_undo()
    test_workflow_interrupt_resume()
    print("\nAll Stage-3 offline tests passed.")
