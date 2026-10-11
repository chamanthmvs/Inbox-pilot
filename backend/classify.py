"""
Stage 2 entry point: classify the messages already in the local store and write
one proposal per message. Run AFTER `ingest.py`, from the `backend/` directory.

    python classify.py                 # rules + LLM (if ANTHROPIC_API_KEY is set)
    python classify.py --no-llm        # rules only; everything else → NEEDS_REVIEW
    python classify.py --model claude-haiku-5-5   # cheaper LLM for big inboxes
    python classify.py --reclassify    # re-decide even human-decided messages

Nothing is applied to Gmail here — this only produces PROPOSALS for a human to
approve in a later stage.
"""

import argparse

from app.classifier.runner import build_classifier, classify_store
from app.store.db import Store


def main():
    parser = argparse.ArgumentParser(description="Classify stored messages into proposals.")
    parser.add_argument("--db", default="mailbox.db", help="SQLite store path")
    parser.add_argument("--no-llm", action="store_true", help="rules only, no LLM")
    parser.add_argument("--model", default=None, help="override classifier LLM model id")
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="re-decide messages even if a human already decided them",
    )
    args = parser.parse_args()

    store = Store(args.db)
    classifier, taxonomy = build_classifier(use_llm=not args.no_llm, model=args.model)

    llm = classifier.llm
    if llm is None:
        print("LLM: off (--no-llm)")
    elif llm.enabled:
        print(f"LLM: on (model={llm.model})")
    else:
        print("LLM: unavailable (no ANTHROPIC_API_KEY) → uncertain mail goes to NEEDS_REVIEW")

    def show(event):
        if event["phase"] == "start":
            print(f"Classifying {event['total']} message(s)...")
        elif event["phase"] == "item":
            total = event.get("total") or "?"
            tag = "skip" if event.get("skipped") else (event.get("label") or "NEEDS_REVIEW")
            print(f"\r  [{event['done']}/{total}] -> {str(tag):<28}", end="", flush=True)
        elif event["phase"] == "done":
            print()

    result = classify_store(store, classifier, reclassify=args.reclassify, on_progress=show)

    print(result.summary())
    print("proposal totals in store:", store.proposal_counts())
    store.close()


if __name__ == "__main__":
    main()
