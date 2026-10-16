"""
ApprovalService — the human-in-the-loop surface over the proposals table.

Thin, deliberate wrapper the API (Stage 4) and the workflow's review node call:
list what awaits a human, and record their verdicts. It never touches Gmail.
"""


class ApprovalService:
    def __init__(self, store):
        self.store = store

    def pending(self) -> list[dict]:
        """Proposals with a suggested label, awaiting approval."""
        return self.store.list_proposals("PENDING")

    def needs_review(self) -> list[dict]:
        """Proposals the classifier couldn't decide — a human must label these."""
        return self.store.list_proposals("NEEDS_REVIEW")

    def approve(self, message_id: str) -> None:
        self.store.approve_proposal(message_id)

    def reject(self, message_id: str) -> None:
        self.store.reject_proposal(message_id)

    def set_label(self, message_id: str, label: str) -> None:
        """
        Human override: assign a label to a (usually NEEDS_REVIEW) message and
        approve it in one step, keyed on the message id.
        """
        existing = self.store.get_proposal(message_id)
        method = "human"
        reason = "set by human reviewer"
        self.store.upsert_proposal(
            message_id=message_id,
            label=label,
            method=method,
            confidence=1.0,
            reason=reason,
            status="PENDING",
        )
        self.store.approve_proposal(message_id)

    def decide_group(self, domain: str, label: str, verdict: str, new_label: str = None, action: str = "label") -> dict:
        """
        Bulk approve/reject a whole (domain, label) group of PENDING proposals.
        Optionally RELABEL the group first (new_label) — e.g. the classifier put a
        sender under the wrong label and you fix all of them in one go.
        On APPROVED, remember the (new_)label as a learned rule so this sender
        classifies for free next time (purpose-aware — multi-purpose domains
        aren't blanketed).
        """
        verdict = str(verdict).upper()
        group = next(
            (
                g
                for g in self.store.grouped_proposals("PENDING")
                if g["domain"] == (domain or "") and g["label"] == label
            ),
            None,
        )
        if group is None:
            return {"updated": 0, "verdict": verdict}

        ids = group["message_ids"]
        final_label = label

        if verdict == "APPROVED":
            if new_label and new_label != label:
                self.store.relabel_proposals(ids, new_label)
                final_label = new_label
            action = (action or "label").lower()
            if action not in ("label", "archive", "trash"):
                action = "label"
            self.store.set_proposals_action(ids, action)
            self.store.update_proposals_status(ids, "APPROVED")
            self.store.add_learned_rule(domain, final_label)
            return {"updated": len(ids), "verdict": verdict, "domain": domain,
                    "label": final_label, "action": action}
        elif verdict == "REJECTED":
            self.store.update_proposals_status(ids, "REJECTED")
        else:
            return {"updated": 0, "verdict": verdict}

        return {"updated": len(ids), "verdict": verdict, "domain": domain, "label": final_label}

    def decide_slice(self, label, domain=None, keyword=None, verdict="APPROVED",
                     action="label", new_label=None) -> dict:
        """
        Act on a SLICE of a label (optionally narrowed by sender domain and/or a
        subject keyword) — approve/reject with an action, optionally relabel.
        """
        verdict = str(verdict).upper()
        ids = self.store.slice_ids(label, domain=domain, keyword=keyword, status="PENDING")
        if not ids:
            return {"updated": 0, "verdict": verdict}

        final_label = label
        if verdict == "APPROVED":
            if new_label and new_label != label:
                self.store.relabel_proposals(ids, new_label)
                final_label = new_label
            action = (action or "label").lower()
            if action not in ("label", "archive", "trash"):
                action = "label"
            self.store.set_proposals_action(ids, action)
            self.store.update_proposals_status(ids, "APPROVED")
            if domain:  # a per-sender decision is safe to learn
                self.store.add_learned_rule(domain, final_label)
            return {"updated": len(ids), "verdict": verdict, "label": final_label, "action": action}
        elif verdict == "REJECTED":
            self.store.update_proposals_status(ids, "REJECTED")
            return {"updated": len(ids), "verdict": verdict}
        return {"updated": 0, "verdict": verdict}

    def apply_decisions(self, decisions: dict) -> dict:
        """
        Apply a {message_id: verdict} map, where verdict is 'APPROVED' or
        'REJECTED'. Returns counts. Unknown verdicts are ignored.
        """
        counts = {"APPROVED": 0, "REJECTED": 0, "ignored": 0}
        for message_id, verdict in (decisions or {}).items():
            v = str(verdict).upper()
            if v == "APPROVED":
                self.approve(message_id)
                counts["APPROVED"] += 1
            elif v == "REJECTED":
                self.reject(message_id)
                counts["REJECTED"] += 1
            else:
                counts["ignored"] += 1
        return counts
