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
# Files that change how pytest or the interpreter starts, wherever they live.
HARNESS_FILE = re.compile(r"(^|/)(conftest\.py|pytest\.ini|tox\.ini|sitecustomize\.py|usercustomize\.py"
                          r"|[^/]*\.pth)$")
HARNESS_SECTION = {"setup.cfg": re.compile(r"^\s*\[tool:pytest\]"),
                   "pyproject.toml": re.compile(r"^\s*\[tool\.pytest")}
SECTION = re.compile(r"^\s*\[")
PYTEST_KEYS = re.compile(r"^\s*(addopts|pythonpath|testpaths|python_files|python_classes|python_functions"
                         r"|required_plugins|norecursedirs|confcutdir|usefixtures|xfail_strict)\s*=")
# Added lines that change a test outcome instead of the code under test.
OUTCOME_HACKS = {
    "assigns an exit status": re.compile(r"\bexitstatus\s*=(?!=)|\bexitcode\s*=(?!=)"),
    "adds a pytest_sessionfinish hook": re.compile(r"def\s+pytest_sessionfinish\b"),
    "adds a pytest_runtest_* hook": re.compile(r"def\s+pytest_runtest_\w+"),
    "adds a pytest_report_teststatus hook": re.compile(r"def\s+pytest_report_teststatus\b"),
    "calls os._exit": re.compile(r"\bos\._exit\s*\("),
    "touches Linus's sandbox paths": re.compile(r"linus-out|linus-oracle|junitxml"),
}
LIBRARY_EXIT = re.compile(r"\bsys\.exit\s*\(|\braise\s+SystemExit\b")
EQ_DEF = re.compile(r"^\s*def\s+__eq__\s*\(")
EQ_LAMBDA = re.compile(r"__eq__\s*=\s*lambda\b[^:]*:\s*True\b")
RETURNS_TRUE = re.compile(r"^\s*return\s+True\s*(#.*)?$")


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
    harness_files: list[str] = field(default_factory=list)
    outcome_hacks: list[str] = field(default_factory=list)

    @property
    def docs_only(self) -> bool:
        return bool(self.doc_files) and not (self.code_files or self.test_files or self.other_files)

    @property
    def touches_tests(self) -> bool:
        return bool(self.test_files)

    @property
    def touches_test_harness(self) -> bool:
        """Edits pytest config, conftest or interpreter start-up: it can change
        how the tests run, so a green run alone doesn't prove the fix."""
        return bool(self.harness_files)

    @property
    def manipulates_outcome(self) -> bool:
        return bool(self.outcome_hacks)

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


def _outcome_hacks(path: str, line: str) -> list[str]:
    """What an added line does to test outcomes, if anything."""
    if not path.endswith(".py"):
        return []
    hits = [label for label, pattern in OUTCOME_HACKS.items() if pattern.search(line)]
    library = _kind(path) == "code" and not path.endswith("__main__.py")
    if library and LIBRARY_EXIT.search(line):
        hits.append("exits the interpreter from library code")
    if EQ_LAMBDA.search(line) or (EQ_DEF.match(line) and re.search(r":\s*return\s+True\s*$", line)):
        hits.append("adds an __eq__ that always returns True")
    return hits


class _Scan:
    """Per-file state while walking a diff: which file, which config section,
    and whether the last definition seen was an __eq__ awaiting its body."""

    def __init__(self, path: str = "") -> None:
        self.path, self.section, self.eq_open = path, "", False

    def harness_line(self, line: str, changed: bool) -> bool:
        name = self.path.rsplit("/", 1)[-1]
        section = HARNESS_SECTION.get(name)
        if section is None:
            return False
        if SECTION.match(line):
            self.section = line.strip()
        under = bool(self.section) and bool(section.match(self.section))
        return changed and (under or bool(section.match(line)) or bool(PYTEST_KEYS.match(line)))

    def eq_always_true(self, line: str, added: bool) -> bool:
        body = line.strip()
        if EQ_DEF.match(line):
            self.eq_open = True
            return False
        if not self.eq_open or not body or body.startswith(("#", '"""', "'''")):
            return False
        self.eq_open = False
        return added and bool(RETURNS_TRUE.match(line))


def analyze(diff: str, changes: list[FileChange], pr_text: str) -> DiffSignals:
    signals = DiffSignals()
    buckets = {"code": signals.code_files, "test": signals.test_files,
               "doc": signals.doc_files, "other": signals.other_files}
    harness = []
    for change in changes:
        kind = _kind(change.path)
        buckets[kind].append(change.path)
        if kind == "code":
            signals.code_lines_changed += change.added + change.removed
        if HARNESS_FILE.search(change.path):
            harness.append(change.path)

    removed, added, added_text, hacks = set(), set(), [], []
    scan = _Scan()
    for line in diff.splitlines():
        if line.startswith("+++ "):
            # A deleted file has "+++ /dev/null": keep the path from its "--- a/" line.
            if line.startswith("+++ b/"):
                scan = _Scan(line[6:])
            continue
        if line.startswith("--- "):
            if not scan.path and line.startswith("--- a/"):
                scan = _Scan(line[6:])
            continue
        if line.startswith("diff --git"):
            scan = _Scan()
            continue
        if line.startswith("@@"):
            scan.section, scan.eq_open = "", False
            continue
        current = scan.path
        is_test = bool(current) and _kind(current) == "test"
        body = line[1:]
        if line.startswith(("+", "-", " ")) and scan.harness_line(body, line[0] != " "):
            harness.append(current)
        if line.startswith(("+", " ")) and scan.eq_always_true(body, line[0] == "+"):
            hacks.append(f"{current}: adds an __eq__ that always returns True")
        if line.startswith("+"):
            added_text.append(body)
            hacks += [f"{current}: {label}" for label in _outcome_hacks(current, body)]
            if is_test:
                if m := TEST_DEF.match(body):
                    added.add(m.group(1))
                if SKIP_MARKER.search(line):
                    signals.added_skips += 1
        elif line.startswith("-") and is_test:
            if m := TEST_DEF.match(body):
                removed.add(m.group(1))

    signals.removed_tests = sorted(removed - added)
    signals.injection_hits = _snippets(pr_text + "\n" + "\n".join(added_text))
    signals.harness_files = list(dict.fromkeys(harness))
    signals.outcome_hacks = list(dict.fromkeys(hacks))
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
