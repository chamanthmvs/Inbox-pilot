# The backend API (FastAPI)

**Stage 4 of the pipeline.** This document describes the HTTP API the frontend
(Stage 5) talks to. It's a **thin layer** over the Stage 1–3 services: it starts
the workflow, streams live progress, surfaces proposals, takes the human's
approvals, and applies them. Written for anyone, with the exact routes and
shapes for developers.

Code: [`app/api/main.py`](../app/api/main.py) (routes) and
[`app/api/manager.py`](../app/api/manager.py) (the run manager). Build on
[`workflow.md`](./workflow.md).

---

## 1. The one-sentence version

> A small web server: tell it "organize my inbox", watch the progress live, see
> what it proposes, click approve/reject, and it applies what you approved —
> every call just drives the workflow underneath.

---

## 2. The shape of it

```
Browser (React, Stage 5)
    │  HTTP + SSE
    ▼
FastAPI  (app/api/main.py)
    │
RunManager (app/api/manager.py) ── runs the LangGraph workflow in a background
    │                               thread; streams progress; holds the pause
    ▼
services: ingestion · classifier · approval · executor · store
```

Two things the manager handles that make the API feel simple:

- **Live progress.** The workflow runs in a background thread; its progress
  events are pushed onto a queue that the SSE endpoint streams to the browser —
  this is the "ingestion is happening…" feed.
- **The pause.** When the graph hits `interrupt()`, the manager records the
  payload and marks the run `paused`. The browser submits decisions, and the
  manager resumes the same graph thread to apply.

> **v1 scope:** single-user, one active run at a time. OAuth happens lazily in
> the server process on the first run (a browser window opens once for the
> `gmail.modify` consent). Read-only endpoints (proposals, audit, taxonomy) work
> before any run, straight from the local store.

---

## 3. Run it

```bash
cd backend
source .venv/bin/activate
uvicorn app.api.main:app --reload          # http://127.0.0.1:8000
# interactive docs: http://127.0.0.1:8000/docs
```

Useful env vars (see [`settings`](../app/settings.py)): `DRY_RUN=1` to preview
without changing Gmail, `USE_LLM=0` for rules-only, `CLASSIFIER_MODEL=claude-haiku-5-5`.

---

## 4. Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness: `{"status":"ok"}`. |
| GET | `/api/status` | Auth flag, dry-run flag, proposal counts, audit count, current run snapshot. |
| POST | `/api/connect` | Authenticate with Gmail now (OAuth) without starting a run — powers the UI's "Connect" step. |
| GET | `/api/taxonomy` | The fixed label menu (names + descriptions) for UI dropdowns. |
| POST | `/api/runs` | Start a run. Body `{"max_messages": 50}`. Returns a run snapshot (`run_id`, `status`). |
| GET | `/api/runs/{id}/stream` | **SSE** live progress; ends with a `paused`/`done`/`error` event. |
| POST | `/api/runs/{id}/decisions` | Body `{"decisions": {"<msg_id>": "APPROVED"\|"REJECTED"}}`. Resumes → applies. |
| GET | `/api/proposals?status=` | Proposals joined with message subject/sender/snippet. |
| GET | `/api/groups?status=` | Proposals grouped by **(sender domain × label)** with counts + samples — the bulk-review unit for large inboxes. |
| POST | `/api/groups/decide` | Bulk approve/reject a group. Body `{"domain","label","verdict","new_label"?,"action"?}`; `new_label` relabels; `action` ∈ `label`/`archive`/`trash` sets what Apply does. Approving remembers a learned rule. |
| GET | `/api/learned` | The learned sender→label rules. |
| GET | `/api/proposals/counts` | `{status: count}`. |
| POST | `/api/proposals/{id}/label` | Human-set a label (e.g. for `NEEDS_REVIEW`). Body `{"label": "..."}`. |
| GET | `/api/messages/{id}` | One stored message's metadata. |
| GET | `/api/audit?limit=` | Recent audit-log rows (newest first). |

### The run lifecycle (status values)

```
POST /api/runs → running ──(SSE progress)──▶ paused ──POST decisions──▶ applying ──▶ done
                                 └─ error (surfaced on the stream + /status)
```

### SSE event shapes (`/api/runs/{id}/stream`)

Each line is `data: <json>`:

```jsonc
{"type":"progress","phase":"start","mode":"full","done":0,"total":50}
{"type":"progress","phase":"item","done":12,"total":50,"subject":"..."}   // ingest
{"type":"progress","phase":"item","done":40,"total":50,"label":"Finance/Statements"} // classify
{"type":"paused","run_id":"...","status":"paused","counts":{"pending":18,"needs_review":7}}
```

The frontend renders `progress` events as a live bar/line and, on `paused`,
loads `/api/proposals` for the approval screen.

---

## 5. A typical frontend conversation

```
POST /api/runs {max_messages:50}          → {run_id, status:"running"}
GET  /api/runs/{id}/stream  (SSE)          → progress… then {type:"paused", counts}
GET  /api/proposals?status=PENDING         → cards to approve/reject
GET  /api/proposals?status=NEEDS_REVIEW    → cards needing a label
POST /api/proposals/{id}/label {label}     → (optional) label an unsure one
POST /api/runs/{id}/decisions {decisions}  → {apply:{applied,dry_run,errors}}
GET  /api/audit                            → what changed
```

---

## 6. Design notes & safety

- **Thin by design.** Routes contain almost no logic — they call the same
  services the CLIs and tests use, so behaviour is identical across entry points.
- **Nothing applied without approval.** Only `POST /decisions` can move a
  proposal toward `APPLIED`, and only `APPROVED` ones are applied.
- **Dry-run respected.** With `DRY_RUN=1`, `/decisions` returns `applied:0,
  dry_run:N` and Gmail is untouched.
- **CORS is wide open for dev** (`allow_origins=["*"]`). Lock this down to the
  real frontend origin before any non-local deployment.
- **Auth state is in-process.** Because the checkpointer is `MemorySaver` and
  OAuth is per-process, restarting the server drops a paused run — re-run
  (ingest/classify are idempotent; decided proposals are preserved). Persisting
  across restarts = `SqliteSaver` + a persisted token (already on disk).

---

## 7. Failure modes

| Response / symptom | Cause | Handling |
| --- | --- | --- |
| `409` on `POST /api/runs` | A run is already running/applying | Wait for it, or read `/api/status`. |
| `409` on `/decisions` | Run isn't `paused` yet (or already done) | Poll `/api/status` until `paused`. |
| `{type:"error"}` on the stream | OAuth/network error building the graph | Message is included; fix creds and start a new run. |
| `400` on set-label | Label not in the taxonomy | Use a name from `/api/taxonomy`. |

---

## 8. Glossary

- **SSE (Server-Sent Events)** — a one-way stream of events over HTTP; how the
  browser watches progress.
- **Run** — one pass of the workflow (ingest→classify→pause→apply).
- **Snapshot** — the small JSON describing a run's status/counts/result.
- **RunManager** — the object that owns the graph, the background thread, and the
  progress queue.

---

## 9. Code map

| File | Role |
| --- | --- |
| [`app/api/main.py`](../app/api/main.py) | FastAPI routes + `create_app(manager)`. |
| [`app/api/manager.py`](../app/api/manager.py) | `RunManager` — background run, progress queue, pause/resume. |
| [`app/graph/`](../app/graph/) | The workflow + services the API drives. |
| [`app/settings.py`](../app/settings.py) | Config the API reads (`DRY_RUN`, model, db path). |
| [`tests/test_api_offline.py`](../tests/test_api_offline.py) | Full HTTP-flow proof via `TestClient` + fake Gmail. |
