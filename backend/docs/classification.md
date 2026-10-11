# How classification works

**Stage 2 of the pipeline.** This document explains how the app decides which
label each email *should* get — and, crucially, how it refuses to guess when it
isn't sure. It is written so **anyone** can follow it, while staying precise
enough for a developer to rebuild it.

Classification reads from the local store (built in
[`ingestion.md`](./ingestion.md)) and writes **proposals** — suggestions a human
will approve later. **It never changes anything in Gmail.**

The code lives in [`app/classifier/`](../app/classifier/): `taxonomy.py`,
`rules.py`, `llm.py`, `classifier.py` (the orchestrator), and `runner.py`.

---

## 1. The one-sentence version

> For each email, a fast **rule-of-thumb clerk** tries first; if stumped, a
> **smart assistant** (Claude) tries; and if *that's* still unsure, the email is
> dropped in a **"needs review" tray** for a human — it is never force-fitted
> into the wrong label.

---

## 2. The mental model: a mailroom with a fixed set of trays 🗂️

Picture a mailroom with a **fixed, labelled set of trays** (the taxonomy). Two
sorters work the incoming pile, in order:

1. **The clerk (rules).** Fast, free, follows a cheat-sheet: *"anything from
   naukri.com → the Job/Applications tray."* Handles the obvious ones instantly.
2. **The assistant (the LLM).** For whatever the clerk sets aside, a smart
   assistant reads the envelope and picks the best tray **from the same fixed
   set** — or says *"I don't know."*
3. **The review tray (NEEDS_REVIEW).** If even the assistant is unsure (or the
   assistant is off duty), the email goes to a review tray. **A human decides;
   the mailroom never guesses.**

The trays are fixed on purpose. Neither sorter can invent a new tray — that's
what keeps the label set from exploding.

---

## 3. Why this design (the three states)

A classifier that always picks *something* will confidently mislabel the hard
cases — the worst outcome for a tool that touches someone's mailbox. So the
output is deliberately **three-state**:

| Outcome | Meaning | Stored as | Who acts next |
| --- | --- | --- | --- |
| **rules** | A deterministic rule matched | proposal, `status=PENDING` | human approves |
| **llm** | Claude chose a label confidently | proposal, `status=PENDING` | human approves |
| **needs_review** | Nobody was confident | proposal, `status=NEEDS_REVIEW` | human classifies |

Every proposal also records **why** (`reason`) and **how sure** (`confidence`),
so no decision is a black box.

---

## 4. The taxonomy: fixed, hierarchical, and data-not-code

The trays come from [`config/taxonomy.yaml`](../config/taxonomy.yaml). Two
properties matter:

- **Fixed & closed.** Both sorters may only choose a name that exists in the
  file. New category? Edit the file — no code change (`taxonomy.py` just loads
  it).
- **Hierarchical.** Labels nest via Gmail's `/` convention
  (`Job/Offers & Documents` sits under `Job`). `taxonomy.py` flattens the tree
  and tags each label with a **depth** (0 = category, 1 = sub-label).

### Graceful degradation along the hierarchy

Confidence maps onto the hierarchy, so the app applies the most specific label
it's *sure* of:

```
sure of the sub-label   → "Job/Offers & Documents"
sure only of category   → "Job"
not sure of either      → NEEDS_REVIEW
```

---

## 5. The rules clerk (`rules.py`)

For each label, the taxonomy may list `rules` hints:

- `from_domains` — the sender's domain (e.g. `naukri.com`)
- `from_contains` — a substring of the sender (e.g. `.gov`)
- `subject_contains` — a substring of the subject (e.g. `invoice`)

Matching is **whole-word** and case-insensitive — *not* raw substring. This
matters: a substring matcher wrongly put "E-Mandate" from `idfcf·irs·t.bank.in`
under *Tax* (because the Tax rule listed `"irs"`), and a donation that mentioned
"Feeding" under *Education* (rule `"fee"`). Whole-word matching (`\b…\b`) stops
both.

When several labels match, the winner is ranked by **`(depth, strength)`**:

- **depth** — a sub-label (`Finance/Securities & Demat`) beats its parent (`Finance`).
- **strength** — a sender-**domain** match (3) beats a sender-substring (2) beats
  a subject-word (1). The sender domain is the most reliable signal, so an NSE
  email goes to *Securities* even if its subject says "Balance".

A match returns the label plus a reason (`"rule: sender domain is 'naukri.com'"`).
No match → the LLM gets a turn. **Keep rule tokens specific** (phrases like
`"account statement"`, not bare `"report"`/`"balance"`) to avoid false hits.

> Rules are cheap, deterministic, and explainable — which is why they run first
> and handle the bulk of a typical inbox for free.

---

## 6. The LLM assistant (`llm.py`)

When rules don't match, Claude classifies — but on a tight, **cheap** leash:

- **Batched.** The runner collects everything the rules missed and sends **~20
  emails per API call** (`chunk_size`), not one call each. For a 50-email inbox
  that's ~2 LLM calls total. This is the main cost lever — "not heavy on LLM".
- **Cheap model.** Default **`claude-haiku-5-5`** (≈40× cheaper than Opus), with
  the label menu prompt-cached and `effort: "low"`. Override via `CLASSIFIER_MODEL`.
- **Fixed menu.** The prompt lists every label + description; the model must pick
  one name or answer `UNKNOWN`. It cannot invent a label.
- **Abstention is first-class.** `UNKNOWN`, an invalid label, malformed JSON, a
  refusal, or confidence below the threshold (default **0.6**) → "not confident"
  → NEEDS_REVIEW.
- **Metadata only.** Sender + subject + snippet; no message body.

### Enabling it

Put your key in `backend/.env` (copy from `.env.example`):
`ANTHROPIC_API_KEY=sk-ant-...`. **Without a key the app is rules-only**, so most
mail the rules don't match lands in NEEDS_REVIEW — turning the LLM on is what
shrinks that pile.

### Graceful disable — no key, no problem

If the `anthropic` package or `ANTHROPIC_API_KEY` is missing, `llm.enabled` is
`False`, the assistant is skipped entirely, and everything the rules didn't
catch flows to NEEDS_REVIEW. **The app runs end-to-end with zero LLM setup** —
the LLM only *improves* coverage when switched on.

### Model choice & cost 💸

The model defaults to **`claude-opus-5-5`**. Email classification is a simple,
high-volume task, so you'll usually want a cheaper model:

```bash
export CLASSIFIER_MODEL=claude-haiku-5-5   # ~40x cheaper per token than Opus
# or per run:
python classify.py --model claude-haiku-5-5
```

Cost is naturally contained three ways: **(1)** rules handle the obvious mail for
free, so the LLM only sees the remainder; **(2)** once a message has a proposal,
re-running skips it (we don't re-pay); **(3)** a decided message is never
re-classified.

---

## 7. What gets stored: proposals

One row per message in the `proposals` table (keyed on `message_id`):

| Column | Meaning |
| --- | --- |
| `message_id` | which email (one proposal per message) |
| `label` | the proposed label (NULL for NEEDS_REVIEW) |
| `method` | `rules` / `llm` / `needs_review` |
| `confidence` | 1.0 for rules, the model's score for llm, 0.0 otherwise |
| `reason` | human-readable justification |
| `status` | `PENDING` / `NEEDS_REVIEW` (later: `APPROVED`/`REJECTED`/`APPLIED`) |
| `created_at` / `decided_at` | timestamps |

**Human decisions are protected.** `classify_store` skips any message with a
status in `APPROVED/REJECTED/APPLIED`, so re-running never clobbers a human
choice — unless you pass `--reclassify`.

---

## 8. Configuration

| Knob | Where | Default |
| --- | --- | --- |
| Use the LLM | `--no-llm` on `classify.py` | on (if a key exists) |
| LLM model | `--model` / `CLASSIFIER_MODEL` env | `claude-opus-5-5` |
| Confidence threshold | `min_confidence` in `LLMClassifier` | `0.6` |
| Re-decide everything | `--reclassify` | off |
| Database path | `--db` | `mailbox.db` |

---

## 9. Try it yourself

```bash
cd backend
source .venv/bin/activate

python ingest.py --max 50       # Stage 1 first (fills mailbox.db)

python classify.py              # rules + LLM (if ANTHROPIC_API_KEY set)
python classify.py --no-llm     # rules only → the rest become NEEDS_REVIEW

# inspect the proposals:
sqlite3 mailbox.db "SELECT status, method, label, COUNT(*) FROM proposals GROUP BY 1,2,3;"
```

Offline test (no Claude call, no network) — proves taxonomy, rules, the 3-state
orchestrator, and the proposals store:

```bash
python tests/test_classifier_offline.py
```

---

## 10. Failure modes & recovery

| Symptom | Cause | What happens |
| --- | --- | --- |
| Everything is NEEDS_REVIEW | No `ANTHROPIC_API_KEY`, or `--no-llm`, and few rules matched | Expected. Add a key / more rules, or classify manually in the UI (Stage 5). |
| LLM labels look off | Model too small, or taxonomy descriptions vague | Raise `CLASSIFIER_MODEL`, sharpen descriptions, or add a rule for that sender. |
| A sender keeps going to the wrong tray | Ambiguous subject/sender | Add a `from_domains`/`subject_contains` rule — rules always win over the LLM. |
| Re-run didn't update a message | It was already human-decided | By design. Use `--reclassify` to override. |

---

## 11. FAQ

**Does this change my Gmail?** No. Stage 2 only writes proposals to the local
database. Labels are applied (after approval) in a later stage.

**Do I need an API key?** No. Without one, the app works rules-only and routes
the rest to NEEDS_REVIEW. A key simply improves automatic coverage.

**Can it create labels I didn't define?** No. Both sorters are confined to the
fixed taxonomy; the LLM can only answer with a menu name or `UNKNOWN`.

**Will it mislabel to avoid saying "I don't know"?** No — that's the whole point
of the third state. Low confidence → NEEDS_REVIEW.

---

## 12. Glossary

- **Taxonomy** — the fixed set of labels (trays) defined in `taxonomy.yaml`.
- **Rule** — a deterministic sender/subject match that picks a label for free.
- **Confidence** — how sure the LLM is (0–1); below the threshold → abstain.
- **Proposal** — a stored suggestion (`PENDING` or `NEEDS_REVIEW`) awaiting a human.
- **NEEDS_REVIEW** — the "don't guess" state; a human must decide.
- **Graceful degradation** — apply the most specific label you're sure of, else step up/aside.

---

## 13. Code map

| File | Role |
| --- | --- |
| [`classify.py`](../classify.py) | Entry point. Flags, progress line, summary. |
| [`app/classifier/taxonomy.py`](../app/classifier/taxonomy.py) | Loads `taxonomy.yaml` → flattened `LabelDef`s + menu. |
| [`app/classifier/rules.py`](../app/classifier/rules.py) | `RulesClassifier` — deterministic, most-specific wins. |
| [`app/classifier/llm.py`](../app/classifier/llm.py) | `LLMClassifier` — Claude on the fixed menu; abstains + self-disables. |
| [`app/classifier/classifier.py`](../app/classifier/classifier.py) | The 3-state orchestrator. |
| [`app/classifier/runner.py`](../app/classifier/runner.py) | `build_classifier` + `classify_store` (walks store, writes proposals, progress). |
| [`app/models/classification.py`](../app/models/classification.py) | The `Classification` result model. |
| [`config/taxonomy.yaml`](../config/taxonomy.yaml) | The fixed label taxonomy (data, not code). |
| [`tests/test_classifier_offline.py`](../tests/test_classifier_offline.py) | Offline proof of the above. |
