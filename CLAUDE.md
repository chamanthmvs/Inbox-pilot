# CLAUDE.md

Guidance for AI agents (and humans) working in this repo. Keep this file current
— update it whenever structure, commands, or direction change.

## What this project is

A Gmail organizing assistant with a **human-in-the-loop** approval step, built as
a **sensitive, production-minded** application. A pipeline ingests mailbox
metadata into a local store, proposes how to organize it (v1: apply category
labels from a fixed taxonomy), a human approves/rejects each proposal, and only
approved actions are applied to Gmail. Nothing is ever deleted in v1, and nothing
touches Gmail without explicit human approval.

## Architecture: ingest once, operate on our copy

We never hit Gmail live for every operation. We ingest metadata into SQLite once,
then classify / display / approve against our own copy. This is the core decision.

```
                    ┌──────────────── SQLite store (backend/mailbox.db) ────────────┐
Gmail API ──(1)───▶ │ messages(metadata)   sync_state   proposals   audit_log        │
  ▲                 └───────────────────────────────────────────────────────────────┘
  │                        │ (2) classify         │ (3) propose          ▲ (5) apply
  │                        ▼                        ▼                      │ approved only
  │                  rules → LLM → NEEDS_REVIEW   proposals = PENDING      │
  └──────────────── (5) executor ◀──(4) approve/reject ◀── API ◀── React UI
       modify labels (only after approval; never delete)
```

Why: **(1)** we fetch each message once → re-classifying/restarting never re-hits
Gmail; **(2)** incremental sync (Gmail history API) later fetches only changes;
**(3)** metadata-only = we never store message bodies (data minimization);
**(4)** every change is approved by a human and written to an audit log; **(5)**
labels are reversible and we never delete → tiny blast radius.

Accuracy note for docs: `format=metadata` is about **data minimization and
smaller/faster responses**, NOT fewer quota units (Gmail bills per *method*,
~5 units for `messages.get` regardless of format). The real rate-limit wins are:
fetch-once store, incremental history sync, batching, and backoff.

## Tech decisions (agreed)

- **Backend:** Python; **LangGraph** workflow (Stage 3); **FastAPI** API (Stage 4).
- **Store:** **SQLite** file (`backend/mailbox.db`), single-user v1; migrate to
  Postgres later if multi-user.
- **Ingestion:** **metadata only** (From/To/Subject/Date headers + snippet +
  labels), no bodies. Full sync first run, incremental via history API after.
- **Classifier (Stage 2):** 3-state — **rules first → LLM (Claude) on uncertain →
  if still uncertain, flag `NEEDS_REVIEW`** (never guess). Rules match **whole
  words** (not substrings — "irs" must not match "idfcfirst") and rank by
  `(depth, strength)` where domain(3) > sender-substring(2) > subject-word(1).
  Keep taxonomy tokens specific. Taxonomy is **v2**: added `Bank` (Alerts/Support
  Tickets/Statements), `Donations`, `Finance/Securities & Demat`, stronger
  investment rules (Zerodha/Motilal/MF). LLM runs **batched** (~20 emails/call,
  `classify_batch`), default model **claude-haiku-5-5** (cheap), menu prompt-
  cached — "not heavy on LLM". Enable via `backend/.env` (`ANTHROPIC_API_KEY`);
  `.env` loaded in `app/__init__.py`. Without a key → rules-only, big NEEDS_REVIEW.
- **Human-in-the-loop:** LangGraph `interrupt()` pauses at the approval node;
  resumes only after the human responds via API/UI.
- **Frontend:** thin **React + Vite** approval UI (Stage 5).
- **v1 action:** apply category **labels** only (non-destructive). No archive /
  delete / send. "Noise" categories are labeled so the user can clean up manually;
  propose-delete is a possible later stage, always human-approved.

## Taxonomy = data, not code

The label taxonomy lives in [`backend/config/taxonomy.yaml`](backend/config/taxonomy.yaml),
not in code. Adding a category or sub-label is a one-line edit, no code change.
Labels are hierarchical via Gmail's `/` convention (e.g. `Job/Offers & Documents`
nests under `Job`). Each entry carries a description (also used as the LLM's menu)
and rule patterns. Keep it lean: a sub-label earns its place only when its mail is
*actionably different* (keep-forever vs. clean-up). See the file for the agreed
v1 set (Finance, Job, Shopping, Travel, Health, Education, Accounts, Personal,
Social, Newsletters, Promotions).

## Repo layout

```
Gmail_organizing_automation/
├─ backend/
│   ├─ app/gmail/        GmailClient (OAuth, list/get/metadata/history, labels) + parser
│   ├─ app/models/       Pydantic models (Email, Attachment, MessageMeta)
│   ├─ app/store/        SQLite store (messages, sync_state, proposals)
│   ├─ app/ingestion/    IngestionService (full + incremental metadata sync)
│   ├─ app/classifier/   Taxonomy + rules + LLM + 3-state orchestrator + runner
│   ├─ app/graph/        LangGraph workflow + approval/executor/factory (Stage 3)
│   ├─ app/api/          FastAPI app + RunManager (Stage 4)
│   ├─ app/settings.py   central env-driven config
│   ├─ config/           taxonomy.yaml
│   ├─ docs/             non-technical + technical docs, per module
│   ├─ tests/            offline tests using raw_email.json as a fixture
│   ├─ ingest.py         Stage 1 entry point (run a metadata sync)
│   ├─ classify.py       Stage 2 entry point (classify store → proposals)
│   ├─ main.py           legacy demo (lists/prints messages)
│   ├─ requirements.txt
│   ├─ credentials.json / token.json   SECRETS — never commit
│   ├─ mailbox.db        SQLite store — gitignored (mailbox metadata = sensitive)
│   └─ .venv/            local virtualenv (recreate; never move a venv folder)
├─ frontend/             thin React (Vite) UI — src/App.jsx, src/api.js, vite.config.js
├─ .vscode/              launch.json (one-click compound run) + tasks.json (setup/tests)
├─ .tools/node/          bundled Node (macOS arm64, gitignored; delete to uninstall)
├─ README.md             root run/setup guide
└─ CLAUDE.md

## Running the whole app

One click: VSCode **Run and Debug → "▶ Run App (backend + frontend)"** (compound
in `.vscode/launch.json`) → API on :8000, UI on :5173. First-time deps via
**Run Task → "Setup (install everything)"**. Needs Node 18+ for the frontend.
Manual: `uvicorn app.api.main:app --reload` in backend + `npm run dev` in frontend.
```

## Run

```bash
cd backend                 # MUST run from here — token/credentials/db are relative paths
source .venv/bin/activate
python ingest.py           # Stage 1: sync mailbox metadata into mailbox.db
```

**Working-directory matters:** paths (`token.json`, `credentials.json`,
`mailbox.db`) are relative to the current directory. Always run from `backend/`.

## Build stages (each ships with its upgraded doc)

1. ✅ Ingestion: metadata-only full + incremental sync → SQLite. Entry:
   `python ingest.py`. Store: `app/store/db.py`. Service: `app/ingestion/service.py`.
   Doc: `docs/ingestion.md`. Offline test: `tests/test_ingestion_offline.py` (21 checks).
2. ✅ Classifier: rules → LLM → NEEDS_REVIEW, closed taxonomy. Entry:
   `python classify.py`. Package: `app/classifier/` (taxonomy/rules/llm/
   classifier/runner). Writes `proposals` table (PENDING/NEEDS_REVIEW). Doc:
   `docs/classification.md`. Offline test: `tests/test_classifier_offline.py`.
3. ✅ Workflow: LangGraph `ingest → classify → review(interrupt) → apply`.
   Package: `app/graph/` (workflow/approval/executor/factory). Adds `audit_log`
   table + proposal status transitions + `DRY_RUN`. Central config `app/settings.py`.
   Doc: `docs/workflow.md`. Offline test: `tests/test_workflow_offline.py`.
   NOTE: checkpointer is `MemorySaver` (in-process); upgrade to `SqliteSaver` to
   persist a parked workflow across restarts. Apply step is the first real Gmail
   write → needs the `gmail.modify` re-consent at run time.
4. ✅ API: FastAPI (`app/api/`). `RunManager` drives the graph in a background
   thread, streams progress via **SSE** (`/api/runs/{id}/stream`), holds the
   pause, resumes on `/api/runs/{id}/decisions`. Read endpoints work without
   OAuth. Run: `uvicorn app.api.main:app --reload`. Doc: `docs/api.md`. Offline
   test: `tests/test_api_offline.py` (full HTTP flow via TestClient + fake Gmail).
5. ✅ UI: thin React+Vite app in `frontend/` (one `App.jsx`, `api.js`, SSE live
   progress, approve/reject + label NEEDS_REVIEW, audit view). Talks to the API
   via Vite `/api` proxy. Doc: `docs/frontend.md`. One-click run via
   `.vscode/launch.json` compound "▶ Run App (backend + frontend)". VERIFIED:
   `npm run build` succeeds and `vite` dev server boots; backend boots and serves
   `/api/health` + `/api/status` with no OAuth. Node is **bundled** in
   `.tools/node/` (macOS arm64, self-contained, gitignored) — no global install;
   run config prepends it to PATH. `frontend/node_modules` installed.
6. ✅ Scale & Automation (for 20k+ inboxes). Approve by **(sender domain × label)
   group**, not per email. Backend: `store.grouped_proposals`, `update/relabel_
   proposals`, `learned_rules` (purpose-aware phase-0 in classify_store),
   `ApprovalService.decide_group(domain,label,verdict,new_label)`; endpoints
   `/api/groups`, `/api/groups/decide`, `/api/learned`. Frontend: clean dashboard
   (stat tiles, single-hue category bars, expandable group cards with Approve
   all / Skip / relabel, NEEDS_REVIEW section, "Apply N approved"). Approval model:
   approve-each-group-once; learning makes reruns cheaper. All offline tests pass;
   frontend builds.
7. ⬜ Hardening + docs polish (root README done) + redacted fixture → `git init` → GitHub.

### UI requirement (user-stated) — live progress visibility
The user wants the UI to show what's happening (e.g. "ingestion is happening…",
text or a simple animation; fancy animation can wait). Backend support exists
now: `IngestionService.run_sync` and `classifier.runner.classify_store` both
take `on_progress(event)` and emit `{"phase","done","total","subject"/"label"}`
events. `ingest.py`/`classify.py` print these as a live CLI line. Stage 4 should
expose them (SSE/WebSocket or a job-status row); Stage 5 renders them.

Done earlier: restructure into `backend/`+`frontend/`; auth scope
`gmail.readonly`→`gmail.modify` with `list_labels`/`get_or_create_label`/
`modify_message_labels`. **ACTION NEEDED before Stage 3:** delete
`backend/token.json` and re-consent (the modify scope). Stage 1 ingestion only
reads, so an existing readonly token still works for it.

## Documentation standard (important — the bar is high)

Every module gets a doc in [`backend/docs/`](backend/docs/) explaining it
**twice**: plain-language (hotel-keycard style) AND technical with real code
mapping. Target structure: mental model → why it exists → what it does/doesn't do
→ data & fields → step-by-step with diagram + code map → idempotency/safety →
rate limits/failure modes & recovery → config → try-it-yourself → FAQ → glossary.
Bar: a newcomer can rebuild the module from scratch and understand *why*. Write
the doc as part of each stage, not after. `docs/ingestion.md` is the current best
example; keep raising it.

## Conventions & gotchas

- `userId="me"` means "the owner of the credentials," not a literal address.
- Auth self-heals: `client._authenticate` catches `RefreshError` (invalid_grant /
  revoked / scope-changed token) → deletes `token.json` and re-runs the consent
  flow. The UI has a **connect-first** step (`POST /api/connect`) before Organize.
- **Full-inbox crawl** (`POST /api/runs/full` → `manager.start_full` →
  `app/ingestion/full.py`): background thread that ingests ALL metadata page by
  page and classifies each batch as it goes (proposals appear progressively).
  **Batched fetch**: `client.get_messages_metadata_batch` pulls ~50 messages per
  HTTP batch request (20k ≈ 400 round-trips, not 20k) with per-item transient
  retry. Resilient (per-item None → skip+continue; client retries 403 rate-limit/
  429/5xx with capped backoff, `max_retries=8`), resumable (skips already-stored ids;
  classifies any stored message still lacking a proposal at the end). Progress has
  `done`(classified)/`total`(messagesTotal)/`ingested`/`errors`; survives browser
  refresh via `run.last_progress` in `/api/status` → UI reconnects the SSE.
  UI "Everything" triggers it; bounded "Fetch up to N" still uses the graph run.
  `classify_messages(store, classifier, metas)` is the shared per-list classifier.
- **Approve/apply are store-driven, not run-driven.** Group approve
  (`/api/groups/decide`) and **apply** (`POST /api/apply` → `manager.apply_approved`
  → `executor.apply_approved()`) work directly on the SQLite store + executor,
  independent of any live LangGraph run. The UI dashboard shows whenever the store
  has pending/needs-review/approved work (even after a browser refresh or backend
  restart) — earlier it only showed during a live `paused` run, which broke apply
  after refresh. The graph's `apply` node still exists for the CLI/tests path.
- **Dry-run toggle** at runtime: `POST /api/settings/dry_run {enabled}` →
  `manager.set_dry_run` flips `settings.dry_run` + `executor.dry_run`. UI top-bar
  LIVE/DRY-RUN button. In DRY-RUN, Apply only simulates (audit `dry_run`) and
  proposals STAY `APPROVED` — that's why "approved" can persist and nothing lands
  in Gmail.
- **Personal heuristic** (rules.py `_looks_personal`): a human 1:1 sender (personal
  provider, no bulk/no-reply markers) → `Personal` as a lowest-priority fallback.
  The LLM prompt (llm.py) also has few-shot examples incl. person→Personal.
- Ingestion stores **metadata only** — no bodies. `parser.parse_metadata()` is the
  metadata path; `parser.parse_email()` (full body) is kept for a later stage.
- Rate-limit retries (`_execute_with_retry`) trigger only on HTTP 403 containing
  `rateLimitExceeded`; other errors re-raise. History-expired (404) → full resync.
- Ingestion is idempotent (upsert by message id); safe to re-run.
- `pydantic` and `PyYAML` are real dependencies — keep them in `requirements.txt`.
- This is **not** a git repository yet (git init is Stage 6).
- `tests/fixtures/raw_email.json` is a captured sample (a Naukri job alert) used
  as a test fixture. It's a REAL email (gitignored); Stage 6 must commit a
  redacted version so tests run after a clone.
- Central config lands in `app/settings.py` (Stage 3); FastAPI under `app/api/`
  (run `uvicorn app.api.main:app`); LangGraph workflow under `app/graph/`.
```
