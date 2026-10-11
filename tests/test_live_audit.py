"""Live regression test for the v2 failure: the generated oracle case that
rejected a correct PR must be dropped by JEV's audit. Needs JEV_API_KEY."""

import pytest

from linus import demo
from linus.config import load_settings
from linus.jev import Jev
from linus.oracle import audit
from linus.source import Issue

settings = load_settings()
pytestmark = pytest.mark.skipif(not settings.jev_api_key, reason="needs JEV_API_KEY")


def test_audit_drops_the_case_that_contradicts_the_issue():
    raw = next(i for i in demo.ISSUES if i["number"] == 2)
    issue = Issue(2, raw["title"], raw["body"])
    cases = [
        {"call": 'slugify("Hello, World!")', "expected": "'hello-world'", "raises": None},
        {"call": 'slugify("Café & Tea / Coffee")', "expected": "'caf--tea-coffee'", "raises": None},  # v2's bad case
        {"call": 'slugify("Café & Tea / Coffee")', "expected": "'caf-tea-coffee'", "raises": None},
    ]
    keep, bad, good = audit(Jev(settings), issue, cases)
    assert keep >= 0.5 and good >= 0.5
    assert bad < 0.5
