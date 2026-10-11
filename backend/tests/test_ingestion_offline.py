"""
Offline Stage-1 verification — no real Gmail, no network.

Exercises the metadata parser, the SQLite store, and the IngestionService
(full + incremental) using a fake client backed by the captured raw_email.json
fixture. Run from backend/:  python tests/test_ingestion_offline.py
"""

import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from app.gmail.parser import parse_metadata
from app.ingestion.service import IngestionService
from app.store.db import Store


# Committed, synthetic fixture (no personal data) so tests run after a clone.
# A real captured message, if present, is gitignored and not required.
FIXTURE = json.load(
    open(os.path.join(os.path.dirname(__file__), "fixtures", "sample_email.json"))
)


def _msg(mid, history_id="100"):
    m = copy.deepcopy(FIXTURE)
    m["id"] = mid
    m["historyId"] = history_id
    return m


class FakeClient:
    """Stand-in for GmailClient with scripted responses."""

    def __init__(self, ids, profile_history="100"):
        self._ids = ids
        self._profile_history = profile_history
        self.history_response = {"historyId": "200", "history": []}

    def get_profile(self):
        return {"emailAddress": "me@example.com", "historyId": self._profile_history}

    def list_messages(self, max_results=100, page_token=None):
        return [{"id": i} for i in self._ids], None

    def get_message_metadata(self, message_id, headers=None):
        return _msg(message_id)

    def list_history(self, start_history_id, page_token=None):
        return self.history_response


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if not condition:
        raise SystemExit(f"Test failed: {name}")


def test_parser():
    print("parse_metadata:")
    meta = parse_metadata(FIXTURE)
    check("sender parsed", meta.from_addr.startswith("Naukri"))
    check("subject parsed", "Openings" in meta.subject)
    check("snippet captured", len(meta.snippet) > 0)
    check("labels captured", "INBOX" in meta.label_ids)
    check("internal_date is int", isinstance(meta.internal_date, int))
    check("history_id is str", isinstance(meta.history_id, str))


def test_store():
    print("Store (upsert / idempotency / sync_state):")
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        meta = parse_metadata(_msg("a1"))
        store.upsert_message(meta)
        store.upsert_message(meta)  # same id again
        check("no duplicate on re-upsert", store.count_messages() == 1)
        check("round-trips to_addrs list", store.get_message("a1").to_addrs == meta.to_addrs)

        store.set_sync_state(email_address="me@example.com", last_history_id="9", mark_full_sync=True)
        store.set_sync_state(last_history_id="10")  # should preserve email + full-sync time
        st = store.get_sync_state()
        check("history id updated", st["last_history_id"] == "10")
        check("email preserved across update", st["email_address"] == "me@example.com")
        check("full-sync time preserved", st["last_full_sync_at"] is not None)
        store.close()


def test_full_sync():
    print("Full sync:")
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        svc = IngestionService(FakeClient(["m1", "m2", "m3"]), store)
        res = svc.run_sync()
        check("mode is full", res.mode == "full")
        check("fetched 3", res.fetched == 3)
        check("store holds 3", store.count_messages() == 3)
        check("anchored history id from profile", store.get_sync_state()["last_history_id"] == "100")
        store.close()


def test_incremental_sync():
    print("Incremental sync (added + deleted):")
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        client = FakeClient(["m1", "m2"])
        svc = IngestionService(client, store)
        svc.run_sync()  # full sync first → history id = 100

        # Next run: m3 added, m1 deleted.
        client.history_response = {
            "historyId": "250",
            "history": [
                {"messagesAdded": [{"message": {"id": "m3"}}]},
                {"messagesDeleted": [{"message": {"id": "m1"}}]},
            ],
        }
        res = svc.run_sync()
        check("mode is incremental", res.mode == "incremental")
        check("fetched 1 (m3)", res.fetched == 1)
        check("deleted 1 (m1)", res.deleted == 1)
        check("m1 gone", not store.has_message("m1"))
        check("m3 present", store.has_message("m3"))
        check("history id advanced to 250", store.get_sync_state()["last_history_id"] == "250")
        store.close()


def test_taxonomy_config():
    print("Taxonomy config:")
    path = os.path.join(os.path.dirname(__file__), "..", "config", "taxonomy.yaml")
    data = yaml.safe_load(open(path))
    names = [c["name"] for c in data["categories"]]
    check("loads as valid YAML", data["version"] >= 1)
    check("has Finance & Job categories", "Finance" in names and "Job & Career" in names)
    check("Job has Offers & Documents child", any(
        ch["name"] == "Job/Offers & Documents"
        for c in data["categories"] if c["name"] == "Job & Career"
        for ch in c["children"]
    ))


if __name__ == "__main__":
    test_parser()
    test_store()
    test_full_sync()
    test_incremental_sync()
    test_taxonomy_config()
    print("\nAll Stage-1 offline tests passed.")
