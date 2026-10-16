"""
LabelExecutor — the ONLY component that changes Gmail, and only for proposals a
human has APPROVED. Everything it does is recorded in the audit log.

  * real mode:  create the label if needed, attach it to the message, mark the
                proposal APPLIED, log `outcome=applied`.
  * dry-run:    touch nothing in Gmail; log `outcome=dry_run`; leave the proposal
                APPROVED so a later real run can apply it.

v1 only ADDS labels. It never removes labels, archives, or deletes.
"""

from dataclasses import dataclass, field


def _emit(on_progress, **event):
    if on_progress is not None:
        try:
            on_progress(event)
        except Exception:
            pass


@dataclass
class ApplyResult:
    applied: int = 0
    dry_run: int = 0
    errors: int = 0
    batch_id: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "errors": self.errors,
            "batch_id": self.batch_id,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        line = f"applied={self.applied} dry_run={self.dry_run} errors={self.errors}"
        if self.notes:
            line += "\n  - " + "\n  - ".join(self.notes)
        return line


class LabelExecutor:
    def __init__(self, client, store, dry_run: bool = False):
        self.client = client
        self.store = store
        self.dry_run = dry_run
        self._label_ids: dict[str, str] = {}  # name → id, cached within a run

    def _label_id(self, name: str) -> str:
        if name not in self._label_ids:
            self._label_ids[name] = self.client.get_or_create_label(name)
        return self._label_ids[name]

    def apply_approved(self, on_progress=None) -> ApplyResult:
        """
        Apply every APPROVED proposal, bucketed by action for bulk efficiency:

          * label   — add the category label
          * archive — add the label (if any) AND remove INBOX (out of inbox)
          * trash   — add TRASH (move to Trash; recoverable ~30 days)

        All are reversible; nothing is permanently deleted. Uses batchModify
        (up to 1000 messages per call).
        """
        import uuid
        from collections import defaultdict

        result = ApplyResult(batch_id=uuid.uuid4().hex)
        approved = self.store.list_proposals("APPROVED")

        # Bucket by (action, label) so identical changes go in one batch.
        buckets: dict = defaultdict(list)
        for p in approved:
            action = (p.get("action") or "label").lower()
            label = p.get("label")
            if action == "label" and not label:
                continue  # nothing to do without a label
            buckets[(action, label)].append(p["message_id"])

        total = sum(len(v) for v in buckets.values())
        _emit(on_progress, phase="start", done=0, total=total)
        done = 0

        for (action, label), ids in buckets.items():
            try:
                if self.dry_run:
                    # Touch NOTHING in Gmail — don't even resolve/create labels.
                    self.store.add_audit_many(
                        [(mid, label, action, "dry_run", f"would {action}") for mid in ids]
                    )
                    result.dry_run += len(ids)
                else:
                    add_ids, remove_ids = [], []
                    if action in ("label", "archive") and label:
                        add_ids.append(self._label_id(label))
                    if action == "archive":
                        remove_ids.append("INBOX")
                    if action == "trash":
                        add_ids.append("TRASH")
                    self.client.batch_modify_labels(ids, add_label_ids=add_ids, remove_label_ids=remove_ids)
                    self.store.update_proposals_status(ids, "APPLIED")
                    self.store.add_audit_many(
                        [(mid, label, action, "applied",
                          f"{action}: +{add_ids} -{remove_ids}") for mid in ids],
                        batch_id=result.batch_id,
                    )
                    result.applied += len(ids)
            except Exception as error:  # a bad bucket never stops the rest
                self.store.add_audit_many(
                    [(mid, label, action, "error", str(error)) for mid in ids]
                )
                result.errors += len(ids)

            done += len(ids)
            _emit(on_progress, phase="item", done=done, total=total,
                  label=label, action=action, dry_run=self.dry_run)

        if self.dry_run and total:
            result.notes.append("dry-run: nothing was changed in Gmail")
        _emit(on_progress, phase="done", done=total, total=total)
        return result

    def undo_last(self, on_progress=None) -> dict:
        """
        Reverse the most recent apply run: remove labels we added, restore INBOX
        for archived mail, untrash trashed mail. Puts those proposals back to
        PENDING so they re-enter review. Everything we did was reversible.
        """
        import uuid
        from collections import defaultdict

        batch_id = self.store.last_applied_batch()
        if not batch_id:
            return {"undone": 0, "note": "nothing to undo"}

        rows = self.store.audit_for_batch(batch_id)
        buckets: dict = defaultdict(list)
        for r in rows:
            buckets[(r.get("action") or "label", r.get("label"))].append(r["message_id"])

        undo_batch = uuid.uuid4().hex
        undone = 0
        total = sum(len(v) for v in buckets.values())
        _emit(on_progress, phase="start", done=0, total=total)

        for (action, label), ids in buckets.items():
            add_ids, remove_ids = [], []
            if action in ("label", "archive") and label:
                remove_ids.append(self._label_id(label))  # take the label back off
            if action == "archive":
                add_ids.append("INBOX")                    # put it back in the inbox
            if action == "trash":
                remove_ids.append("TRASH")                 # untrash
            try:
                self.client.batch_modify_labels(ids, add_label_ids=add_ids, remove_label_ids=remove_ids)
                self.store.update_proposals_status(ids, "PENDING")
                self.store.add_audit_many(
                    [(mid, label, "undo", "undone", f"reversed {action}") for mid in ids],
                    batch_id=undo_batch,
                )
                undone += len(ids)
            except Exception as error:
                self.store.add_audit_many(
                    [(mid, label, "undo", "error", str(error)) for mid in ids]
                )
            _emit(on_progress, phase="item", done=undone, total=total)

        _emit(on_progress, phase="done", done=total, total=total)
        return {"undone": undone, "batch_id": batch_id}
