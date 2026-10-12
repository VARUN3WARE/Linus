"""What a maintainer looks at between closes: every repo's latest month, what
needs them, and the approved payouts to hand to whoever pays people.

Pure functions over the runs table and the ledger, so the CLI only prints.
"""

import csv
import io
import json

PLAIN_STATUS = {
    "proposed": "waiting for your approval",
    "approved": "approved",
    "running": "unfinished (resume it)",
    "halted": "stopped: invariants failed",
}


def _summary(run: dict) -> dict:
    try:
        return json.loads(run.get("summary") or "{}")
    except ValueError:
        return {}


def pr_url(repo: str, number, source: str) -> str:
    return f"https://github.com/{repo}/pull/{number}" if source == "github" else ""


def latest(runs: list[dict]) -> list[dict]:
    """One run per (repo, period): the approved one if the period is settled,
    else the newest. Eval runs are measurements and never shown."""
    chosen: dict[tuple[str, str], dict] = {}
    for run in sorted(runs, key=lambda r: r["created_at"] or "", reverse=True):
        if run["status"] == "eval":
            continue
        key = (run["repo"], run["period"])
        if key not in chosen or (run["status"] == "approved" and chosen[key]["status"] != "approved"):
            chosen[key] = run
    return sorted(chosen.values(), key=lambda r: (r["repo"], r["period"]), reverse=False)


def overview(runs: list[dict], registered: list[dict]) -> dict:
    """Rows per repo-month, plus the to-do list."""
    rows, todo = [], []
    seen = set()
    for run in latest(runs):
        s = _summary(run)
        seen.add(run["repo"])
        verdicts = s.get("verdicts", {})
        counts = {k: sum(1 for v in verdicts.values() if v.get("verdict") == k) for k in ("pay", "reject", "flag")}
        primary = (s.get("allocations") or [{}])[0]
        paid = sum((primary.get("payouts") or {}).values())
        rows.append({"repo": run["repo"], "period": run["period"], "status": run["status"],
                     "plain": PLAIN_STATUS.get(run["status"], run["status"]), "run_id": run["id"],
                     "prs": len(verdicts), **counts,
                     "payouts": f"{paid:,.2f} {primary['currency']}" if primary.get("currency") else "-"})
        source = s.get("source", "local")
        repo_opt = f" --github {run['repo']} --period {run['period']}" if source == "github" else ""
        if run["status"] == "proposed":
            todo.append({"repo": run["repo"], "period": run["period"], "what": "approve the payouts",
                         "how": f"read {s.get('run_dir', '.linus/runs/' + run['id'])}/report.md, then "
                                f"`uv run linus approve {run['id']}`"})
        elif run["status"] == "running":
            todo.append({"repo": run["repo"], "period": run["period"], "what": "finish the close",
                         "how": f"`uv run linus close{repo_opt} --resume {run['id']}`"})
        elif run["status"] == "halted":
            todo.append({"repo": run["repo"], "period": run["period"], "what": "fix what stopped the close",
                         "how": f"see {s.get('run_dir', '')}/report.md: {'; '.join(s.get('problems', []))[:200]}"})
        if run["status"] in ("proposed", "approved"):
            for number, v in sorted(verdicts.items(), key=lambda kv: int(kv[0])):
                if v.get("verdict") == "flag":
                    todo.append({"repo": run["repo"], "period": run["period"],
                                 "what": f"look at PR #{number} by {v.get('author')}",
                                 "how": (v.get("reason") or "")[:240],
                                 "url": pr_url(run["repo"], number, source)})
    for repo in registered:
        if repo["repo"] not in seen:
            todo.append({"repo": repo["repo"], "period": "", "what": "run the first close",
                         "how": f"`uv run linus close --github {repo['repo']} --period last --comment` "
                                "on the 1st (or `--period this` to try it now)"})
    return {"rows": rows, "todo": todo,
            "repos": sorted({r["repo"] for r in rows} | {r["repo"] for r in registered})}


def approved_payouts(entries: list[dict], sources: dict[str, str] | None = None,
                     repo: str | None = None, period: str | None = None) -> list[dict]:
    """Payout lines from the ledger whose run a maintainer approved. `sources`
    maps repo -> 'github' or 'local' (for PR links)."""
    sources = sources or {}
    approvals = {e["run_id"]: e for e in entries if e.get("type") == "approval"}
    rows = []
    for e in entries:
        if e.get("type") != "payout" or e.get("run_id") not in approvals:
            continue
        if (repo and e["repo"] != repo) or (period and e["period"] != period):
            continue
        approval = approvals[e["run_id"]]
        github = sources.get(e["repo"]) == "github"
        rows.append({
            "repo": e["repo"], "period": e["period"], "contributor": e["contributor"],
            "github_profile": f"https://github.com/{e['contributor']}" if github else "",
            "amount": f"{e['amount']:.2f}", "currency": e["currency"],
            "prs": " ".join(f"#{n}" for n in e.get("prs", [])),
            "pr_links": " ".join(pr_url(e["repo"], n, "github") for n in e.get("prs", [])) if github else "",
            "run_id": e["run_id"], "approved_by": approval.get("approved_by", ""),
            "approved_at": approval.get("approved_at", ""), "ledger_hash": e.get("hash", ""),
        })
    return rows


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    fields = ["repo", "period", "contributor", "github_profile", "amount", "currency", "prs", "pr_links",
              "run_id", "approved_by", "approved_at", "ledger_hash"]
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k, "")) for k in fields})
    return out.getvalue()


def _cell(value) -> str:
    """Spreadsheets run cells that start with = + - @ as formulas."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text
