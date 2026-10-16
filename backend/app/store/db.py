"""
SQLite store for the ingestion pipeline.

Holds our own copy of mailbox METADATA plus sync bookkeeping, so the rest of the
app reads from here instead of hitting Gmail live. Everything is idempotent:
re-ingesting the same message updates its row rather than duplicating it.

Later stages add `proposals` and `audit_log` tables alongside these.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.models.message import MessageMeta


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    thread_id     TEXT,
    from_addr     TEXT,
    to_addrs      TEXT,          -- JSON array
    subject       TEXT,
    snippet       TEXT,
    internal_date INTEGER,       -- ms since epoch
    received_at   TEXT,          -- raw Date header
    label_ids     TEXT,          -- JSON array
    size_estimate INTEGER,
    history_id    TEXT,
    ingested_at   TEXT           -- ISO-8601 UTC, when we stored/updated it
);

CREATE INDEX IF NOT EXISTS idx_messages_internal_date
    ON messages (internal_date);

CREATE TABLE IF NOT EXISTS sync_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    email_address     TEXT,
    last_history_id   TEXT,
    last_sync_at      TEXT,
    last_full_sync_at TEXT
);

-- One proposal per message: the classifier's suggestion + its fate.
-- status: PENDING (has a label, awaiting approval)
--       | NEEDS_REVIEW (classifier couldn't decide; human must look)
--       | APPROVED | REJECTED | APPLIED  (set in later stages)
CREATE TABLE IF NOT EXISTS proposals (
    message_id  TEXT PRIMARY KEY,
    label       TEXT,            -- NULL for NEEDS_REVIEW
    method      TEXT,            -- rules | llm | needs_review
    confidence  REAL,
    reason      TEXT,
    status      TEXT,
    action      TEXT DEFAULT 'label',  -- label | archive | trash (what Apply does)
    created_at  TEXT,
    decided_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals (status);

-- Append-only record of every inbox-changing action we take (or would take, in
-- dry-run). This is the audit trail: what label, on which message, with what
-- outcome, and when. Nothing is ever deleted from here.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT,
    label       TEXT,
    action      TEXT,            -- label | archive | trash | undo
    outcome     TEXT,            -- applied | dry_run | error | skipped | undone
    detail      TEXT,
    batch_id    TEXT,            -- groups one apply/undo run (for Undo last run)
    created_at  TEXT
);

-- Rules the app LEARNED from your approvals: "mail from <domain> is <label>".
-- A domain with more than one learned label is treated as MULTI-PURPOSE and is
-- never blanket-labelled — its mail keeps going through content classification.
CREATE TABLE IF NOT EXISTS learned_rules (
    domain      TEXT,
    label       TEXT,
    created_at  TEXT,
    PRIMARY KEY (domain, label)
);
"""


def _domain_of(sender: str) -> str:
    import re
    match = re.search(r"[\w.\-+]+@([\w.\-]+)", sender or "")
    return match.group(1).lower() if match else ""


_KW_STOP = {
    "the", "your", "you", "for", "and", "with", "from", "this", "that", "has",
    "have", "are", "was", "will", "new", "now", "get", "our", "all", "out",
    "inc", "ltd", "pvt", "re", "fwd", "hi", "dear", "mr", "ms", "update",
    "updates", "notification", "email", "mail", "please", "important",
}
# Multi-word phrases worth surfacing as their own cluster.
_KW_BIGRAMS = (
    "debit alert", "credit card", "credit alert", "e-mandate", "low balance",
    "account statement", "bank statement", "mini statement", "payment due",
    "amount due", "out for delivery", "order confirmed", "tax invoice",
    "interview", "offer letter", "payslip", "salary slip", "otp", "verification code",
)


def _subject_keywords(subjects, top: int = 30):
    """Frequent informative words/phrases across subjects → [(keyword, count)] desc."""
    import re
    from collections import Counter

    counter = Counter()
    for s in subjects:
        sl = (s or "").lower()
        for bg in _KW_BIGRAMS:
            if bg in sl:
                counter[bg] += 1
        for w in re.findall(r"[a-z][a-z\-]{2,}", sl):
            if w not in _KW_STOP:
                counter[w] += 1
    return counter.most_common(top)

# A human decision we must not silently overwrite when re-classifying.
DECIDED_STATUSES = ("APPROVED", "REJECTED", "APPLIED")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Thin, explicit wrapper over a SQLite file. No ORM on purpose."""

    def __init__(self, path: str = "mailbox.db"):
        self.path = str(path)
        # check_same_thread=False so a web server (Stage 4) can share it.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Lightweight, additive migrations for stores created before new columns."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(proposals)")}
        if "action" not in cols:
            self.conn.execute(
                "ALTER TABLE proposals ADD COLUMN action TEXT DEFAULT 'label'"
            )
        acols = {r["name"] for r in self.conn.execute("PRAGMA table_info(audit_log)")}
        if "batch_id" not in acols:
            self.conn.execute("ALTER TABLE audit_log ADD COLUMN batch_id TEXT")

    # ---------------------------------------------------------------- messages

    def upsert_message(self, msg: MessageMeta) -> None:
        """Insert a message, or update it in place if we've seen its id."""

        self.conn.execute(
            """
            INSERT INTO messages (
                id, thread_id, from_addr, to_addrs, subject, snippet,
                internal_date, received_at, label_ids, size_estimate,
                history_id, ingested_at
            ) VALUES (
                :id, :thread_id, :from_addr, :to_addrs, :subject, :snippet,
                :internal_date, :received_at, :label_ids, :size_estimate,
                :history_id, :ingested_at
            )
            ON CONFLICT(id) DO UPDATE SET
                thread_id     = excluded.thread_id,
                from_addr     = excluded.from_addr,
                to_addrs      = excluded.to_addrs,
                subject       = excluded.subject,
                snippet       = excluded.snippet,
                internal_date = excluded.internal_date,
                received_at   = excluded.received_at,
                label_ids     = excluded.label_ids,
                size_estimate = excluded.size_estimate,
                history_id    = excluded.history_id,
                ingested_at   = excluded.ingested_at
            """,
            {
                "id": msg.id,
                "thread_id": msg.thread_id,
                "from_addr": msg.from_addr,
                "to_addrs": json.dumps(msg.to_addrs),
                "subject": msg.subject,
                "snippet": msg.snippet,
                "internal_date": msg.internal_date,
                "received_at": msg.received_at,
                "label_ids": json.dumps(msg.label_ids),
                "size_estimate": msg.size_estimate,
                "history_id": msg.history_id,
                "ingested_at": _now_iso(),
            },
        )
        self.conn.commit()

    def delete_message(self, message_id: str) -> None:
        self.conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        self.conn.commit()

    def has_message(self, message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def count_messages(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

    def get_message(self, message_id: str) -> Optional[MessageMeta]:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return self._row_to_meta(row) if row else None

    @staticmethod
    def _row_to_meta(row: sqlite3.Row) -> MessageMeta:
        return MessageMeta(
            id=row["id"],
            thread_id=row["thread_id"] or "",
            from_addr=row["from_addr"] or "",
            to_addrs=json.loads(row["to_addrs"] or "[]"),
            subject=row["subject"] or "",
            snippet=row["snippet"] or "",
            internal_date=row["internal_date"],
            received_at=row["received_at"],
            label_ids=json.loads(row["label_ids"] or "[]"),
            size_estimate=row["size_estimate"],
            history_id=row["history_id"],
        )

    def iter_messages(self):
        """Yield every stored message as a MessageMeta (for classification)."""
        for row in self.conn.execute("SELECT * FROM messages"):
            yield self._row_to_meta(row)

    def has_proposal(self, message_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM proposals WHERE message_id = ?", (message_id,)
        ).fetchone() is not None

    def iter_messages_without_proposal(self):
        """Stored messages that have no proposal yet (resume after interruption)."""
        rows = self.conn.execute(
            "SELECT m.* FROM messages m "
            "LEFT JOIN proposals p ON p.message_id = m.id "
            "WHERE p.message_id IS NULL"
        ).fetchall()
        for row in rows:
            yield self._row_to_meta(row)

    # --------------------------------------------------------------- proposals

    def get_proposal(self, message_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def has_decided_proposal(self, message_id: str) -> bool:
        """True if a human has already approved/rejected/applied this message."""
        row = self.conn.execute(
            "SELECT 1 FROM proposals WHERE message_id = ? AND status IN "
            "('APPROVED','REJECTED','APPLIED')",
            (message_id,),
        ).fetchone()
        return row is not None

    def upsert_proposal(
        self,
        message_id: str,
        label: Optional[str],
        method: str,
        confidence: float,
        reason: str,
        status: str,
    ) -> None:
        """Insert or replace the proposal for a message (keyed on message_id)."""
        self.conn.execute(
            """
            INSERT INTO proposals (
                message_id, label, method, confidence, reason, status, created_at
            ) VALUES (:mid, :label, :method, :conf, :reason, :status, :now)
            ON CONFLICT(message_id) DO UPDATE SET
                label      = excluded.label,
                method     = excluded.method,
                confidence = excluded.confidence,
                reason     = excluded.reason,
                status     = excluded.status,
                created_at = excluded.created_at,
                decided_at = NULL
            """,
            {
                "mid": message_id,
                "label": label,
                "method": method,
                "conf": confidence,
                "reason": reason,
                "status": status,
                "now": _now_iso(),
            },
        )
        self.conn.commit()

    def list_proposals(self, status: Optional[str] = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM proposals ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_proposals_detailed(self, status: Optional[str] = None) -> list[dict]:
        """Proposals joined with their message subject/sender/snippet (for the UI)."""
        base = (
            "SELECT p.*, m.subject, m.from_addr, m.snippet "
            "FROM proposals p LEFT JOIN messages m ON m.id = p.message_id"
        )
        if status:
            rows = self.conn.execute(
                base + " WHERE p.status = ? ORDER BY p.created_at", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute(base + " ORDER BY p.created_at").fetchall()
        return [dict(r) for r in rows]

    def proposal_counts(self) -> dict:
        """{status: count} across all proposals — handy for progress/summary."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM proposals GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def set_proposal_status(self, message_id: str, status: str) -> None:
        """Move a proposal to a new status and stamp decided_at."""
        self.conn.execute(
            "UPDATE proposals SET status = ?, decided_at = ? WHERE message_id = ?",
            (status, _now_iso(), message_id),
        )
        self.conn.commit()

    def approve_proposal(self, message_id: str) -> None:
        self.set_proposal_status(message_id, "APPROVED")

    def reject_proposal(self, message_id: str) -> None:
        self.set_proposal_status(message_id, "REJECTED")

    def mark_applied(self, message_id: str) -> None:
        self.set_proposal_status(message_id, "APPLIED")

    # --------------------------------------------------------------- audit_log

    def add_audit(
        self,
        message_id: str,
        label: Optional[str],
        action: str,
        outcome: str,
        detail: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_log (message_id, label, action, outcome, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, label, action, outcome, detail, _now_iso()),
        )
        self.conn.commit()

    def add_audit_many(self, rows: list, batch_id: str = None) -> None:
        """Bulk audit insert. Each row: (message_id, label, action, outcome, detail)."""
        if not rows:
            return
        now = _now_iso()
        self.conn.executemany(
            "INSERT INTO audit_log (message_id, label, action, outcome, detail, batch_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(mid, label, action, outcome, detail, batch_id, now)
             for (mid, label, action, outcome, detail) in rows],
        )
        self.conn.commit()

    def last_applied_batch(self) -> Optional[str]:
        """The most recent apply batch_id (for Undo). None if nothing to undo."""
        row = self.conn.execute(
            "SELECT batch_id FROM audit_log WHERE outcome = 'applied' AND batch_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["batch_id"] if row else None

    def audit_for_batch(self, batch_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE batch_id = ? AND outcome = 'applied'",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_audit(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def audit_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    # ------------------------------------------------------- learned rules

    def add_learned_rule(self, domain: str, label: str) -> None:
        if not domain or not label:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO learned_rules (domain, label, created_at) "
            "VALUES (?, ?, ?)",
            (domain.lower(), label, _now_iso()),
        )
        self.conn.commit()

    def get_learned_map(self) -> dict:
        """{domain: [labels]} — a domain with >1 label is multi-purpose."""
        out: dict[str, list] = {}
        for row in self.conn.execute("SELECT domain, label FROM learned_rules"):
            out.setdefault(row["domain"], []).append(row["label"])
        return out

    def learned_label_for(self, domain: str) -> Optional[str]:
        """The learned label for a domain, only if it maps to exactly one."""
        labels = self.get_learned_map().get((domain or "").lower(), [])
        return labels[0] if len(labels) == 1 else None

    def list_learned(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM learned_rules ORDER BY created_at DESC"
        )]

    # ------------------------------------------------- grouping & bulk ops

    def grouped_proposals(self, status: str = "PENDING") -> list[dict]:
        """
        Group proposals by (sender domain, label) for bulk review. Each group:
        domain, label, count, sample subjects, message_ids, method mix,
        confidence range.
        """
        rows = self.list_proposals_detailed(status)
        groups: dict[tuple, dict] = {}
        for r in rows:
            domain = _domain_of(r.get("from_addr") or "")
            key = (domain, r.get("label"))
            g = groups.get(key)
            if g is None:
                g = {
                    "domain": domain,
                    "label": r.get("label"),
                    "count": 0,
                    "message_ids": [],
                    "samples": [],
                    "methods": set(),
                    "confidence_min": 1.0,
                    "confidence_max": 0.0,
                }
                groups[key] = g
            g["count"] += 1
            g["message_ids"].append(r["message_id"])
            if len(g["samples"]) < 4 and r.get("subject"):
                g["samples"].append({"subject": r["subject"], "from": r.get("from_addr")})
            g["methods"].add(r.get("method"))
            conf = r.get("confidence") or 0.0
            g["confidence_min"] = min(g["confidence_min"], conf)
            g["confidence_max"] = max(g["confidence_max"], conf)

        result = []
        for g in groups.values():
            g["methods"] = sorted(x for x in g["methods"] if x)
            result.append(g)
        # Biggest groups first — the highest-leverage decisions.
        result.sort(key=lambda g: g["count"], reverse=True)
        return result

    def drilldown(self, label: str, status: str = "PENDING", top: int = 14) -> dict:
        """Break ONE label's slice down by sub-sender and subject-keyword clusters."""
        from collections import Counter

        rows = [r for r in self.list_proposals_detailed(status) if r.get("label") == label]
        senders = Counter(_domain_of(r.get("from_addr") or "") for r in rows)
        keywords = _subject_keywords([r.get("subject") or "" for r in rows])
        samples = [
            {"subject": r.get("subject"), "from": r.get("from_addr")}
            for r in rows[:6]
        ]
        return {
            "label": label,
            "total": len(rows),
            "senders": [{"domain": d, "count": n} for d, n in senders.most_common(top)],
            "keywords": [{"keyword": k, "count": n} for k, n in keywords[:top]],
            "samples": samples,
        }

    def slice_ids(self, label: str, domain: str = None, keyword: str = None, status: str = "PENDING") -> list:
        """message_ids of a slice: proposals with `label`, optionally a sender domain and/or subject keyword."""
        rows = [r for r in self.list_proposals_detailed(status) if r.get("label") == label]
        if domain:
            rows = [r for r in rows if _domain_of(r.get("from_addr") or "") == domain.lower()]
        if keyword:
            kw = keyword.lower()
            rows = [r for r in rows if kw in (r.get("subject") or "").lower()]
        return [r["message_id"] for r in rows]

    def update_proposals_status(self, message_ids: list, status: str) -> int:
        if not message_ids:
            return 0
        now = _now_iso()
        self.conn.executemany(
            "UPDATE proposals SET status = ?, decided_at = ? WHERE message_id = ?",
            [(status, now, mid) for mid in message_ids],
        )
        self.conn.commit()
        return len(message_ids)

    def relabel_proposals(self, message_ids: list, new_label: str) -> int:
        if not message_ids:
            return 0
        self.conn.executemany(
            "UPDATE proposals SET label = ?, method = 'human' WHERE message_id = ?",
            [(new_label, mid) for mid in message_ids],
        )
        self.conn.commit()
        return len(message_ids)

    def set_proposals_action(self, message_ids: list, action: str) -> int:
        """Set what Apply will do for these proposals: label | archive | trash."""
        if not message_ids:
            return 0
        self.conn.executemany(
            "UPDATE proposals SET action = ? WHERE message_id = ?",
            [(action, mid) for mid in message_ids],
        )
        self.conn.commit()
        return len(message_ids)

    # -------------------------------------------------------------- sync_state

    def get_sync_state(self) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM sync_state WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def set_sync_state(
        self,
        email_address: Optional[str] = None,
        last_history_id: Optional[str] = None,
        mark_full_sync: bool = False,
    ) -> None:
        """Upsert the single sync_state row, preserving fields not passed."""

        current = self.get_sync_state() or {}
        now = _now_iso()

        email_address = email_address or current.get("email_address")
        last_history_id = last_history_id or current.get("last_history_id")
        last_full_sync_at = (
            now if mark_full_sync else current.get("last_full_sync_at")
        )

        self.conn.execute(
            """
            INSERT INTO sync_state (
                id, email_address, last_history_id, last_sync_at, last_full_sync_at
            ) VALUES (1, :email, :hist, :now, :full)
            ON CONFLICT(id) DO UPDATE SET
                email_address     = :email,
                last_history_id   = :hist,
                last_sync_at      = :now,
                last_full_sync_at = :full
            """,
            {
                "email": email_address,
                "hist": last_history_id,
                "now": now,
                "full": last_full_sync_at,
            },
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
