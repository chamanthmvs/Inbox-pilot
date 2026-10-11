from typing import Optional

from pydantic import BaseModel


class Classification(BaseModel):
    """
    The classifier's decision for one message — the 3-state outcome.

    method:
        "rules"        -> matched a deterministic rule (confidence 1.0)
        "llm"          -> the LLM chose a label with enough confidence
        "needs_review" -> neither was confident; a human must decide

    status mirrors how it is stored as a proposal:
        "PENDING"      -> has a label, awaiting human approval
        "NEEDS_REVIEW" -> no confident label; surfaced to the human
    """

    message_id: str
    label: Optional[str] = None
    method: str
    confidence: float = 0.0
    reason: str = ""
    status: str
