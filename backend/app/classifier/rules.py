"""
Rules classifier — deterministic, free, explainable, runs first.

Two correctness rules learned the hard way:

  1. Match WHOLE WORDS, not substrings. ("irs" must not match "idfcfirst",
     "fee" must not match "Feeding", "report" must not match inside other words.)
  2. The sender DOMAIN is the strongest signal, then a sender substring, then a
     subject word. And a more specific (sub-)label beats its parent.

So each candidate match gets a key `(depth, type_weight)` and the highest key
wins (ties → first in taxonomy order).
"""

import re
from dataclasses import dataclass
from typing import Optional

# Strength of each kind of match (higher = stronger signal).
TYPE_WEIGHT = {"from_domains": 3, "from_contains": 2, "subject_contains": 1}


@dataclass
class RuleMatch:
    label: str
    reason: str
    score: tuple = (0, 0)  # (depth, type_weight) — internal ranking


def _domain_of(sender: str) -> str:
    """Extract the domain from 'Name <user@host.com>' or 'user@host.com'."""
    match = re.search(r"[\w.\-+]+@([\w.\-]+)", sender or "")
    return match.group(1).lower() if match else ""


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _has_word(needle: str, haystack: str) -> bool:
    """
    Whole-token, case-insensitive match. `needle` may be a multi-word phrase.

    The boundary is applied only on the token's WORD-character edges: "irs" won't
    match inside "idfcfirst", but "% off" still matches "50% off" (the leading
    '%' is not a word char, so no left boundary is required).
    """
    n = (needle or "").lower().strip()
    if not n:
        return False
    left = r"(?<!\w)" if _is_word_char(n[0]) else ""
    right = r"(?!\w)" if _is_word_char(n[-1]) else ""
    return re.search(left + re.escape(n) + right, (haystack or "").lower()) is not None


def _domain_match(rules: dict, domain: str):
    """Reason string if the sender's domain matches a from_domains entry, else None."""
    for dom in rules.get("from_domains", []) or []:
        d = dom.lower()
        if domain == d or domain.endswith("." + d):
            return f"sender domain is '{dom}'"
    return None


def _content_match(rules: dict, sender: str, subject: str):
    """(weight, reason) for the best sender-substring / subject-word match, else None."""
    for needle in rules.get("from_contains", []) or []:
        if _has_word(needle, sender):
            return (TYPE_WEIGHT["from_contains"], f"sender mentions '{needle}'")
    for needle in rules.get("subject_contains", []) or []:
        if _has_word(needle, subject):
            return (TYPE_WEIGHT["subject_contains"], f"subject mentions '{needle}'")
    return None


# Senders that are clearly automated/bulk, not a person.
_BULK_MARKERS = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "notification",
    "notifications", "alert", "alerts", "mailer", "mailchimp", "newsletter",
    "updates", "update", "marketing", "promo", "promotions", "offers", "news",
    "bounce", "support", "info", "team", "hello", "contact", "service", "account",
)
# Common personal mailbox providers.
_PERSONAL_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "ymail.com",
    "outlook.com", "hotmail.com", "live.com", "icloud.com", "me.com",
    "proton.me", "protonmail.com",
}


def _looks_personal(sender: str, domain: str) -> bool:
    """A human 1:1 sender: a personal provider and no bulk/no-reply markers."""
    local = (sender.split("@")[0] if "@" in sender else sender).lower()
    if any(marker in local for marker in _BULK_MARKERS):
        return False
    return domain in _PERSONAL_PROVIDERS


class RulesClassifier:
    def __init__(self, taxonomy):
        self.taxonomy = taxonomy

    def classify(self, meta) -> Optional[RuleMatch]:
        sender = meta.from_addr or ""
        subject = meta.subject or ""
        domain = _domain_of(sender)

        domain_hits = []   # (label, reason)
        content_hits = []  # (label, reason, weight)

        for label in self.taxonomy.iter_labels():
            if not label.rules:
                continue
            dmatch = _domain_match(label.rules, domain)
            if dmatch:
                domain_hits.append((label, dmatch))
            cmatch = _content_match(label.rules, sender, subject)
            if cmatch:
                content_hits.append((label, cmatch[1], cmatch[0]))

        # Sender-domain is authoritative ACROSS categories: a domain match wins
        # over any subject/keyword match in a different family. But a more specific
        # sub-label of the SAME matched domain family still wins (e.g. a bank domain
        # → Bank, but its "debit alert" subject → Bank/Alerts & Notifications).
        if domain_hits:
            dom_label, dom_reason = max(domain_hits, key=lambda h: h[0].depth)
            prefix = dom_label.name + "/"
            descendants = [
                h for h in content_hits
                if h[0].name.startswith(prefix) and h[0].depth > dom_label.depth
            ]
            if descendants:
                best = max(descendants, key=lambda h: (h[0].depth, h[2]))
                return RuleMatch(best[0].name, f"rule: {best[1]}", (best[0].depth, best[2]))
            return RuleMatch(dom_label.name, f"rule: {dom_reason}", (dom_label.depth, 3))

        if content_hits:
            best = max(content_hits, key=lambda h: (h[0].depth, h[2]))
            return RuleMatch(best[0].name, f"rule: {best[1]}", (best[0].depth, best[2]))

        # Lowest-priority fallback: a human 1:1 sender → Personal.
        if self.taxonomy.is_valid("Personal") and _looks_personal(sender, domain):
            return RuleMatch(label="Personal", reason="rule: personal sender", score=(0, 0))

        return None
