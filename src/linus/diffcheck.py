"""Deterministic signals from a PR's diff. No model involved: anything code can
compute exactly (counts, deleted tests, skip markers) is computed here.
"""

import re
from dataclasses import dataclass, field

from linus.source import FileChange

TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")
DOC_PATH = re.compile(r"(\.(md|rst|txt)$)|(^|/)docs?/")
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)")
SKIP_MARKER = re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|unittest\.skip|@skip\b")
INJECTION = re.compile(
    r"(?i)(ignore (all |any )?(previous|prior|above) instructions"
    r"|(ai|automated|llm|bot)\s+(agents?|reviewers?)"
    r"|pre-?approved"
    r"|mark (this|it)( pull request| pr)? as (verified|approved)"
    r"|treat (it|this)( pr| pull request)? as (fully )?verified"
    r"|skip running the tests)"
)


@dataclass
class DiffSignals:
    code_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    doc_files: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    code_lines_changed: int = 0
    removed_tests: list[str] = field(default_factory=list)
    added_skips: int = 0
    injection_hits: list[str] = field(default_factory=list)

    @property
    def docs_only(self) -> bool:
        return bool(self.doc_files) and not (self.code_files or self.test_files or self.other_files)

    @property
    def touches_tests(self) -> bool:
        return bool(self.test_files)

    @property
    def tampering(self) -> bool:
        return bool(self.removed_tests) or self.added_skips > 0


def _kind(path: str) -> str:
    if TEST_PATH.search(path):
        return "test"
    if DOC_PATH.search(path):
        return "doc"
    if path.endswith(".py"):
        return "code"
    return "other"


def _snippets(text: str, limit: int = 3) -> list[str]:
    hits = []
    for m in INJECTION.finditer(text):
        start, end = max(0, m.start() - 40), min(len(text), m.end() + 40)
        hits.append(" ".join(text[start:end].split()))
        if len(hits) >= limit:
            break
    return hits


def analyze(diff: str, changes: list[FileChange], pr_text: str) -> DiffSignals:
    signals = DiffSignals()
    buckets = {"code": signals.code_files, "test": signals.test_files,
               "doc": signals.doc_files, "other": signals.other_files}
    for change in changes:
        kind = _kind(change.path)
        buckets[kind].append(change.path)
        if kind == "code":
            signals.code_lines_changed += change.added + change.removed

    removed, added, added_text = set(), set(), []
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
            continue
        if line.startswith("--- "):
            if not current and line.startswith("--- a/"):
                current = line[6:]
            continue
        if line.startswith("diff --git"):
            current = ""
            continue
        is_test = bool(current) and _kind(current) == "test"
        if line.startswith("+"):
            added_text.append(line[1:])
            if is_test:
                if m := TEST_DEF.match(line[1:]):
                    added.add(m.group(1))
                if SKIP_MARKER.search(line):
                    signals.added_skips += 1
        elif line.startswith("-") and is_test:
            if m := TEST_DEF.match(line[1:]):
                removed.add(m.group(1))

    signals.removed_tests = sorted(removed - added)
    signals.injection_hits = _snippets(pr_text + "\n" + "\n".join(added_text))
    return signals


def change_fingerprint(diff: str) -> set[str]:
    """Normalized changed lines, for spotting copied PRs."""
    lines = set()
    for line in diff.splitlines():
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        body = " ".join(line[1:].split())
        if body:
            lines.add(line[0] + body)
    return lines


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
