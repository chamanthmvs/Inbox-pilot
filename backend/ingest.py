"""
Stage 1 entry point: sync Gmail metadata into the local SQLite store.

Run from the `backend/` directory (paths are relative):

    python ingest.py                 # default: up to 50 messages
    python ingest.py --max 200       # fetch up to 200
    python ingest.py --all           # no cap (mind rate limits on huge inboxes)

Re-running is safe and cheap: after the first full sync, later runs fetch only
what changed.
"""

import argparse

from app.gmail.client import GmailClient
from app.ingestion.service import IngestionService
from app.store.db import Store


def main():
    parser = argparse.ArgumentParser(description="Sync Gmail metadata locally.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--max",
        type=int,
        default=50,
        help="maximum messages to fetch this run (default: 50)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="no cap — fetch everything new",
    )
    parser.add_argument(
        "--db",
        default="mailbox.db",
        help="path to the SQLite store (default: mailbox.db)",
    )
    args = parser.parse_args()

    max_messages = None if args.all else args.max

    store = Store(args.db)
    client = GmailClient()
    service = IngestionService(client, store)

    def show(event):
        # Live, overwriting progress line — "ingestion is happening".
        if event["phase"] == "start":
            print(f"Syncing ({event['mode']})... {event['total']} message(s) to process")
        elif event["phase"] == "item":
            total = event.get("total") or "?"
            subject = (event.get("subject") or "")[:50]
            print(f"\r  [{event['done']}/{total}] {subject:<50}", end="", flush=True)
        elif event["phase"] == "done":
            print()  # finish the progress line

    print("Starting sync...")
    result = service.run_sync(max_messages=max_messages, on_progress=show)

    print(result.summary())
    print(f"Store now holds {store.count_messages()} message(s) in {args.db}")

    store.close()


if __name__ == "__main__":
    main()
