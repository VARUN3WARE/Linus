"""bounty.yml: the maintainer's rules for one pool.

A repo keeps its rules in `.github/bounty.yml` on its default branch, where
contributors can read them; Linus reads that file at every close. Without it,
Linus uses the copy `linus init` wrote for the repo, then the bounty.yml next
to Linus. Whichever was used, its SHA-256 goes into every receipt.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_RULES = ".github/bounty.yml"
LEVELS = {"difficulty": 4, "impact": 4, "docs": 3}  # entries each scoring list must have


class PolicyError(ValueError):
    """bounty.yml is missing something or has a value Linus can't use."""


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
    origin: str = ""  # where these rules were read from, for people

    @property
    def primary_currency(self) -> str:
        return next(iter(self.budget))

    def excluded(self, author: str) -> str | None:
        if author in self.excluded_authors:
            return "author is excluded by bounty.yml"
        if self.exclude_bots and (author.endswith("[bot]") or author.endswith("-bot")):
            return "bots are excluded by bounty.yml"
        return None


def _number(value, where: str, low: float = 0, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{where} must be a number, got {value!r}")
    if value < low or (high is not None and value > high):
        bounds = f"between {low:g} and {high:g}" if high is not None else f"at least {low:g}"
        raise PolicyError(f"{where} must be {bounds}, got {value!r}")
    return float(value)


def _section(data: dict, key: str) -> dict:
    value = data.get(key)
    if not isinstance(value, dict):
        raise PolicyError(f"`{key}:` section is missing")
    return value


def parse(raw: bytes, origin: str = "") -> Policy:
    """Rules from bounty.yml bytes, with a plain-English error for anything wrong."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("expected sections like `budget:` and `scoring:`")
    budget = _section(data, "budget")
    if not budget:
        raise PolicyError("`budget:` needs at least one currency, e.g. `USD: 500`")
    scoring, caps = _section(data, "scoring"), _section(data, "caps")
    eligible = data.get("eligible") or {}
    exclude = data.get("exclude") or {}
    lists = {}
    for key, n in LEVELS.items():
        values = scoring.get(key)
        if not isinstance(values, list) or len(values) != n:
            raise PolicyError(f"scoring.{key} must list {n} numbers, got {values!r}")
        lists[key] = tuple(_number(v, f"scoring.{key}") for v in values)
    labels = eligible.get("issue_labels", ["bounty"])
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) and x for x in labels):
        raise PolicyError(f"eligible.issue_labels must list label names, got {labels!r}")
    authors = exclude.get("authors") or []
    if not isinstance(authors, list):
        raise PolicyError(f"exclude.authors must be a list of GitHub usernames, got {authors!r}")
    return Policy(
        budget={str(k): _number(v, f"budget.{k}") for k, v in budget.items()},
        issue_labels=tuple(labels),
        include_docs=bool(eligible.get("include_docs", True)),
        base_points=_number(scoring.get("base"), "scoring.base"),
        difficulty_points=lists["difficulty"],
        impact_multiplier=lists["impact"],
        docs_points=lists["docs"],
        cap_fraction=_number(caps.get("per_contributor_pct"), "caps.per_contributor_pct", 1, 100) / 100,
        min_payout=_number(caps.get("min_payout", 0), "caps.min_payout"),
        duplicate_similarity=_number((data.get("anti_gaming") or {}).get("duplicate_similarity", 0.8),
                                     "anti_gaming.duplicate_similarity", 0, 1),
        exclude_bots=bool(exclude.get("bots", True)),
        excluded_authors=frozenset(str(a) for a in authors),
        sha256=hashlib.sha256(raw).hexdigest(),
        origin=origin,
    )


def load(path: Path) -> Policy:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise PolicyError(f"{path} not found") from exc
    try:
        return parse(raw, str(path))
    except PolicyError as exc:
        raise PolicyError(f"{path}: {exc}") from exc


def starter(budget: float, currency: str = "USD", label: str = "bounty") -> str:
    """A commented bounty.yml for a new repo."""
    amount = int(budget) if float(budget).is_integer() else budget
    return STARTER.format(currency=currency, amount=amount, label=label)


STARTER = """\
# Bounty rules for this repo, read by Linus at every month-end close.
# Contributors can read them here. Linus never moves money: it proposes
# payouts, you approve them, and you pay through your usual channel.

budget:                     # this month's pool; the first currency is the primary one
  {currency}: {amount}

eligible:
  issue_labels: [{label}]   # a code PR earns only if it fixes an issue with one of these labels
  include_docs: true        # docs-only PRs can earn the (small) docs points

scoring:
  # code PR points = (base + difficulty) x impact; the pool is split by points
  base: 20
  difficulty: [0, 10, 25, 50]     # trivial, small, moderate, hard
  impact: [0.5, 1.0, 1.5, 2.0]    # cosmetic, minor, moderate, severe
  docs: [0, 5, 15]                # no real change, small correction, substantial

caps:
  per_contributor_pct: 30   # nobody gets more than this share; the rest rolls over to next month
  min_payout: 10            # shares below this (primary currency) are dropped

anti_gaming:
  duplicate_similarity: 0.8   # a PR this similar to an earlier one is a copy

exclude:
  bots: true
  authors: []               # e.g. your own team: [alice, bob]
"""
