"""
Offline Stage-4 verification — real FastAPI app via TestClient, fake Gmail,
no network. Exercises the whole HTTP flow:

    start run → (background) ingest+classify → pause → list proposals →
    stream progress (SSE) → submit decisions → apply → audit.

Run from backend/:  python tests/test_api_offline.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.manager import RunManager
from app.settings import Settings


def check(name, condition):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        raise SystemExit(f"Test failed: {name}")


def _raw(mid, frm, subject):
    return {
        "id": mid, "threadId": "t" + mid, "labelIds": ["INBOX"],
        "snippet": subject, "internalDate": "1700000000000",
        "sizeEstimate": 1000, "historyId": "100",
        "payload": {"headers": [
            {"name": "From", "value": frm},
            {"name": "To", "value": "me@example.com"},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Fri, 2 Oct 2026 09:08:19 +0530"},
        ]},
    }


class FakeGmail:
    def __init__(self, messages):
        self._messages = messages
        self.label_ids = {}
        self.modify_calls = []

    def get_profile(self):
        return {"emailAddress": "me@example.com", "historyId": "100"}

    def list_messages(self, max_results=100, page_token=None):
        return [{"id": i} for i in self._messages], None

    def get_message_metadata(self, message_id, headers=None):
        frm, subject = self._messages[message_id]
        return _raw(message_id, frm, subject)

    def get_or_create_label(self, name):
        self.label_ids.setdefault(name, f"Label_{len(self.label_ids) + 1}")
        return self.label_ids[name]

    def modify_message_labels(self, message_id, add_label_ids=None, remove_label_ids=None):
        self.modify_calls.append((message_id, tuple(add_label_ids or [])))
        return {"id": message_id}

    def batch_modify_labels(self, message_ids, add_label_ids=None, remove_label_ids=None, chunk=1000):
        for mid in message_ids:
            self.modify_calls.append((mid, tuple(add_label_ids or []), tuple(remove_label_ids or [])))


def wait_until(fn, timeout=8.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(interval)
    return False


def main():
    with tempfile.TemporaryDirectory() as d:
        settings = Settings(
            db_path=os.path.join(d, "t.db"),
            taxonomy_path=os.path.join(os.path.dirname(__file__), "..", "config", "taxonomy.yaml"),
            use_llm=False,
        )
        client = FakeGmail({
            "m1": ("Naukri <alerts@naukri.com>", "Top openings for you"),
            "m2": ("Bank <no-reply@bank.com>", "Your monthly statement is ready"),
            "m3": ("Friend <friend@example.com>", "dinner sunday?"),
        })
        app = create_app(RunManager(settings, client=client))
        http = TestClient(app)

        print("Basic endpoints:")
        check("health ok", http.get("/api/health").json()["status"] == "ok")
        tax = http.get("/api/taxonomy").json()
        check("taxonomy has labels", len(tax["labels"]) > 10)

        print("Connect-first flow:")
        check("status starts unauthenticated", http.get("/api/status").json()["authenticated"] is False)
        conn = http.post("/api/connect").json()
        check("connect authenticates", conn["authenticated"] is True)
        check("status now authenticated", http.get("/api/status").json()["authenticated"] is True)

        print("Start a run → it pauses for approval:")
        started = http.post("/api/runs", json={"max_messages": 10}).json()
        run_id = started["run_id"]
        check("run id returned", bool(run_id))
        paused = wait_until(lambda: http.get("/api/status").json()["run"]["status"] == "paused")
        check("run reaches paused", paused)

        print("Proposals surfaced:")
        pend = http.get("/api/proposals", params={"status": "PENDING"}).json()["proposals"]
        need = http.get("/api/proposals", params={"status": "NEEDS_REVIEW"}).json()["proposals"]
        check("2 pending", len(pend) == 2)
        check("1 needs_review", len(need) == 1)
        check("proposal carries subject (joined)", pend[0].get("subject") is not None)

        print("Grouped review (domain × label):")
        groups = http.get("/api/groups").json()["groups"]
        check("2 groups (one per sender/label)", len(groups) == 2)
        check("group has domain, label, count, samples",
              all({"domain", "label", "count", "samples", "message_ids"} <= set(g) for g in groups))

        print("SSE progress stream:")
        text = http.get(f"/api/runs/{run_id}/stream").text
        check("stream has progress events", "progress" in text)
        check("stream has paused event", "paused" in text)

        print("Submit decisions → apply:")
        body = {"decisions": {"m1": "APPROVED", "m2": "REJECTED"}}
        res = http.post(f"/api/runs/{run_id}/decisions", json=body).json()
        check("applied exactly 1", res["apply"]["applied"] == 1)
        applied = http.get("/api/proposals", params={"status": "APPLIED"}).json()["proposals"]
        check("m1 is APPLIED", len(applied) == 1 and applied[0]["message_id"] == "m1")
        check("only m1 labelled in Gmail", len(client.modify_calls) == 1 and client.modify_calls[0][0] == "m1")
        check("audit has 1 row", len(http.get("/api/audit").json()["audit"]) == 1)

        print("Direct apply endpoint + dry-run toggle:")
        r = http.post("/api/apply").json()
        check("apply endpoint returns a result", "applied" in r)
        check("status reports llm_enabled flag", "llm_enabled" in http.get("/api/status").json())
        rc = http.post("/api/classify", json={"reclassify": False}).json()
        check("reclassify returns a run", bool(rc.get("run_id")))
        wait_until(lambda: http.get("/api/status").json()["run"]["status"] in ("done", "error"))

        dr = http.post("/api/settings/dry_run", json={"enabled": True}).json()
        check("dry-run toggles on", dr["dry_run"] is True)
        check("status reflects dry-run", http.get("/api/status").json()["dry_run"] is True)
        http.post("/api/settings/dry_run", json={"enabled": False})

        print("Human set-label on the NEEDS_REVIEW one:")
        r = http.post("/api/proposals/m3/label", json={"label": "Personal"})
        check("set_label ok", r.status_code == 200 and r.json()["ok"])
        m3 = [p for p in http.get("/api/proposals").json()["proposals"] if p["message_id"] == "m3"][0]
        check("m3 now APPROVED with label", m3["status"] == "APPROVED" and m3["label"] == "Personal")
        check("reject bad label", http.post("/api/proposals/m3/label", json={"label": "Nope"}).status_code == 400)

    print("\nAll Stage-4 offline tests passed.")


if __name__ == "__main__":
    main()
