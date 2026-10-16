"""
Load the label taxonomy from config/taxonomy.yaml into a usable structure.

The taxonomy is DATA, not code (see the YAML file). This module just reads it
and flattens the category/sub-label tree into a list of `LabelDef`s, each
tagged with its `depth` (0 = top-level category, 1 = sub-label) so the rules
classifier can prefer the most specific match.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LabelDef:
    name: str
    description: str
    keep: bool = True
    rules: dict = field(default_factory=dict)
    depth: int = 0  # 0 = top-level category, 1 = sub-label


class Taxonomy:
    def __init__(self, labels: list[LabelDef], needs_review_label: str):
        self.labels = labels
        self.needs_review_label = needs_review_label
        self._by_name = {label.name: label for label in labels}

    @classmethod
    def load(cls, path: str = "config/taxonomy.yaml") -> "Taxonomy":
        data = yaml.safe_load(Path(path).read_text())

        labels: list[LabelDef] = []
        for category in data.get("categories", []):
            labels.append(
                LabelDef(
                    name=category["name"],
                    description=category.get("description", ""),
                    keep=category.get("keep", True),
                    rules=category.get("rules") or {},
                    depth=0,
                )
            )
            for child in category.get("children") or []:
                labels.append(
                    LabelDef(
                        name=child["name"],
                        description=child.get("description", ""),
                        keep=child.get("keep", True),
                        rules=child.get("rules") or {},
                        depth=1,
                    )
                )

        # Merge AI/user-grown sub-labels from a sibling taxonomy.local.yaml, so the
        # hand-maintained taxonomy.yaml (with its comments) is never rewritten.
        local_path = Path(path).with_name("taxonomy.local.yaml")
        if local_path.exists():
            existing = {label.name for label in labels}
            local = yaml.safe_load(local_path.read_text()) or {}
            for entry in local.get("labels", []) or []:
                name = entry.get("name")
                if not name or name in existing:
                    continue
                labels.append(
                    LabelDef(
                        name=name,
                        description=entry.get("description", ""),
                        keep=entry.get("keep", True),
                        rules=entry.get("rules") or {},
                        depth=name.count("/"),
                    )
                )
                existing.add(name)

        return cls(labels, data.get("needs_review_label", "Needs Review"))

    @staticmethod
    def add_local_labels(entries, path: str = "config/taxonomy.yaml") -> None:
        """
        Append sub-labels to config/taxonomy.local.yaml (created if needed).
        `entries`: list of {name, description?, keep?}. Idempotent by name.
        """
        local_path = Path(path).with_name("taxonomy.local.yaml")
        data = {}
        if local_path.exists():
            data = yaml.safe_load(local_path.read_text()) or {}
        labels = data.get("labels") or []
        have = {l.get("name") for l in labels}
        for e in entries:
            if e.get("name") and e["name"] not in have:
                labels.append({
                    "name": e["name"],
                    "description": e.get("description", ""),
                    "keep": e.get("keep", True),
                })
                have.add(e["name"])
        data["labels"] = labels
        local_path.write_text(
            "# AI/user-grown sub-labels, merged into the taxonomy at load time.\n"
            "# Safe to edit or delete. The main taxonomy.yaml is never rewritten.\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        )

    def names(self) -> list[str]:
        return [label.name for label in self.labels]

    def is_valid(self, name: str) -> bool:
        return name in self._by_name

    def get(self, name: str):
        return self._by_name.get(name)

    def menu(self) -> list[tuple[str, str]]:
        """(name, description) pairs — the fixed menu the LLM must choose from."""
        return [(label.name, label.description) for label in self.labels]

    def iter_labels(self) -> list[LabelDef]:
        return list(self.labels)
