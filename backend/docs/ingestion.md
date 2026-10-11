# How ingestion works

**Stage 1 of the pipeline.** This document explains how the app copies your
mailbox into a small local database — and, just as importantly, what it
deliberately does *not* do. It is written so **anyone** can follow it, while
staying precise enough that a developer could rebuild it from scratch.

All of the behaviour described here lives in three files:
[`app/ingestion/service.py`](../app/ingestion/service.py) (the orchestration),
[`app/store/db.py`](../app/store/db.py) (the database), and
[`app/gmail/client.py`](../app/gmail/client.py) (the Gmail calls).

> **Read the authentication doc first.** Ingestion assumes we already have a
> valid Gmail connection. How that connection is made — OAuth, the keycard, the
> two secret files — is explained in
> [`authentication.md`](./authentication.md).

---

## 1. The one-sentence version

> Ingestion fetches a little bit of information about each email — **just the
> envelope, never the letter inside** — and files it on an index card in a local
> drawer, so the rest of the app can work quickly without disturbing your
> mailbox again.

---

## 2. The mental model: an index-card drawer 🗂️

Imagine a librarian who, instead of re-reading every book each time someone asks
a question, writes a small **index card** for each one: title, author, date, which
shelf it's on. The cards live in a drawer on the librarian's desk.

- The **books** are your emails, which stay safely in Gmail.
- The **index cards** are rows in our local database (one per email).
- A card holds only the **spine information** — who it's from, the subject, the
  date, Gmail's one-line preview — **never the contents of the book.**
- Once a card is written, the librarian answers questions from the **drawer**,
  not by walking back to the shelves.

Writing the cards once, then working from the drawer, is the whole idea. It is
why the app is fast, why it respects Gmail's limits, and why it keeps so little
of your data.

---

## 3. Why this exists (the problem it solves)

A naïve version of this app would call Gmail *every* time it needed to show you an
email or decide how to label it. That is bad for three concrete reasons:

| Problem | What goes wrong | How ingestion fixes it |
| --- | --- | --- |
| **Rate limits** | Gmail caps how fast an app may call it. Re-fetching constantly hits that ceiling and the app starts failing. | We fetch each email **once** into the drawer, then never re-fetch to classify, display, or restart. |
| **Speed** | Network round-trips are slow; a live call per email makes the UI crawl. | Reading a local SQLite row is effectively instant. |
| **Sensitivity** | The more of your mail we read and hold, the bigger the risk if anything leaks. | We copy **metadata only** — no message bodies — so there is far less to protect. |

This is a deliberate, production-minded choice: **ingest once, operate on our own
copy.**

---

## 4. What it does — and does NOT do

**It does:**

- Read a small set of fields per message (metadata only).
- Store them in a local SQLite file, one row per message.
- Keep the drawer in sync as new mail arrives (incremental sync).

**It does NOT:**

- ❌ Read or store the **body** of any email.
- ❌ Change anything in Gmail — no labels, no archiving, nothing. (Labelling is a
  *later* stage and only ever happens after you approve it.)
- ❌ Send your data anywhere. The database is a file on your own machine.

---

## 5. What we store (and why each field earns its place)

Every email becomes one row in the `messages` table
([`app/store/db.py`](../app/store/db.py)). Here is the whole card:

| Field | Example | Why we keep it |
| --- | --- | --- |
| `id` | `18f...a2` | Gmail's unique message id — the primary key. Lets re-ingestion *update* a card instead of duplicating it. |
| `thread_id` | `18f...a0` | Groups a conversation. |
| `from_addr` | `Naukri <...@naukri.com>` | The sender — the single strongest classification signal. |
| `to_addrs` | `["me@…"]` | Recipients (stored as JSON). |
| `subject` | `Top Openings for Data Analyst…` | Classification signal. |
| `snippet` | `Chamanth, we found jobs…` | Gmail's own ~100-char preview. **This is as close to the body as we get** — and Gmail already exposes it. |
| `internal_date` | `1790912299000` | When Gmail received it (ms since epoch). For sorting. |
| `received_at` | `Fri, 2 Oct 2026 …` | The human-readable `Date` header. |
| `label_ids` | `["INBOX","UNREAD"]` | Labels the message already has. |
| `size_estimate` | `303343` | Rough size in bytes. Diagnostics only. |
| `history_id` | `15224046` | Gmail's change-counter at fetch time (see §7). |
| `ingested_at` | `2026-10-10T…Z` | When *we* wrote/updated the card. Audit trail. |

> **Note the absence.** There is no `body` column, by design. The richest text we
> store is `snippet`, which Gmail hands out freely. If a later stage ever needs
> the body, that will be a separate, clearly-documented decision — not a quiet
> default.

---

## 6. The two sync modes

Ingestion picks its mode automatically based on whether the drawer already has a
**bookmark** (a saved `history_id` in the `sync_state` table).

```
run_sync()
   │
   ├─ Do we have a saved history id?  ── NO ──▶  FULL SYNC
   │                                              (first run)
   └─ YES ──▶ INCREMENTAL SYNC
                 │
                 └─ history id too old? (HTTP 404) ──▶ fall back to FULL SYNC
```

### Full sync — "write a card for everything" (first run)

*Code: `IngestionService._full_sync()`*

1. Ask Gmail **who we are** and for the current change-counter.
   `profile = client.get_profile()` → gives `emailAddress` and `historyId`.
   We record that `historyId` **before fetching** as our bookmark, so anything
   that arrives *during* the run is caught by the next incremental sync — nothing
   slips through the cracks.
2. **List message ids**, page by page (`client.list_messages`, 100 at a time).
3. For each id, **fetch metadata** (`client.get_message_metadata`) and write the
   card (`store.upsert_message`).
4. Save the bookmark (`store.set_sync_state(..., mark_full_sync=True)`).

### Incremental sync — "only what changed" (every later run)

*Code: `IngestionService._incremental_sync()`*

Instead of listing the whole mailbox again, we ask Gmail's **history API** a
single question: *"what changed since bookmark X?"*
(`client.list_history(start_history_id)`).

Gmail replies with a list of changes — messages **added**, **deleted**, or
**re-labelled**. We then:

- refetch metadata for anything **added or re-labelled** (its card is new or
  stale),
- **delete** cards for anything removed,
- and move the bookmark forward to Gmail's latest `historyId`.

A quiet mailbox produces an empty change list, so a routine run does **almost no
work** — which is exactly what keeps us comfortably under the rate limits.

---

## 7. What is a `history_id`? (the bookmark) 🔖

Think of your mailbox as having a single **ever-increasing counter** that ticks up
on *every* change (mail arrives, you label something, you delete something). That
counter is the `history_id`.

- When we finish a sync, we write down the current count — our **bookmark**.
- Next time, we hand that bookmark to Gmail and say *"replay everything after
  this."*
- Gmail keeps this replay history for a **limited time** (days to a week). If our
  bookmark is older than that, Gmail can't replay it and returns **HTTP 404**. We
  treat that as a signal to simply **re-read everything** (a fresh full sync) and
  set a new bookmark. No data is lost — it's just a slower catch-up.

This fallback is handled in `run_sync()`.

---

## 8. Idempotency & safety 🧯

- **Safe to re-run.** Cards are written with an *upsert* keyed on the message id
  (`INSERT … ON CONFLICT(id) DO UPDATE`). Running ingestion twice yields the same
  drawer, never duplicates. *(Verified by the offline test.)*
- **Safe to interrupt.** If a full sync is killed halfway, the cards already
  written stay written; the next run's full-sync fallback simply finishes the
  job. The bookmark is only advanced at the end.
- **Read-only toward Gmail.** Ingestion never calls a Gmail method that changes
  your mailbox. The only writing it does is to the local database.

---

## 9. Rate limits — the honest explanation ⏱️

A common misconception: *"fetching metadata instead of the full message saves
quota."* It mostly **doesn't**. Gmail charges a fixed **quota cost per method**
(roughly 5 units for `messages.get`) **regardless of the `format`.** So
`format=metadata` is chosen for **data minimization and smaller, faster
responses** — not to save quota.

The things that *actually* protect us from rate limits are architectural:

1. **Fetch-once drawer** — we never re-download a message we already have.
2. **Incremental sync** — routine runs touch only what changed, often nothing.
3. **Exponential backoff** — if Gmail does say "slow down" (HTTP 403
   `rateLimitExceeded`), `client._execute_with_retry` waits and retries with
   increasing, slightly-randomised delays.
4. **Batching** — the full-inbox crawl bundles ~50 `get`s into one HTTP batch
   request (`client.get_messages_metadata_batch`), cutting a 20k crawl from ~20k
   round-trips to ~400. Per-item transient failures are retried within the batch.

So we get the best of both: **less sensitive data *and* real rate-limit safety**,
from two different mechanisms. Don't conflate them.

---

## 10. Failure modes & recovery

| Symptom | Cause | What happens / what to do |
| --- | --- | --- |
| `HttpError 404` during incremental sync | Bookmark `history_id` expired | Automatic: falls back to a full resync and re-bookmarks. |
| `HttpError 403` `rateLimitExceeded` | Too many calls too fast | Automatic: backoff + retry (up to 5 times). |
| Browser opens asking you to sign in | No / invalid `token.json` | Expected on first run or after a scope change — click **Allow**. See [`authentication.md`](./authentication.md). |
| `FileNotFoundError: credentials.json` | Running from the wrong folder | Run from inside `backend/` — paths are relative. |
| Fewer messages than expected | You passed `--max` (default 50) | Use `--all` for the whole mailbox. |

---

## 11. Configuration

| Knob | Where | Default |
| --- | --- | --- |
| Max messages per run | `--max N` on `ingest.py` | `50` |
| Fetch everything | `--all` on `ingest.py` | off |
| Database path | `--db PATH` on `ingest.py` | `mailbox.db` |
| Which headers are fetched | `headers=` in `client.get_message_metadata` | From, To, Subject, Date |

---

## 12. Try it yourself

```bash
cd backend
source .venv/bin/activate

# First run → full sync (opens the browser the first time).
python ingest.py --max 50

# Run it again → incremental; usually "no changes since last sync".
python ingest.py

# Inspect the drawer directly (read-only SQL):
sqlite3 mailbox.db "SELECT from_addr, subject FROM messages LIMIT 5;"
```

And the offline test — **no Gmail, no network** — which proves the parser, the
store, and both sync modes:

```bash
python tests/test_ingestion_offline.py
```

---

## 13. FAQ

**Does this read my emails' contents?** No. Only metadata + Gmail's own one-line
snippet. There is no `body` field in the database.

**Does it change anything in Gmail?** No. Ingestion is read-only toward Gmail.
Labelling comes in a later stage and always needs your approval first.

**Where is my data?** In a single SQLite file (`mailbox.db`) on your own machine.
It is listed in [`.gitignore`](../.gitignore) and never leaves your computer.

**Is it safe to run repeatedly?** Yes — it's idempotent. Re-running updates cards
in place and, after the first run, fetches only what changed.

**What if I delete `mailbox.db`?** Nothing breaks. The next run does a fresh full
sync and rebuilds it from Gmail.

---

## 14. Glossary

- **Metadata** — information *about* a message (sender, subject, date, labels),
  as opposed to its body.
- **Snippet** — the short preview text Gmail generates for each message.
- **Full sync** — read metadata for every message once.
- **Incremental sync** — read only what changed since the last run.
- **history_id** — Gmail's mailbox change-counter; our "bookmark" for incremental
  sync.
- **Idempotent** — running it again produces the same result, no duplicates.
- **Upsert** — insert if new, update if it already exists.

---

## 15. Code map

| File | Role |
| --- | --- |
| [`ingest.py`](../ingest.py) | Entry point. Parses `--max/--all/--db`, runs a sync, prints a summary. |
| [`app/ingestion/service.py`](../app/ingestion/service.py) | `IngestionService` — chooses full vs incremental, orchestrates fetch + store. |
| [`app/store/db.py`](../app/store/db.py) | `Store` — SQLite schema, `upsert_message`, `sync_state`. |
| [`app/gmail/client.py`](../app/gmail/client.py) | `get_message_metadata`, `list_history`, `list_messages`, `get_profile`, backoff. |
| [`app/gmail/parser.py`](../app/gmail/parser.py) | `parse_metadata()` — Gmail JSON → `MessageMeta`. |
| [`app/models/message.py`](../app/models/message.py) | The `MessageMeta` model (metadata only). |
| [`tests/test_ingestion_offline.py`](../tests/test_ingestion_offline.py) | Offline proof of the above. |
