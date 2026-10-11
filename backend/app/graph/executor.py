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
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "errors": self.errors,
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
        """Apply every APPROVED proposal that carries a label."""
        result = ApplyResult()
        approved = [
            p for p in self.store.list_proposals("APPROVED") if p.get("label")
        ]
        total = len(approved)
        _emit(on_progress, phase="start", done=0, total=total)

        for i, proposal in enumerate(approved, start=1):
            message_id = proposal["message_id"]
            label = proposal["label"]
            try:
                if self.dry_run:
                    self.store.add_audit(
                        message_id, label, "apply_label", "dry_run",
                        f"would add label '{label}'",
                    )
                    result.dry_run += 1
                else:
                    label_id = self._label_id(label)
                    self.client.modify_message_labels(
                        message_id, add_label_ids=[label_id]
                    )
                    self.store.mark_applied(message_id)
                    self.store.add_audit(
                        message_id, label, "apply_label", "applied",
                        f"added '{label}' ({label_id})",
                    )
                    result.applied += 1
            except Exception as error:  # never let one bad message stop the batch
                self.store.add_audit(
                    message_id, label, "apply_label", "error", str(error)
                )
                result.errors += 1

            _emit(
                on_progress,
                phase="item",
                done=i,
                total=total,
                label=label,
                dry_run=self.dry_run,
            )

        if self.dry_run and total:
            result.notes.append("dry-run: nothing was changed in Gmail")
        _emit(on_progress, phase="done", done=total, total=total)
        return result
