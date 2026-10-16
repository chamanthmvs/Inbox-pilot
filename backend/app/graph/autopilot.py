"""
Auto-Pilot — decide the CONFIDENT proposals automatically, per a policy, and
leave only the unsure ones for human review. The executor then applies them.
Everything it does (label / archive) is reversible and logged, with Undo.

Policy:
  confident = method in (rules, learned, human) OR confidence >= threshold
  Balanced / Aggressive:
    confident + noise label (taxonomy keep == False) -> action "archive"
    confident + keep label                            -> action "label"
  Conservative:
    confident                                         -> action "label"
  not confident                                       -> left for review
Trash is never automatic here.
"""


def autopilot_decide(store, taxonomy, threshold: float = 0.85, level: str = "balanced") -> dict:
    pending = store.list_proposals("PENDING")
    to_label, to_archive = [], []
    left = 0

    for p in pending:
        label = p.get("label")
        if not label:
            left += 1
            continue
        method = p.get("method")
        conf = p.get("confidence") or 0.0
        confident = method in ("rules", "learned", "human") or conf >= threshold
        if not confident:
            left += 1
            continue

        ld = taxonomy.get(label)
        is_noise = ld is not None and ld.keep is False
        if level in ("balanced", "aggressive") and is_noise:
            to_archive.append(p["message_id"])
        else:
            to_label.append(p["message_id"])

    if to_label:
        store.set_proposals_action(to_label, "label")
        store.update_proposals_status(to_label, "APPROVED")
    if to_archive:
        store.set_proposals_action(to_archive, "archive")
        store.update_proposals_status(to_archive, "APPROVED")

    return {
        "auto_label": len(to_label),
        "auto_archive": len(to_archive),
        "left_for_review": left,
    }
