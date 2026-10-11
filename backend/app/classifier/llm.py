"""
LLM classifier (Claude) — runs only on what the rules didn't catch.

Kept LIGHT on the LLM:
  * BATCHED — up to `chunk_size` emails per API call (one call for ~20 emails,
    not one call each). This is the main cost lever.
  * cheap model by default (claude-haiku-5-5); override with CLASSIFIER_MODEL.
  * the stable label menu is prompt-cached.
  * low effort, small output.

Graceful: if `anthropic` or ANTHROPIC_API_KEY is missing, `enabled` is False and
the caller routes everything to NEEDS_REVIEW. Bounded: the model must pick a name
from the taxonomy menu or answer "UNKNOWN"; anything else → not confident.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None


DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_CHUNK = 20


@dataclass
class LLMMatch:
    label: str
    confidence: float
    reason: str


def _first_json(text: str, opener: str, closer: str):
    start = (text or "").find(opener)
    end = (text or "").rfind(closer)
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


class LLMClassifier:
    def __init__(self, taxonomy, model=None, min_confidence=0.6, api_key=None, chunk_size=DEFAULT_CHUNK):
        self.taxonomy = taxonomy
        self.model = model or os.environ.get("CLASSIFIER_MODEL") or DEFAULT_MODEL
        self.min_confidence = min_confidence
        self.chunk_size = chunk_size
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    @property
    def enabled(self) -> bool:
        return anthropic is not None and bool(self._api_key)

    def _client_or_none(self):
        if self._client is None and self.enabled:
            self._client = anthropic.Anthropic()
        return self._client

    def _menu_text(self) -> str:
        return "\n".join(f"- {name}: {desc}" for name, desc in self.taxonomy.menu())

    def _validate(self, label, confidence) -> bool:
        return bool(
            label
            and label != "UNKNOWN"
            and self.taxonomy.is_valid(label)
            and confidence >= self.min_confidence
        )

    # -- single (used by the orchestrator / tests) ----------------------------

    def classify(self, meta) -> Optional[LLMMatch]:
        result = self.classify_batch([meta])
        return result.get(meta.id)

    # -- batch (used by the runner — the cheap path) --------------------------

    def classify_batch(self, metas) -> dict:
        """Return {message_id: LLMMatch | None} for the given messages."""
        out = {m.id: None for m in metas}
        client = self._client_or_none()
        if client is None or not metas:
            return out

        for i in range(0, len(metas), self.chunk_size):
            chunk = metas[i : i + self.chunk_size]
            out.update(self._classify_chunk(client, chunk))
        return out

    def _classify_chunk(self, client, chunk) -> dict:
        out = {m.id: None for m in chunk}

        system = (
            "You label emails for an inbox-organizing tool. For EACH email choose "
            "the single best label from the fixed MENU below.\n\n"
            "Guidelines:\n"
            "- Prefer the MOST SPECIFIC label that clearly fits (a sub-label over "
            "its parent). If only the broad category fits, use the category.\n"
            "- A genuine one-to-one message from a real PERSON (a human sender, not "
            "a no-reply / marketing / notification address) is 'Personal' — even "
            "from a work domain (e.g. a colleague at a company).\n"
            "- Automated mail (no-reply, alerts, receipts, notifications) belongs to "
            "the topic it is about (bank alert, order, statement, etc.), NOT Personal.\n"
            "- The sender's domain is a strong hint; use the subject to disambiguate.\n"
            "- If you are not reasonably sure, answer \"UNKNOWN\" rather than guessing.\n\n"
            "Examples:\n"
            '  from="Ravi Kumar <ravi@gmail.com>" subject="catching up this weekend?" -> Personal\n'
            '  from="Swapnil <swapnil@cognizant.com>" subject="lunch tomorrow?" -> Personal\n'
            '  from="HDFC Bank <alerts@hdfcbank.net>" subject="Debit alert on your card" -> Bank/Alerts & Notifications\n'
            '  from="Flipkart <noreply@flipkart.com>" subject="Your order has shipped" -> Shopping/Orders & Delivery\n'
            '  from="Zerodha <noreply@zerodha.net>" subject="Redemption report" -> Finance/Investments & Insurance\n'
            '  from="Myntra <offers@myntra.com>" subject="Flat 60% off this weekend" -> Promotions\n\n'
            f"MENU (use exactly one name from this list, or UNKNOWN):\n{self._menu_text()}"
        )
        lines = [
            f'[{idx}] from="{m.from_addr}" subject="{m.subject}" '
            f'preview="{(m.snippet or "")[:160]}"'
            for idx, m in enumerate(chunk)
        ]
        user = (
            "Classify each email below. Respond with ONLY a JSON array, one object "
            'per email: [{"index": <n>, "label": "<name or UNKNOWN>", '
            '"confidence": <0..1>, "reason": "<short>"}].\n\n' + "\n".join(lines)
        )

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=2048,
                output_config={"effort": "low"},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
        except Exception:
            return out  # all None → caller routes to NEEDS_REVIEW

        if getattr(response, "stop_reason", None) == "refusal":
            return out

        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        )
        data = _first_json(text, "[", "]")
        if not isinstance(data, list):
            return out

        for item in data:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(chunk):
                continue
            label = item.get("label")
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            if self._validate(label, confidence):
                out[chunk[idx].id] = LLMMatch(
                    label=label,
                    confidence=confidence,
                    reason=f"llm: {item.get('reason', '')}",
                )
        return out
