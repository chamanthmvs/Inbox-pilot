"""
Offline Stage-2 verification — no real Claude call, no network.

Covers: taxonomy loading, the rules classifier (incl. most-specific wins), the
3-state orchestrator with a fake LLM, the proposals store, and the skip-decided
behaviour of classify_store. Run from backend/:

    python tests/test_classifier_offline.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.classifier.classifier import Classifier
from app.classifier.llm import LLMMatch
from app.classifier.rules import RuleMatch, RulesClassifier
from app.classifier.runner import classify_store
from app.classifier.taxonomy import LabelDef, Taxonomy
from app.models.message import MessageMeta
from app.store.db import Store

TAX_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "taxonomy.yaml")


def check(name, condition):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        raise SystemExit(f"Test failed: {name}")


def msg(mid="x", frm="", subject="", snippet=""):
    return MessageMeta(id=mid, thread_id="t", from_addr=frm, subject=subject, snippet=snippet)


# --- fakes for the LLM -------------------------------------------------------

class FakeLLM:
    def __init__(self, result, enabled=True):
        self._result = result
        self.enabled = enabled
        self.model = "fake"

    def classify(self, meta):
        return self._result


class LLMHit:
    def __init__(self, label, confidence, reason):
        self.label, self.confidence, self.reason = label, confidence, reason


class FakeBatchLLM:
    """A batched LLM: labels everything 'Personal' except subjects with 'mystery'."""
    enabled = True
    model = "fake"

    def classify_batch(self, metas, hints=None):
        out = {}
        for m in metas:
            if "mystery" in (m.subject or "").lower():
                out[m.id] = None  # not confident → NEEDS_REVIEW
            else:
                out[m.id] = LLMMatch("Personal", 0.9, "llm: looks personal")
        return out

    def classify(self, meta):
        return self.classify_batch([meta]).get(meta.id)


# --- tests -------------------------------------------------------------------

def test_taxonomy():
    print("Taxonomy load:")
    tax = Taxonomy.load(TAX_PATH)
    names = tax.names()
    check("loads labels", len(names) > 10)
    check("has top-level Finance", "Finance" in names)
    check("has sub-label Job/Offers & Documents", "Job/Offers & Documents" in names)
    check("menu is (name, desc) pairs", all(len(x) == 2 for x in tax.menu()))
    check("is_valid rejects junk", not tax.is_valid("Nope/Nope"))


def test_rules_real_taxonomy():
    print("Rules (real taxonomy):")
    tax = Taxonomy.load(TAX_PATH)
    rules = RulesClassifier(tax)

    naukri = msg(frm="Naukri <naukrialerts@naukri.com>", subject="Top Openings for Data Analyst")
    m = rules.classify(naukri)
    check("naukri → Job/Applications & Alerts", m and m.label == "Job/Applications & Alerts")

    bank = msg(frm="Bank <noreply@bank.com>", subject="Your monthly e-statement is ready")
    m = rules.classify(bank)
    check("statement subject → Finance/Statements", m and m.label == "Finance/Statements")

    person = msg(frm="Ravi Kumar <ravi@gmail.com>", subject="catching up this weekend?")
    m = rules.classify(person)
    check("human sender on gmail → Personal", m and m.label == "Personal")

    bulk = msg(frm="noreply@gmail.com", subject="verify your account")
    check("no-reply gmail → NOT personal", (rules.classify(bulk) or RuleMatch("", "")).label != "Personal"
          or rules.classify(bulk) is None)

    none = msg(frm="info@randomcorp.xyz", subject="dinner on sunday?")
    check("unknown corp no-match → None (goes to LLM)", rules.classify(none) is None)


def test_sender_domain_folders():
    print("Sender-domain folders (route by WHO sent it, not subject words):")
    tax = Taxonomy.load(TAX_PATH)
    r = RulesClassifier(tax)
    def L(frm, subj=""):
        m = r.classify(msg(frm=frm, subject=subj))
        return m.label if m else None
    check("github.com sender → Developer Tools/GitHub", L("noreply@github.com", "anything") == "Developer Tools/GitHub")
    check("'github' in subject from a person is NOT GitHub",
          L("Ravi <ravi@gmail.com>", "check my github repo") != "Developer Tools/GitHub")
    check("vercel.com → Deployment/Vercel", L("ship@vercel.com", "deploy done") == "Deployment/Vercel")
    check("render.com → Deployment/Render", L("x@render.com", "live") == "Deployment/Render")
    check("anthropic.com → AI Tools/Claude", L("x@anthropic.com", "invoice") == "AI Tools/Claude")
    check("openai.com → AI Tools/OpenAI", L("x@openai.com", "receipt") == "AI Tools/OpenAI")
    check("groq.com → AI Tools/Groq", L("x@groq.com", "welcome") == "AI Tools/Groq")
    check("zerodha domain wins across categories", L("x@zerodha.com", "redemption report") == "Zerodha")


def test_rules_most_specific():
    print("Rules (most-specific wins):")
    tax = Taxonomy(
        labels=[
            LabelDef("Parent", "", True, {"subject_contains": ["foo"]}, depth=0),
            LabelDef("Parent/Child", "", True, {"subject_contains": ["foo"]}, depth=1),
        ],
        needs_review_label="Needs Review",
    )
    rules = RulesClassifier(tax)
    m = rules.classify(msg(subject="this has foo in it"))
    check("child (depth 1) beats parent", m and m.label == "Parent/Child")


def test_orchestrator_states():
    print("Orchestrator (3 states):")
    tax = Taxonomy.load(TAX_PATH)
    rules = RulesClassifier(tax)

    # 1) rules win
    c = Classifier(tax, rules, FakeLLM(None)).classify(
        msg(frm="x@naukri.com", subject="job alert")
    )
    check("rules → PENDING/rules", c.status == "PENDING" and c.method == "rules" and c.confidence == 1.0)

    # 2) no rule, LLM confident → PENDING/llm
    c = Classifier(tax, rules, FakeLLM(LLMHit("Personal", 0.9, "looks personal"))).classify(
        msg(frm="friend@example.com", subject="hey")
    )
    check("llm → PENDING/llm", c.status == "PENDING" and c.method == "llm" and c.label == "Personal")

    # 3) no rule, LLM returns None → NEEDS_REVIEW
    c = Classifier(tax, rules, FakeLLM(None)).classify(
        msg(frm="friend@example.com", subject="hey")
    )
    check("llm unsure → NEEDS_REVIEW", c.status == "NEEDS_REVIEW" and c.method == "needs_review")

    # 4) LLM disabled → NEEDS_REVIEW with 'disabled' reason
    c = Classifier(tax, rules, FakeLLM(None, enabled=False)).classify(
        msg(frm="friend@example.com", subject="hey")
    )
    check("llm disabled → NEEDS_REVIEW", c.status == "NEEDS_REVIEW")
    check("disabled reason mentions key", "ANTHROPIC_API_KEY" in c.reason)


def test_real_world_examples():
    print("Real-world examples (the ones that were misclassified):")
    tax = Taxonomy.load(TAX_PATH)
    rules = RulesClassifier(tax)

    cases = [
        ("noreply@idfcfirst.bank.in", "E-Mandate Set", "Bank/Alerts & Notifications"),
        ("noreply@idfcfirstbank.com", "Debit Alert: Your IDFC FIRST Bank Credit Card", "Bank/Alerts & Notifications"),
        ("nse_alerts@nse.co.in", "Funds/Securities Balance", "Finance/Securities & Demat"),
        ("sangeetha.naveen@akshayapatra.org", "Honour Your Ancestors by Feeding Children with Akshaya Patra", "Donations"),
        ("noreply@rmt.flipkart.com", "Your Order for Google Fitbit Air has been successfully placed", "Shopping/Orders & Delivery"),
        ("samf@kfintech.com", "Monthly portfolio disclosure of Motilal Oswal Mutual Fund", "Finance/Investments & Insurance"),
        ("noreply-coin@qmailer.zerodha.net", "Coin by Zerodha - Redemption report - 06-10-2026", "Zerodha"),
        ("noreply@idfcfirst.bank.in", "Thank you for banking with us", "Bank"),  # parent only
    ]
    for frm, subject, expected in cases:
        m = rules.classify(msg(frm=frm, subject=subject))
        got = m.label if m else None
        check(f"{subject[:38]!r:42} → {expected}", got == expected)

    # The two substring bugs must stay fixed:
    check("'irs' does NOT match 'idfcfirst'",
          rules.classify(msg(frm="noreply@idfcfirst.bank.in", subject="E-Mandate Set")).label != "Finance/Tax & Government")
    check("'fee' does NOT match 'Feeding'",
          "Education" not in (rules.classify(msg(frm="x@akshayapatra.org", subject="Feeding Children")).label or ""))


def test_llm_batch_path():
    print("Runner: rules-first then batched LLM:")
    tax = Taxonomy.load(TAX_PATH)
    classifier = Classifier(tax, RulesClassifier(tax), FakeBatchLLM())
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        store.upsert_message(msg("a1", frm="x@naukri.com", subject="jobs for you"))   # rules
        store.upsert_message(msg("a2", frm="friend@example.com", subject="hello"))     # llm → Personal
        store.upsert_message(msg("a3", frm="friend@example.com", subject="mystery"))   # llm abstains → review
        res = classify_store(store, classifier)
        check("a1 via rules", store.get_proposal("a1")["method"] == "rules")
        check("a2 via llm → Personal",
              store.get_proposal("a2")["method"] == "llm" and store.get_proposal("a2")["label"] == "Personal")
        check("a3 → NEEDS_REVIEW", store.get_proposal("a3")["status"] == "NEEDS_REVIEW")
        check("method mix", res.by_method.get("rules") == 1 and res.by_method.get("llm") == 1 and res.by_method.get("needs_review") == 1)
        store.close()


def test_ai_vetted_rules():
    print("AI-vetted rules (rules become hints; AI decides):")
    tax = Taxonomy.load(TAX_PATH)

    class VetLLM:
        enabled = True
        model = "fake"
        seen_hints = {}
        def classify_batch(self, metas, hints=None):
            VetLLM.seen_hints.update(hints or {})
            out = {}
            for m in metas:
                # The AI CORRECTS a wrong rule hint, and abstains on "weird".
                if "weird" in (m.subject or "").lower():
                    out[m.id] = None
                else:
                    out[m.id] = LLMMatch("Personal", 0.92, "llm: looks personal")
            return out

    classifier = Classifier(tax, RulesClassifier(tax), VetLLM())
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        # a1 would match a rule (naukri→Job/Applications); AI overrides to Personal
        store.upsert_message(msg("a1", frm="x@naukri.com", subject="jobs for you"))
        store.upsert_message(msg("a2", frm="y@x.com", subject="weird thing"))
        classify_store(store, classifier, verify_rules=True)
        p1 = store.get_proposal("a1")
        check("rule was passed to AI as a hint", VetLLM.seen_hints.get("a1") == "Job/Applications & Alerts")
        check("AI decision wins (method ai-vetted)", p1["method"] == "ai-vetted" and p1["label"] == "Personal")
        check("no confident AI → NEEDS_REVIEW", store.get_proposal("a2")["status"] == "NEEDS_REVIEW")
        store.close()


def test_learned_rules():
    print("Learned rules (phase 0 + multi-purpose guard):")
    tax = Taxonomy.load(TAX_PATH)
    classifier = Classifier(tax, RulesClassifier(tax), FakeBatchLLM())
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        store.add_learned_rule("example.com", "Personal")
        store.upsert_message(msg("a1", frm="someone@example.com", subject="whatever"))
        classify_store(store, classifier)
        check("single learned label applied",
              store.get_proposal("a1")["method"] == "learned" and store.get_proposal("a1")["label"] == "Personal")

        # Make the domain multi-purpose → it must NOT be blanket-labelled.
        store.add_learned_rule("example.com", "Promotions")
        store.upsert_message(msg("a2", frm="x@example.com", subject="mystery"))
        classify_store(store, classifier, reclassify=True)
        check("multi-purpose domain falls through (not learned)",
              store.get_proposal("a2")["method"] != "learned")
        store.close()


def test_group_decide():
    print("Grouping + bulk decide + learned rule:")
    from app.graph.approval import ApprovalService
    tax = Taxonomy.load(TAX_PATH)
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        store.upsert_message(msg("n1", frm="a@naukri.com", subject="jobs for you"))
        store.upsert_message(msg("n2", frm="b@naukri.com", subject="openings for data"))
        classify_store(store, Classifier(tax, RulesClassifier(tax), None))  # rules only
        groups = store.grouped_proposals("PENDING")
        check("one naukri group of 2",
              any(g["domain"] == "naukri.com" and g["count"] == 2 for g in groups))
        res = ApprovalService(store).decide_group("naukri.com", "Job/Applications & Alerts", "APPROVED")
        check("bulk approved 2", res["updated"] == 2)
        check("both now APPROVED",
              store.get_proposal("n1")["status"] == "APPROVED" and store.get_proposal("n2")["status"] == "APPROVED")
        check("learned rule remembered", store.learned_label_for("naukri.com") == "Job/Applications & Alerts")

        # Relabel a group before approving (classifier put it in the wrong bucket).
        store.upsert_message(msg("z1", frm="x@zomato.com", subject="50% off today"))
        classify_store(store, Classifier(tax, RulesClassifier(tax), None))  # → Promotions
        before = store.get_proposal("z1")["label"]
        ApprovalService(store).decide_group("zomato.com", before, "APPROVED", new_label="Personal")
        check("relabel applied + approved",
              store.get_proposal("z1")["label"] == "Personal" and store.get_proposal("z1")["status"] == "APPROVED")
        check("learned the relabel", store.learned_label_for("zomato.com") == "Personal")
        store.close()


def test_full_ingest():
    print("Full background ingest + classify (resilient/resumable):")
    from app.ingestion.full import full_ingest_and_classify

    class FG:
        def get_profile(self):
            return {"emailAddress": "me@x.com", "historyId": "10", "messagesTotal": 5}

        def list_messages(self, max_results=100, page_token=None):
            return [{"id": f"m{i}"} for i in range(5)], None

        def _raw(self, mid):
            return {
                "id": mid, "threadId": "t", "labelIds": ["INBOX"], "snippet": "hi",
                "internalDate": "1", "sizeEstimate": 1, "historyId": "10",
                "payload": {"headers": [
                    {"name": "From", "value": "a@naukri.com"},
                    {"name": "Subject", "value": "jobs for you"},
                    {"name": "Date", "value": "x"},
                ]},
            }

        def get_message_metadata(self, mid, headers=None):
            return self._raw(mid)

        def get_messages_metadata_batch(self, ids, headers=None, batch_size=50, max_retries=6):
            return {mid: self._raw(mid) for mid in ids}

    tax = Taxonomy.load(TAX_PATH)
    classifier = Classifier(tax, RulesClassifier(tax), None)  # rules only
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        events = []
        summary = full_ingest_and_classify(FG(), store, classifier, on_progress=events.append, batch_size=2, page_pause=0)
        check("ingested 5", summary["ingested"] == 5)
        check("classified 5", summary["classified"] == 5)
        check("store has 5 messages", store.count_messages() == 5)
        check("5 proposals written", len(store.list_proposals()) == 5)
        check("progress reported total", any(e.get("total") == 5 for e in events))

        s2 = full_ingest_and_classify(FG(), store, classifier, batch_size=2, page_pause=0)
        check("resume re-ingests 0", s2["ingested"] == 0)
        check("no duplicate proposals", len(store.list_proposals()) == 5)
        store.close()


def test_drill_and_slice():
    print("Drill-down facets + slice decide:")
    from app.graph.approval import ApprovalService
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        data = [
            ("d1", "alerts@sbi.co.in", "Debit alert on your account"),
            ("d2", "alerts@sbi.co.in", "Debit alert received today"),
            ("d3", "noreply@hdfcbank.net", "Credit card statement ready"),
            ("d4", "alerts@sbi.co.in", "E-Mandate set successfully"),
        ]
        for mid, frm, subj in data:
            store.upsert_message(msg(mid, frm=frm, subject=subj))
            store.upsert_proposal(mid, "Bank/Alerts & Notifications", "llm", 0.9, "r", "PENDING")

        drill = store.drilldown("Bank/Alerts & Notifications")
        check("drill total is 4", drill["total"] == 4)
        check("top sender is sbi.co.in (3)",
              drill["senders"][0]["domain"] == "sbi.co.in" and drill["senders"][0]["count"] == 3)
        check("keywords include 'debit alert'",
              "debit alert" in {k["keyword"] for k in drill["keywords"]})

        res = ApprovalService(store).decide_slice(
            "Bank/Alerts & Notifications", keyword="debit alert", verdict="APPROVED", action="label")
        check("approved the 'debit alert' slice (2)", res["updated"] == 2)
        check("d1 approved", store.get_proposal("d1")["status"] == "APPROVED")
        check("d3 untouched (pending)", store.get_proposal("d3")["status"] == "PENDING")
        store.close()


def test_segregate():
    print("AI re-segregate (split a label into sub-labels):")
    from app.classifier.segregate import segregate_label
    tax = Taxonomy.load(TAX_PATH)
    PARENT = "Bank/Alerts & Notifications"

    class SegLLM:
        enabled = True
        model = "fake"
        def propose_sublabels(self, parent, samples, n=6):
            return [{"name": f"{parent}/Debit", "description": "debit"},
                    {"name": f"{parent}/Statements", "description": "statements"}]
        def assign_batch(self, metas, options):
            out = {}
            for m in metas:
                s = (m.subject or "").lower()
                if "debit" in s:
                    out[m.id] = LLMMatch(options[0], 0.9, "x")
                elif "statement" in s:
                    out[m.id] = LLMMatch(options[1], 0.9, "x")
                else:
                    out[m.id] = None
            return out

    classifier = Classifier(tax, RulesClassifier(tax), SegLLM())
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        rows = [("s1", "Debit alert received"), ("s2", "Debit alert on card"),
                ("s3", "Monthly statement ready"), ("s4", "Something else")]
        for mid, subj in rows:
            store.upsert_message(msg(mid, frm="x@sbi.co.in", subject=subj))
            store.upsert_proposal(mid, PARENT, "rules", 1.0, "r", "PENDING")

        summary = segregate_label(store, classifier, PARENT,
                                  taxonomy_path=os.path.join(d, "taxonomy.yaml"))
        check("moved 3 into sub-labels", summary["moved"] == 3)
        check("1 unchanged", summary["unchanged"] == 1)
        check("s1 relabeled to Debit sub-label",
              store.get_proposal("s1")["label"] == f"{PARENT}/Debit")
        check("s4 stays on parent", store.get_proposal("s4")["label"] == PARENT)
        check("sub-labels persisted locally", os.path.exists(os.path.join(d, "taxonomy.local.yaml")))
        store.close()


def test_proposals_store_and_skip():
    print("Proposals store + classify_store skip-decided:")
    tax = Taxonomy.load(TAX_PATH)
    classifier = Classifier(tax, RulesClassifier(tax), FakeLLM(None, enabled=False))

    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        store.upsert_message(msg("a1", frm="x@naukri.com", subject="jobs for you"))
        store.upsert_message(msg("a2", frm="friend@example.com", subject="lunch?"))

        events = []
        res = classify_store(store, classifier, on_progress=events.append)
        check("processed both", res.processed == 2)
        check("one rules, one needs_review",
              res.by_method.get("rules") == 1 and res.by_method.get("needs_review") == 1)
        check("progress emitted (start+done)", len(events) >= 2)

        p = store.get_proposal("a1")
        check("a1 proposal is Job/Applications & Alerts",
              p["label"] == "Job/Applications & Alerts" and p["status"] == "PENDING")

        counts = store.proposal_counts()
        check("counts has PENDING + NEEDS_REVIEW",
              counts.get("PENDING") == 1 and counts.get("NEEDS_REVIEW") == 1)

        # Simulate a human decision, then re-run: it must be skipped.
        store.conn.execute("UPDATE proposals SET status='APPROVED' WHERE message_id='a1'")
        store.conn.commit()
        check("has_decided_proposal true for a1", store.has_decided_proposal("a1"))

        res2 = classify_store(store, classifier)
        check("re-run skips the decided one", res2.skipped_decided == 1 and res2.processed == 1)
        check("decision preserved", store.get_proposal("a1")["status"] == "APPROVED")
        store.close()


if __name__ == "__main__":
    test_taxonomy()
    test_rules_real_taxonomy()
    test_rules_most_specific()
    test_sender_domain_folders()
    test_real_world_examples()
    test_orchestrator_states()
    test_llm_batch_path()
    test_ai_vetted_rules()
    test_learned_rules()
    test_group_decide()
    test_full_ingest()
    test_drill_and_slice()
    test_segregate()
    test_proposals_store_and_skip()
    print("\nAll Stage-2 offline tests passed.")
