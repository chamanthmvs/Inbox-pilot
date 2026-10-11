"""
Central, env-driven configuration — one source of truth shared by the CLIs,
the workflow, and (Stage 4) the API. Nothing here authenticates or touches the
network; it just holds values.
"""

import os
from dataclasses import dataclass


def _as_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    db_path: str = "mailbox.db"
    taxonomy_path: str = "config/taxonomy.yaml"
    classifier_model: str | None = None   # None → LLMClassifier's own default
    use_llm: bool = True                  # self-disables anyway without a key
    dry_run: bool = False                 # True → plan label changes, don't apply
    thread_id: str = "default"            # LangGraph checkpoint thread

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("MAILBOX_DB", "mailbox.db"),
            taxonomy_path=os.environ.get("TAXONOMY_PATH", "config/taxonomy.yaml"),
            classifier_model=os.environ.get("CLASSIFIER_MODEL") or None,
            use_llm=_as_bool(os.environ.get("USE_LLM"), True),
            dry_run=_as_bool(os.environ.get("DRY_RUN"), False),
            thread_id=os.environ.get("WORKFLOW_THREAD_ID", "default"),
        )
