"""
IngestionService — pull mailbox METADATA from Gmail into the local store.

Two modes, chosen automatically:

  * Full sync      — first run (or when history is too old): list message ids,
                     fetch each as metadata, upsert. Anchors a history id.
  * Incremental    — later runs: ask Gmail's history API only for what changed
                     since the stored history id, and apply just those changes.

Both are idempotent: safe to re-run, safe to interrupt and resume.

Progress: every public sync accepts an optional `on_progress` callback. We call
it with small event dicts ({"phase", "mode", "done", "total", "subject"}) so a
CLI can print a line and a UI can show a live bar — "ingestion is happening".
"""

from dataclasses import dataclass, field

from googleapiclient.errors import HttpError

from app.gmail.parser import parse_metadata


def _emit(on_progress, **event):
    """Call the progress callback if one was supplied. Never let UI break sync."""
    if on_progress is not None:
        try:
            on_progress(event)
        except Exception:
            pass


@dataclass
class SyncResult:
    mode: str                       # "full" or "incremental"
    fetched: int = 0                # messages fetched + upserted
    deleted: int = 0                # messages removed from the store
    email_address: str = ""
    history_id: str = ""
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"[{self.mode}] fetched={self.fetched} deleted={self.deleted} "
            f"history_id={self.history_id} account={self.email_address}"
        )
        if self.notes:
            line += "\n  - " + "\n  - ".join(self.notes)
        return line


class IngestionService:
    def __init__(self, client, store):
        self.client = client
        self.store = store

    # ------------------------------------------------------------------ public

    def run_sync(self, max_messages=None, on_progress=None) -> SyncResult:
        """
        Sync the mailbox. Picks incremental if we have a stored history id,
        otherwise a full sync. Falls back to full sync if the stored history id
        is too old for Gmail to serve.
        """

        state = self.store.get_sync_state()

        if state and state.get("last_history_id"):
            try:
                return self._incremental_sync(
                    state["last_history_id"],
                    max_messages=max_messages,
                    on_progress=on_progress,
                )
            except HttpError as error:
                if error.resp.status == 404:
                    # history id expired — the only safe recovery is a full sync.
                    result = self._full_sync(
                        max_messages=max_messages,
                        on_progress=on_progress,
                    )
                    result.notes.append(
                        "stored history id was too old; did a full resync"
                    )
                    return result
                raise

        return self._full_sync(max_messages=max_messages, on_progress=on_progress)

    # ------------------------------------------------------------------- modes

    def _full_sync(self, max_messages=None, on_progress=None) -> SyncResult:
        profile = self.client.get_profile()
        email_address = profile.get("emailAddress", "")

        # Capture the history id BEFORE fetching. Anything that happens during
        # the run is then caught by the next incremental sync — nothing slips.
        anchor_history_id = str(profile.get("historyId", ""))

        result = SyncResult(
            mode="full",
            email_address=email_address,
            history_id=anchor_history_id,
        )

        # Collect ids first so we can report a total to the progress callback.
        ids = list(self._iter_message_ids(max_messages))
        total = len(ids)
        _emit(on_progress, phase="start", mode="full", done=0, total=total)

        for i, message_id in enumerate(ids, start=1):
            raw = self.client.get_message_metadata(message_id)
            meta = parse_metadata(raw)
            self.store.upsert_message(meta)
            result.fetched += 1
            _emit(
                on_progress,
                phase="item",
                mode="full",
                done=i,
                total=total,
                subject=meta.subject,
            )

        self.store.set_sync_state(
            email_address=email_address,
            last_history_id=anchor_history_id,
            mark_full_sync=True,
        )
        _emit(on_progress, phase="done", mode="full", done=result.fetched, total=total)
        return result

    def _incremental_sync(
        self,
        start_history_id,
        max_messages=None,
        on_progress=None,
    ) -> SyncResult:
        result = SyncResult(mode="incremental", history_id=start_history_id)

        added_ids: set[str] = set()
        deleted_ids: set[str] = set()
        latest_history_id = start_history_id
        page_token = None

        while True:
            response = self.client.list_history(
                start_history_id,
                page_token=page_token,
            )

            latest_history_id = str(
                response.get("historyId", latest_history_id)
            )

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    added_ids.add(added["message"]["id"])
                for deleted in record.get("messagesDeleted", []):
                    deleted_ids.add(deleted["message"]["id"])
                # A label change also means the stored row is stale → refetch.
                for changed in record.get("labelsAdded", []):
                    added_ids.add(changed["message"]["id"])
                for changed in record.get("labelsRemoved", []):
                    added_ids.add(changed["message"]["id"])

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        # A message both added and deleted in the window → net deleted.
        added_ids -= deleted_ids

        if max_messages is not None:
            added_ids = set(list(added_ids)[:max_messages])

        total = len(added_ids) + len(deleted_ids)
        _emit(on_progress, phase="start", mode="incremental", done=0, total=total)
        done = 0

        for message_id in added_ids:
            raw = self.client.get_message_metadata(message_id)
            meta = parse_metadata(raw)
            self.store.upsert_message(meta)
            result.fetched += 1
            done += 1
            _emit(
                on_progress,
                phase="item",
                mode="incremental",
                done=done,
                total=total,
                subject=meta.subject,
            )

        for message_id in deleted_ids:
            self.store.delete_message(message_id)
            result.deleted += 1
            done += 1
            _emit(on_progress, phase="item", mode="incremental", done=done, total=total)

        self.store.set_sync_state(last_history_id=latest_history_id)
        result.history_id = latest_history_id

        if result.fetched == 0 and result.deleted == 0:
            result.notes.append("no changes since last sync")

        _emit(on_progress, phase="done", mode="incremental", done=done, total=total)
        return result

    # ----------------------------------------------------------------- helpers

    def _iter_message_ids(self, max_messages=None):
        """Yield message ids across pages, stopping at max_messages if set."""

        page_token = None
        yielded = 0

        while True:
            page_size = 100
            if max_messages is not None:
                remaining = max_messages - yielded
                if remaining <= 0:
                    return
                page_size = min(100, remaining)

            messages, page_token = self.client.list_messages(
                max_results=page_size,
                page_token=page_token,
            )

            for message in messages:
                yield message["id"]
                yielded += 1
                if max_messages is not None and yielded >= max_messages:
                    return

            if not page_token:
                return
