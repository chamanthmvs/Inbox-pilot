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

        return cls(labels, data.get("needs_review_label", "Needs Review"))

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
