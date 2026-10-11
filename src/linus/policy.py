"""bounty.yml: the maintainer's rules for one pool."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Policy:
    budget: dict[str, float]
    issue_labels: tuple[str, ...]
    include_docs: bool
    base_points: float
    difficulty_points: tuple[float, ...]
    impact_multiplier: tuple[float, ...]
    docs_points: tuple[float, ...]
    cap_fraction: float
    min_payout: float
    duplicate_similarity: float
    exclude_bots: bool
    excluded_authors: frozenset[str]
    sha256: str

    @property
    def primary_currency(self) -> str:
        return next(iter(self.budget))

    def excluded(self, author: str) -> str | None:
        if author in self.excluded_authors:
            return "author is excluded by bounty.yml"
        if self.exclude_bots and (author.endswith("[bot]") or author.endswith("-bot")):
            return "bots are excluded by bounty.yml"
        return None


def load(path: Path) -> Policy:
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    scoring, caps = data["scoring"], data["caps"]
    exclude = data.get("exclude", {})
    return Policy(
        budget={str(k): float(v) for k, v in data["budget"].items()},
        issue_labels=tuple(data["eligible"].get("issue_labels", ["bounty"])),
        include_docs=bool(data["eligible"].get("include_docs", True)),
        base_points=float(scoring["base"]),
        difficulty_points=tuple(float(x) for x in scoring["difficulty"]),
        impact_multiplier=tuple(float(x) for x in scoring["impact"]),
        docs_points=tuple(float(x) for x in scoring["docs"]),
        cap_fraction=float(caps["per_contributor_pct"]) / 100,
        min_payout=float(caps["min_payout"]),
        duplicate_similarity=float(data.get("anti_gaming", {}).get("duplicate_similarity", 0.8)),
        exclude_bots=bool(exclude.get("bots", True)),
        excluded_authors=frozenset(exclude.get("authors") or []),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
