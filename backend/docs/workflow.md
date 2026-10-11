# How the workflow works (human-in-the-loop)

**Stage 3 of the pipeline — the spine that ties everything together.** This
document explains the end-to-end flow, how the app *pauses for a human* before
changing anything, how approved labels get applied, and how every change is
recorded. Written so **anyone** can follow it, with the real code mapping for
developers.

The code lives in [`app/graph/`](../app/graph/): `workflow.py` (the LangGraph
graph), `approval.py` (the human surface), `executor.py` (the only thing that
touches Gmail), and `factory.py` (wiring). It builds on
[`ingestion.md`](./ingestion.md) and [`classification.md`](./classification.md).

---

## 1. The one-sentence version

> The app fetches, labels-on-paper, then **stops and asks you**; only the items
> you approve get their label applied in Gmail, and every change is written to a
> logbook.

---

## 2. The mental model: a sorter who waits for your nod 🙋

Earlier stages are like a mail sorter who reads each envelope and writes a
sticky-note label — *but doesn't stick it on yet*. Stage 3 is the moment the
sorter **brings the tray to you**:

- "Here's what I'm confident about" (the **pending** pile).
- "Here's what I couldn't decide" (the **needs-review** pile).

You say yes/no (and optionally label the unsure ones). Only then does the sorter
**actually stick on** the labels you approved — and note every one in a logbook.

The app never sticks a label on without your nod. That "stop and wait" is a real
mechanism, not a figure of speech — see §4.

---

## 3. The four steps

```
   ingest ──▶ classify ──▶ review (HUMAN) ──▶ apply
   (Stage 1)  (Stage 2)    ▲        │           │
                           │  interrupt()       │ only APPROVED
                           └── resume(verdicts) ─┘ get labels in Gmail
```

| Step | Node | What it does | Touches Gmail? |
| --- | --- | --- | --- |
| **ingest** | `ingest_node` | Sync mailbox metadata into the store. | reads only |
| **classify** | `classify_node` | Turn messages into proposals (rules→LLM→NEEDS_REVIEW). | no |
| **review** | `review_node` | **Pause.** Hand pending + needs-review to the human; record verdicts. | no |
| **apply** | `apply_node` | Add the approved labels; write the audit log. | **writes labels** |

---

## 4. The pause: LangGraph `interrupt()` 🛑

The review step calls LangGraph's `interrupt()`. Concretely:

1. You start the graph with a **thread id** (so it can be resumed later):
   `graph.invoke({...}, {"configurable": {"thread_id": "t1"}})`.
2. It runs ingest → classify, reaches `review_node`, and calls
   `interrupt({"pending": [...], "needs_review": [...]})`. The call **returns**
   to you with that payload under `state["__interrupt__"]` — the graph is now
   parked at a checkpoint.
3. Your UI shows the piles and collects decisions.
4. You resume the *same thread*:
   `graph.invoke(Command(resume={"<message_id>": "APPROVED"|"REJECTED"}), cfg)`.
   `review_node` picks up exactly where it left off, records the verdicts, and
   the graph flows into `apply`.

This is why the backend was built service-first: the API (Stage 4) is what calls
`invoke` and `resume` across two HTTP requests; the graph state survives in a
**checkpointer** in between.

> **v1 checkpointer:** `MemorySaver` (in-process). Good for a single running
> server. A production upgrade is a persistent `SqliteSaver` so a parked workflow
> survives a restart — noted for later; it's a one-line swap where the graph is
> compiled.

---

## 5. Applying labels — the only write to Gmail (`executor.py`)

`LabelExecutor.apply_approved()` walks every **APPROVED** proposal that has a
label and, for each:

1. `get_or_create_label(name)` — create the Gmail label if it doesn't exist
   (cached within a run, so `Finance/Statements` is resolved once no matter how
   many messages use it).
2. `modify_message_labels(message_id, add_label_ids=[id])` — attach it.
3. Mark the proposal `APPLIED` and write an audit row.

**It only ever adds labels.** No removal, no archive, no delete — v1, by design.

### Dry-run 🧪

With `DRY_RUN=1` (or `Settings.dry_run`), the executor touches **nothing** in
Gmail. It writes an audit row with `outcome=dry_run` ("would add label …") and
leaves the proposal `APPROVED`, so a later real run applies it. Use it to preview
exactly what *would* change.

---

## 6. The audit log 📒

Every apply attempt — real, dry-run, or error — appends one row to `audit_log`:

| Column | Meaning |
| --- | --- |
| `message_id`, `label` | what was (or would be) labelled |
| `action` | `apply_label` |
| `outcome` | `applied` / `dry_run` / `error` |
| `detail` | the created label id, or the error text |
| `created_at` | timestamp |

It's **append-only** — the permanent record of everything the app did to the
mailbox. A single failing message is logged as `error` and the batch continues.

---

## 7. Proposal lifecycle

```
PENDING ──approve──▶ APPROVED ──apply (real)──▶ APPLIED
   │                    ▲
   └──reject──▶ REJECTED │
NEEDS_REVIEW ──human sets a label──┘ (becomes APPROVED)
```

Re-running ingest/classify never disturbs a proposal that reached
`APPROVED/REJECTED/APPLIED` (see [classification.md](./classification.md) §7).

---

## 8. Configuration (`app/settings.py`)

| Setting | Env var | Default |
| --- | --- | --- |
| Database path | `MAILBOX_DB` | `mailbox.db` |
| Taxonomy path | `TAXONOMY_PATH` | `config/taxonomy.yaml` |
| Classifier model | `CLASSIFIER_MODEL` | LLM default (`claude-opus-5-5`) |
| Use the LLM | `USE_LLM` | `true` |
| **Dry-run** | `DRY_RUN` | `false` |
| Workflow thread id | `WORKFLOW_THREAD_ID` | `default` |

---

## 9. Try it yourself (programmatically)

The workflow is driven by code (and, in Stage 4, the API), because the pause
spans two calls. A minimal driver:

```python
from app.settings import Settings
from app.graph.factory import build_default_workflow
from langgraph.types import Command

graph, services = build_default_workflow(Settings.from_env())  # builds GmailClient → OAuth
cfg = {"configurable": {"thread_id": "t1"}}

state = graph.invoke({"max_messages": 50}, cfg)       # ingest+classify, then pause
pending = state["__interrupt__"][0].value["pending"]  # show these to the human
# ...collect verdicts...
final = graph.invoke(Command(resume={"<id>": "APPROVED"}), cfg)  # apply
print(final["apply"])
```

Offline test (no Gmail, no Claude) proving the executor and the full
interrupt/resume cycle:

```bash
python tests/test_workflow_offline.py
```

---

## 10. Failure modes & recovery

| Symptom | Cause | What happens |
| --- | --- | --- |
| `apply` did nothing | No proposals were APPROVED | Expected — approve some first. |
| One message errored, rest applied | Gmail rejected that modify | Logged as `error`; batch continues. |
| Parked workflow lost after restart | `MemorySaver` is in-process | Re-run; ingest/classify are idempotent and decided proposals are preserved. Upgrade to `SqliteSaver` to persist. |
| Browser opens on first real run | OAuth for `gmail.modify` | Expected once — this is the re-consent (see [authentication.md](./authentication.md)). |

---

## 11. FAQ

**Can the app apply a label without me approving it?** No. Only `APPROVED`
proposals are applied, and approval only happens by your verdict in `review`.

**What if I approve by mistake?** v1 only *adds* a label, which you can remove in
Gmail in one click. Nothing is deleted or archived.

**Does dry-run cost anything or change anything?** No Gmail changes and no Gmail
calls — it only writes local audit rows describing what it *would* do.

**Where do the "needs review" items go?** They stay `NEEDS_REVIEW` until a human
labels them (the UI will let you pick a label, which approves them).

---

## 12. Glossary

- **Node** — one step in the LangGraph workflow (ingest/classify/review/apply).
- **interrupt()** — LangGraph's pause; returns control to the caller with a payload.
- **Command(resume=…)** — how the caller feeds the human's answer back in.
- **Checkpointer** — where the paused graph's state is saved between calls.
- **Audit log** — the append-only record of every apply.
- **Dry-run** — plan the changes, apply nothing.

---

## 13. Code map

| File | Role |
| --- | --- |
| [`app/graph/workflow.py`](../app/graph/workflow.py) | Builds the LangGraph graph; `review_node` holds the `interrupt()`. |
| [`app/graph/approval.py`](../app/graph/approval.py) | `ApprovalService` — list pending/needs-review, record verdicts. |
| [`app/graph/executor.py`](../app/graph/executor.py) | `LabelExecutor` — apply approved labels, dry-run, audit. The only Gmail writer. |
| [`app/graph/factory.py`](../app/graph/factory.py) | Wires real services from `Settings` (lazy OAuth). |
| [`app/settings.py`](../app/settings.py) | Central env-driven config. |
| [`app/store/db.py`](../app/store/db.py) | Adds `audit_log` + proposal status transitions. |
| [`tests/test_workflow_offline.py`](../tests/test_workflow_offline.py) | Offline proof of executor + interrupt/resume. |
