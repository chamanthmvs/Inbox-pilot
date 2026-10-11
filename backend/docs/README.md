# Documentation

Welcome to the documentation for the **Gmail Organizer** — a human-in-the-loop
assistant that proposes how to organize your inbox (v1: apply category labels)
and only acts after you approve.

## Start here

1. **[How the Gmail connection works](./authentication.md)** — How the app signs
   into Gmail *without* ever seeing your password: OAuth, the "keycard,"
   `credentials.json` vs `token.json`, and the `gmail.modify` scope.
2. **[How ingestion works](./ingestion.md)** — How the app copies your mailbox
   **metadata** (never the body) into a local database, and syncs it efficiently
   without tripping Gmail's rate limits.
3. **[How classification works](./classification.md)** — How each email gets a
   proposed label via rules → LLM → `NEEDS_REVIEW`, and why it refuses to guess.
4. **[How the workflow works](./workflow.md)** — The human-in-the-loop spine:
   ingest → classify → **pause for approval** → apply labels, with an audit log.
5. **[The backend API](./api.md)** — The FastAPI endpoints the UI calls, incl.
   the SSE live-progress stream and the approve → apply flow.
6. **[The frontend](./frontend.md)** — The thin React+Vite approval UI and how it
   talks to the API (live progress, approve/reject, audit).

## How the whole thing fits together

```
Gmail ──▶ Ingestion ──▶ SQLite store ──▶ Classifier ──▶ Proposals
                                              │              │
                                              ▼              ▼
                                        (rules→LLM→      Human approves
                                         NEEDS_REVIEW)   → labels applied
```

Each stage gets its own doc as it's built. See the build stages and design
decisions in [`CLAUDE.md`](../../CLAUDE.md).

## What the application does today

- **Ingestion (Stage 1) — done.** [`ingest.py`](../ingest.py) syncs mailbox
  metadata into `mailbox.db`: a full sync on first run, incremental after.
  Read-only toward Gmail.
- **Classifier (Stage 2) — done.** [`classify.py`](../classify.py) turns stored
  messages into **proposals** (rules → LLM → `NEEDS_REVIEW`) using the fixed
  taxonomy in [`config/taxonomy.yaml`](../config/taxonomy.yaml). Still read-only
  toward Gmail — proposals await human approval.
- **Workflow + approval (Stage 3) — done.** LangGraph orchestration
  (`app/graph/`) with a human-in-the-loop `interrupt`, a `LabelExecutor` that
  applies approved labels (with dry-run), and an append-only `audit_log`.
- **API (Stage 4) — done.** FastAPI (`app/api/`) over these services:
  start a run, **SSE live progress**, list proposals, submit approvals → apply,
  audit. Run `uvicorn app.api.main:app --reload`.
- **UI (Stage 5) — done.** A thin React (Vite) app (`frontend/`) with the live
  "ingestion is happening…" progress, approve/reject, labelling of NEEDS_REVIEW,
  and an audit view. One-click run via `.vscode/launch.json`.
- **Stage 6 — next.** Hardening + a redacted test fixture, then `git init` and
  push to GitHub.
- Nothing is ever applied to Gmail without explicit human approval, and v1 never
  deletes.

[`main.py`](../main.py) is a legacy demo that lists and prints a few messages.

## Code map (current)

| File / dir | Role |
| --- | --- |
| [`ingest.py`](../ingest.py) | Stage 1 entry point — run a metadata sync. |
| [`app/gmail/`](../app/gmail/) | `GmailClient` (OAuth, list/get/metadata/history, labels) + parsers. |
| [`app/store/`](../app/store/) | SQLite store (`messages`, `sync_state`). |
| [`app/ingestion/`](../app/ingestion/) | `IngestionService` (full + incremental sync). |
| [`app/models/`](../app/models/) | Pydantic models (`MessageMeta`, `Email`, `Attachment`). |
| [`config/taxonomy.yaml`](../config/taxonomy.yaml) | The label taxonomy (data, not code). |
| [`tests/`](../tests/) | Offline tests using `raw_email.json` as a fixture. |
| `credentials.json` / `token.json` | Secrets — never commit. |
| `mailbox.db` | Local metadata store — gitignored (sensitive). |

> ⚠️ `credentials.json`, `token.json`, and `mailbox.db` contain sensitive data.
> They are listed in [`.gitignore`](../.gitignore) and must never be shared or
> committed.
