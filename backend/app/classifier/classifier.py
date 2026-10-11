"""
The 3-state orchestrator: rules → LLM → NEEDS_REVIEW.

    1. Rules match?                → PENDING (method=rules, confidence 1.0)
    2. else LLM confident?         → PENDING (method=llm, its confidence)
    3. else                        → NEEDS_REVIEW (never guess)

Returns a Classification; the runner persists it as a proposal.
"""

from app.models.classification import Classification


class Classifier:
    def __init__(self, taxonomy, rules, llm=None):
        self.taxonomy = taxonomy
        self.rules = rules
        self.llm = llm

    def classify(self, meta) -> Classification:
        rule_match = self.rules.classify(meta)
        if rule_match is not None:
            return Classification(
                message_id=meta.id,
                label=rule_match.label,
                method="rules",
                confidence=1.0,
                reason=rule_match.reason,
                status="PENDING",
            )

        if self.llm is not None and self.llm.enabled:
            llm_match = self.llm.classify(meta)
            if llm_match is not None:
                return Classification(
                    message_id=meta.id,
                    label=llm_match.label,
                    method="llm",
                    confidence=llm_match.confidence,
                    reason=llm_match.reason,
                    status="PENDING",
                )
            reason = "rules found no match and the LLM was not confident"
        else:
            reason = (
                "rules found no match and the LLM is disabled "
                "(no ANTHROPIC_API_KEY)"
            )

        return Classification(
            message_id=meta.id,
            label=None,
            method="needs_review",
            confidence=0.0,
            reason=reason,
            status="NEEDS_REVIEW",
        )
