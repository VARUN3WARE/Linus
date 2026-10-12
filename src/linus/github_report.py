"""Reports a finished close back to GitHub (`linus close --github ... --comment`):
one verdict comment and one `linus: pay|reject|flag` label per PR, and one
"Bounty close <period>" issue with the leaderboard.

This runs after the close has settled, never inside the per-PR loop, and a
GitHub error on one PR is logged and skipped: an outage can delay the report
but can't change or break a close. Comments and the issue are found again by
a hidden marker, and only if the authenticated user wrote them, so re-running
updates them instead of piling up new ones.
"""

import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from linus import ledger
from linus.economics import pr_cost_usd
from linus.gates import ADDRESSES
from linus.github import VERDICT_LABELS, GitHub, GitHubError

VERDICT_MARKER = "<!-- linus:verdict -->"
CLOSE_MARKER = "<!-- linus:close -->"
WORD = {"pay": "PAY", "reject": "REJECT", "flag": "FLAG for a human"}


def _money(amount: float, currency: str) -> str:
    return f"${amount:,.2f}" if currency == "USD" else f"{amount:,.2f} {currency}"


def _status_line(summary: dict) -> str:
    if summary["status"] == "halted":
        return "Halted: the payout invariants failed, so nothing will be paid from this run."
    return "Proposed: pending maintainer approval."


def _run(receipt: dict, suffix: str) -> dict | None:
    runs = (receipt.get("verification") or {}).get("runs") or []
    return next((r for r in runs if r.get("label", "").endswith(suffix)), None)


def evidence(receipt: dict) -> list[str]:
    """Plain bullet points of what the verdict rests on."""
    lines = []
    oracle = receipt.get("oracle")
    if oracle:
        cases = len(oracle.get("cases") or [])
        attempts = oracle.get("attempts") if isinstance(oracle.get("attempts"), list) else []
        base = next((a for a in reversed(attempts) if a.get("base_outcome")), None)
        outcome = {"fail": "failed", "pass": "passed", "error": "errored"}.get(base and base["base_outcome"], "")
        on_base = f"{outcome} ({base.get('base_failed')} of {cases} cases failing)" if base else "was not recorded"
        lines.append(f"Reproduction test for #{oracle['issue']} ({cases} cases, sha256 "
                     f"`{(oracle.get('test_sha256') or '')[:12]}`): on the base code it {on_base}.")
        if repro := _run(receipt, "_oracle"):
            lines.append(f"On this PR: {repro['passed']} passed, {repro['failed']} failed.")
    if suite := _run(receipt, "_suite"):
        lines.append(f"Test suite on this PR, including the base branch's tests: {suite['outcome']} "
                     f"({suite['passed']} passed, {suite['failed']} failed).")
    if not receipt.get("verification"):
        lines.append("Decided before the sandbox; no tests were run.")
    answers = (receipt.get("gate") or {}).get("answers") or {}
    signals = []
    for key, label in (("manipulation", "addresses an AI reviewer"), ("claims_match", "PR claims match the diff"),
                       ("special_casing", "hard-codes literal inputs")):
        if key in answers:
            signals.append(f"{label} p={answers[key]['noul']:.2f}")
    if addresses := answers.get("addresses_issue"):
        probs = {int(k): v for k, v in addresses["probabilities"].items()}
        level = max(probs, key=probs.get)
        signals.append(f"fixes the issue: {ADDRESSES[level].split(':')[0].lower()} (p={probs[level]:.2f})")
    if signals:
        lines.append("JEV signals: " + "; ".join(signals) + ".")
    s = receipt.get("signals") or {}
    if s.get("removed_tests"):
        lines.append(f"Deletes tests: {', '.join(s['removed_tests'])}.")
    if s.get("added_skips"):
        lines.append(f"Adds {s['added_skips']} skip marker(s).")
    if s.get("harness_files"):
        lines.append(f"Edits the test harness: {', '.join(s['harness_files'])}.")
    if s.get("outcome_hacks"):
        lines.append(f"Changes test outcomes: {'; '.join(s['outcome_hacks'])}.")
    if ag := receipt.get("anti_gaming"):
        lines.append(f"Anti-gaming: {ag['similarity']:.0%} of changed lines match #{ag['winner']}.")
    return lines


def verdict_comment(receipt: dict, summary: dict) -> str:
    verdict = receipt.get("verdict") or "flag"
    lines = [VERDICT_MARKER, f"**Linus verdict: {WORD.get(verdict, verdict)}**", "", receipt.get("reason") or "",
             "", "Evidence:", ""]
    lines += [f"- {line}" for line in evidence(receipt)]
    if verdict == "pay" and receipt.get("points"):
        payout = ", ".join(_money(v, c) for c, v in (receipt.get("contributor_payout") or {}).items() if v)
        lines += ["", f"Points: {receipt['points']['points']:g} ({receipt['points'].get('explanation', '')}). "
                      f"Proposed payout to {receipt['pr']['author']} this month: {payout or 'none'}."]
    trace = f" · neatlogs trace `{summary['trace_id']}`" if summary.get("trace_id") else ""
    lines += ["", f"Receipt sha256 `{ledger.sha256(receipt)[:12]}` · run `{summary['run_id']}`{trace}",
              "", _status_line(summary)]
    return "\n".join(lines) + "\n"


def close_issue_body(summary: dict, items: dict, approval: dict | None = None) -> str:
    verdicts = {int(n): v for n, v in summary["verdicts"].items()}
    allocations = summary["allocations"]
    points: dict[str, float] = {}
    prs: dict[str, list[int]] = {}
    for n, v in sorted(verdicts.items()):
        if v["verdict"] == "pay":
            points[v["author"]] = points.get(v["author"], 0) + (v.get("points") or 0)
            prs.setdefault(v["author"], []).append(n)
    primary = allocations[0]["currency"] if allocations else None
    ranked = sorted(points, key=lambda c: (-points[c], -(allocations[0]["payouts"].get(c, 0) if primary else 0), c))

    if approval:
        status = (f"**Approved** by {approval['approved_by']} at {approval['approved_at']}. "
                  f"Ledger head `{approval['hash'][:12]}`.")
    else:
        status = f"**{summary['status']}**. {_status_line(summary)}"
    trace = f" · neatlogs trace `{summary['trace_id']}`" if summary.get("trace_id") else ""
    lines = [CLOSE_MARKER, f"Linus month-end close for `{summary['repo']}`, period {summary['period']}.", "",
             f"Status: {status}", "", f"Run `{summary['run_id']}`{trace}", "", "## Leaderboard", "",
             "| Rank | Contributor | PRs | Points | " + " | ".join(a["currency"] for a in allocations) + " |",
             "|---:|---|---|---:|" + "---:|" * len(allocations)]
    for rank, who in enumerate(ranked, 1):
        lines.append(f"| {rank} | {who} | {' '.join(f'#{n}' for n in prs[who])} | {points[who]:g} | "
                     + " | ".join(_money(a["payouts"].get(who, 0), a["currency"]) for a in allocations) + " |")
    if any(a["rollover"] for a in allocations):
        lines.append("| | _rollover to next month_ | | | "
                     + " | ".join(_money(a["rollover"], a["currency"]) for a in allocations) + " |")

    model = summary["cost"].get("openai_model", "")
    lines += ["", "## Every PR", "", "| PR | Author | Verdict | Reason | Model cost |", "|---|---|---|---|---:|"]
    for n, v in sorted(verdicts.items()):
        cost = (items.get(n) or {}).get("data", {}).get("cost") or {}
        jev, openai = pr_cost_usd(cost, model)
        usd = f"${jev + openai:.5f}" if openai is not None else f"${jev:.5f} + OpenAI"
        reason = (v.get("reason") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| #{n} | {v['author']} | {v['verdict']} | {reason} | {usd} |")

    lines += ["", "## Invariants", ""]
    if summary["problems"]:
        lines += [f"- FAILED: {p}" for p in summary["problems"]]
    else:
        lines.append("- All hold: payouts + rollover = budget, per-contributor cap, minimum payout, "
                     "every payout backed by a verified receipt, nobody paid twice, no excluded authors.")
    c = summary["cost"]
    openai_usd = f"${c['openai_usd']:.4f}" if c.get("openai_usd") is not None else "price unknown"
    lines += ["", "## Cost", "",
              f"JEV {c['jev_tokens']:,} tokens (${c['jev_usd']:.5f}) · OpenAI {c['openai_tokens']:,} tokens "
              f"({model}, {openai_usd}) · ${c.get('usd_per_pr', 0):.4f} per PR · "
              f"{c['pr_seconds']:.0f}s of agent time for {c['prs']} PRs"]
    return "\n".join(lines) + "\n"


def _upsert_comment(gh: GitHub, repo: str, number: int, body: str) -> None:
    me = gh.login()
    for comment in gh.paginate(f"/repos/{repo}/issues/{number}/comments"):
        if VERDICT_MARKER in (comment.get("body") or "") and comment["user"]["login"] == me:
            if comment["body"] != body:
                gh.patch(f"/repos/{repo}/issues/comments/{comment['id']}", {"body": body})
            return
    gh.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})


def _set_label(gh: GitHub, repo: str, number: int, verdict: str) -> None:
    want = VERDICT_LABELS[verdict][0]
    have = {label["name"] for label in gh.paginate(f"/repos/{repo}/issues/{number}/labels")}
    for name, *_ in VERDICT_LABELS.values():
        if name != want and name in have:
            gh.delete(f"/repos/{repo}/issues/{number}/labels/{quote(name, safe='')}")
    if want not in have:
        gh.post(f"/repos/{repo}/issues/{number}/labels", {"labels": [want]})


def _find_close_issue(gh: GitHub, repo: str, period: str) -> dict | None:
    title = f"Bounty close {period}"
    for issue in gh.paginate(f"/repos/{repo}/issues", state="all", creator=gh.login()):
        if issue["title"] == title and CLOSE_MARKER in (issue.get("body") or "") and "pull_request" not in issue:
            return issue
    return None


def upsert_close_issue(gh: GitHub, summary: dict, items: dict, approval: dict | None = None,
                       create: bool = True) -> str | None:
    """Create or update the "Bounty close <period>" issue; its URL, or None if
    there is none and `create` is off."""
    repo, body = summary["repo"], close_issue_body(summary, items, approval)
    if issue := _find_close_issue(gh, repo, summary["period"]):
        if issue.get("body") != body:
            gh.patch(f"/repos/{repo}/issues/{issue['number']}", {"body": body})
        return issue["html_url"]
    if not create:
        return None
    return gh.post(f"/repos/{repo}/issues", {"title": f"Bounty close {summary['period']}", "body": body})["html_url"]


def report(gh: GitHub, summary: dict, items: dict, log: Callable[[str], None] = print) -> list[str]:
    """Post everything for a finished close. Returns the GitHub errors it skipped."""
    repo, errors = summary["repo"], []
    receipts = Path(summary["run_dir"]) / "receipts"

    def attempt(what: str, fn: Callable[[], object]) -> object:
        try:
            return fn()
        except (GitHubError, OSError, KeyError, ValueError) as exc:
            errors.append(f"{what}: {exc}")
            log(f"GitHub: {what} failed, skipped: {exc}")
            return None

    attempt("labels", lambda: gh.ensure_labels(repo, list(VERDICT_LABELS.values())))
    for n, v in sorted(((int(n), v) for n, v in summary["verdicts"].items())):
        path = receipts / f"pr-{n}.json"
        if not path.exists():
            continue
        receipt = json.loads(path.read_text())
        attempt(f"comment on #{n}", lambda: _upsert_comment(gh, repo, n, verdict_comment(receipt, summary)))
        attempt(f"label on #{n}", lambda: _set_label(gh, repo, n, v["verdict"]))
    url = attempt("close issue", lambda: upsert_close_issue(gh, summary, items))
    if url:
        log(f"GitHub: verdicts posted on {len(summary['verdicts'])} PRs; close issue {url}")
    return errors
