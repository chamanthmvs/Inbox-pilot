"""
AI re-segregate: split ONE coarse label into finer nested sub-labels.

  1. Propose 3–6 sub-labels (nested under the parent) from a sample — AI, no assumptions.
  2. Assign each email in the slice to a sub-label (batched) — or leave it on the parent.
  3. Relabel the proposals to their sub-label (still PENDING — nothing hits Gmail).
  4. Persist the new sub-labels to taxonomy.local.yaml so future mail auto-splits.

Reversible and review-first: the finer labels just become new (sender × label)
groups in the dashboard; the human still approves/applies them.
"""

from app.classifier.taxonomy import Taxonomy


def _emit(on_progress, **event):
    if on_progress is not None:
        try:
            on_progress(event)
        except Exception:
            pass


def segregate_label(store, classifier, label, taxonomy_path="config/taxonomy.yaml", on_progress=None) -> dict:
    llm = classifier.llm
    if llm is None or not llm.enabled:
        return {"error": "AI is off — add ANTHROPIC_API_KEY to use re-segregate.", "sublabels": []}

    metas = [
        m for m in store.iter_messages()
        if (store.get_proposal(m.id) or {}).get("label") == label
        and (store.get_proposal(m.id) or {}).get("status") == "PENDING"
    ]
    total = len(metas)
    _emit(on_progress, phase="start", done=0, total=total, stage="proposing")
    if total < 4:
        return {"error": "Too few emails in this label to split.", "sublabels": []}

    proposed = llm.propose_sublabels(label, metas)
    if not proposed:
        return {"error": "The AI did not find clear sub-categories here.", "sublabels": []}

    options = [p["name"] for p in proposed]
    _emit(on_progress, phase="item", done=0, total=total, stage="assigning")

    assignments = llm.assign_batch(metas, options)  # {id: LLMMatch|None}
    by_sub: dict = {}
    for m in metas:
        hit = assignments.get(m.id)
        if hit is not None:
            by_sub.setdefault(hit.label, []).append(m.id)

    # Relabel each assigned group; unassigned stay on the parent label.
    moved = 0
    counts = []
    for p in proposed:
        ids = by_sub.get(p["name"], [])
        if ids:
            store.relabel_proposals(ids, p["name"])
            moved += len(ids)
            counts.append({"name": p["name"], "count": len(ids)})
    _emit(on_progress, phase="item", done=moved, total=total, stage="assigning")

    # Persist the sub-labels that actually got mail (merged at load; taxonomy.yaml untouched).
    descriptions = {p["name"]: p.get("description", "") for p in proposed}
    Taxonomy.add_local_labels(
        [{"name": c["name"], "description": descriptions.get(c["name"], ""), "keep": True}
         for c in counts],
        path=taxonomy_path,
    )

    _emit(on_progress, phase="done", done=moved, total=total)
    return {
        "label": label,
        "moved": moved,
        "unchanged": total - moved,
        "sublabels": sorted(counts, key=lambda c: c["count"], reverse=True),
    }
