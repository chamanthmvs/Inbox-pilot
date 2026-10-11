"""
Wire the pieces together and classify everything in the store.

`classify_store` walks stored messages, classifies each, and upserts a proposal.
It skips messages a human has already decided on (approved/rejected/applied) so
re-running never clobbers human choices — unless `reclassify=True`.

Progress is reported through the same `on_progress(event)` convention the
ingestion service uses, so the CLI and (later) the UI can show live status.
"""

from dataclasses import dataclass, field

from app.classifier.classifier import Classifier
from app.classifier.llm import LLMClassifier
from app.classifier.rules import RulesClassifier, _domain_of
from app.classifier.taxonomy import Taxonomy


def _emit(on_progress, **event):
    if on_progress is not None:
        try:
            on_progress(event)
        except Exception:
            pass


@dataclass
class ClassifyResult:
    processed: int = 0
    skipped_decided: int = 0
    by_method: dict = field(default_factory=dict)
    by_status: dict = field(default_factory=dict)

    def _bump(self, d, key):
        d[key] = d.get(key, 0) + 1

    def summary(self) -> str:
        methods = ", ".join(f"{k}={v}" for k, v in sorted(self.by_method.items()))
        statuses = ", ".join(f"{k}={v}" for k, v in sorted(self.by_status.items()))
        return (
            f"classified={self.processed} skipped_decided={self.skipped_decided}\n"
            f"  by method:  {methods or '(none)'}\n"
            f"  by status:  {statuses or '(none)'}"
        )


def build_classifier(
    taxonomy_path: str = "config/taxonomy.yaml",
    use_llm: bool = True,
    model: str = None,
):
    """Return (Classifier, Taxonomy). LLM is attached but self-disables without a key."""
    taxonomy = Taxonomy.load(taxonomy_path)
    rules = RulesClassifier(taxonomy)
    llm = LLMClassifier(taxonomy, model=model) if use_llm else None
    return Classifier(taxonomy, rules, llm), taxonomy


def classify_messages(store, classifier, metas, result=None) -> ClassifyResult:
    """
    Classify an explicit list of messages: learned rules → rules → batched LLM →
    NEEDS_REVIEW. Writes one proposal per message. Reusable by both the normal
    store pass and the full-inbox background job (per batch).
    """
    result = result or ClassifyResult()
    rules = classifier.rules
    llm = classifier.llm
    taxonomy = classifier.taxonomy

    def _save(meta, label, method, confidence, reason, status):
        store.upsert_proposal(meta.id, label, method, confidence, reason, status)
        result.processed += 1
        result._bump(result.by_method, method)
        result._bump(result.by_status, status)

    # Phase 0 — learned rules (one label per domain; multi-purpose falls through).
    learned_map = store.get_learned_map()
    after_learned = []
    for meta in metas:
        domain = _domain_of(meta.from_addr or "")
        labels = learned_map.get(domain, [])
        if len(labels) == 1 and taxonomy.is_valid(labels[0]):
            _save(meta, labels[0], "learned", 1.0,
                  f"learned: you approved '{domain}' as this before", "PENDING")
        else:
            after_learned.append(meta)

    # Phase 1 — rules.
    unruled = []
    for meta in after_learned:
        match = rules.classify(meta)
        if match is not None:
            _save(meta, match.label, "rules", 1.0, match.reason, "PENDING")
        else:
            unruled.append(meta)

    # Phase 2 — one batched LLM pass over the rest.
    llm_on = llm is not None and llm.enabled
    llm_results = llm.classify_batch(unruled) if (llm_on and unruled) else {}
    for meta in unruled:
        hit = llm_results.get(meta.id)
        if hit is not None:
            _save(meta, hit.label, "llm", hit.confidence, hit.reason, "PENDING")
        else:
            reason = (
                "no rule matched and the LLM was not confident"
                if llm_on
                else "no rule matched and the LLM is disabled (no ANTHROPIC_API_KEY)"
            )
            _save(meta, None, "needs_review", 0.0, reason, "NEEDS_REVIEW")

    return result


def classify_store(store, classifier, reclassify=False, on_progress=None) -> ClassifyResult:
    """Classify every undecided message in the store (one batched LLM pass)."""
    todo = []
    skipped = 0
    for meta in store.iter_messages():
        if not reclassify and store.has_decided_proposal(meta.id):
            skipped += 1
        else:
            todo.append(meta)

    _emit(on_progress, phase="start", done=0, total=len(todo))
    result = classify_messages(store, classifier, todo)
    result.skipped_decided = skipped
    _emit(on_progress, phase="done", done=len(todo), total=len(todo))
    return result
