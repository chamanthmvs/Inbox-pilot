"""
Full-inbox background crawl: ingest ALL metadata and classify it progressively.

Built for large inboxes (tens of thousands):
  * streams page by page, classifying each batch as it goes, so proposals appear
    while the crawl is still running;
  * resilient — a single failed message is skipped, the crawl continues;
    transient rate-limit / 5xx errors are retried in the client with backoff;
  * resumable — already-ingested messages are skipped on re-run, and any stored
    message still missing a proposal is classified at the end.

It reports progress through the same `on_progress(event)` convention used
elsewhere, with `done` (classified so far) and `total` (mailbox size).
"""

import time

from app.classifier.runner import classify_messages
from app.gmail.parser import parse_metadata


def _emit(on_progress, **event):
    if on_progress is not None:
        try:
            on_progress(event)
        except Exception:
            pass


def full_ingest_and_classify(
    client,
    store,
    classifier,
    on_progress=None,
    batch_size: int = 40,
    skip_existing: bool = True,
    page_pause: float = 2.0,
):
    # page_pause throttles throughput to stay under Gmail's per-user per-minute
    # quota (~15k units/min; each metadata fetch is ~5 units). ~2s per 100-message
    # page keeps us near ~3k messages/min. Only applied when we actually fetched,
    # so resuming over already-stored pages stays fast.
    profile = client.get_profile()
    email = profile.get("emailAddress", "")
    anchor_history_id = str(profile.get("historyId", ""))
    total = int(profile.get("messagesTotal") or 0)

    ingested = 0
    classified = 0
    seen = 0
    errors = 0
    page_token = None

    _emit(on_progress, phase="start", mode="full", done=0, total=total)

    def flush(batch):
        nonlocal classified
        if batch:
            classify_messages(store, classifier, batch)
            classified += len(batch)
            _emit(on_progress, phase="item", mode="full", done=classified,
                  total=total, ingested=ingested, seen=seen, errors=errors)

    while True:
        messages, page_token = client.list_messages(max_results=100, page_token=page_token)
        seen += len(messages)

        # Only fetch ids we don't already have (resumable + cheap re-runs).
        to_fetch = [
            m["id"] for m in messages
            if not (skip_existing and store.has_message(m["id"]))
        ]

        if to_fetch:
            raws = client.get_messages_metadata_batch(to_fetch)  # one HTTP call per ~50
            batch = []
            for mid in to_fetch:
                raw = raws.get(mid)
                if raw is None:
                    errors += 1
                    continue
                try:
                    meta = parse_metadata(raw)
                    store.upsert_message(meta)
                    ingested += 1
                    batch.append(meta)
                except Exception:
                    errors += 1
                if len(batch) >= batch_size:
                    flush(batch)
                    batch = []
            flush(batch)
            if page_pause:
                time.sleep(page_pause)  # stay under the per-minute quota
        else:
            _emit(on_progress, phase="item", mode="full", done=classified,
                  total=total, ingested=ingested, seen=seen, errors=errors)

        if not page_token:
            break

    # Resume safety: classify anything ingested earlier but not yet classified.
    leftovers = list(store.iter_messages_without_proposal())
    for i in range(0, len(leftovers), batch_size):
        flush(leftovers[i : i + batch_size])

    store.set_sync_state(
        email_address=email,
        last_history_id=anchor_history_id,
        mark_full_sync=True,
    )
    _emit(on_progress, phase="done", mode="full", done=classified, total=total,
          ingested=ingested, seen=seen, errors=errors)

    return {"ingested": ingested, "classified": classified, "seen": seen, "errors": errors}
