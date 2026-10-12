"""What humans see: live console output during a close, and report.md after."""

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

STYLE = {"pay": ("PAY", "bold green"), "reject": ("REJECT", "bold red"), "flag": ("FLAG", "bold yellow")}


def _money(amount: float, currency: str) -> str:
    return f"${amount:,.2f}" if currency == "USD" else f"{amount:,.2f} {currency}"


class ConsoleUI:
    def __init__(self, console: Console) -> None:
        self.c = console

    def plan(self, close, pulls) -> None:
        budget = " + ".join(_money(v, k) for k, v in close.policy.budget.items())
        title = "Resuming" if close.resumed else "Month-end close"
        self.c.print(Panel.fit(
            f"[bold]{close.source.name}[/]  period {close.source.period}  ·  {len(pulls)} PRs  ·  pool {budget}\n"
            f"run [cyan]{close.run_id}[/]",
            title=f"Linus · {title}", border_style="cyan"))

    def item(self, pr, verdict: str | None, reason: str, data: dict, resumed: bool = False,
             late: bool = False) -> None:
        label, style = STYLE.get(verdict or "", ("?", "white"))
        tag = " [dim](checkpoint)[/]" if resumed else (" [dim](anti-gaming)[/]" if late else "")
        self.c.print(f"  #{pr.number}  {pr.author:<12} [{style}]{label:<6}[/]  {reason}{tag}")

    def crash(self, run_id: str, processed: int) -> None:
        self.c.print(f"\n[bold red]Simulated crash (kill -9) after {processed} PRs.[/] "
                     f"State is saved per PR. Resume with: [bold]uv run linus close --resume {run_id}[/]")

    def done(self, summary: dict) -> None:
        titles = {"proposed": "Proposed payouts", "eval": "Payouts (eval run: measurement only, not approvable)"}
        table = Table(title=titles.get(summary["status"], "Payouts (halted)"))
        table.add_column("contributor")
        table.add_column("PRs")
        for a in summary["allocations"]:
            table.add_column(a["currency"], justify="right")
        authors = sorted({c for a in summary["allocations"] for c in a["payouts"]})
        prs_by_author: dict[str, list[str]] = {}
        for n, v in summary["verdicts"].items():
            if v["verdict"] == "pay":
                prs_by_author.setdefault(v["author"], []).append(f"#{n}")
        for who in authors:
            table.add_row(who, " ".join(prs_by_author.get(who, [])),
                          *[_money(a["payouts"].get(who, 0), a["currency"]) for a in summary["allocations"]])
        if any(a["rollover"] for a in summary["allocations"]):
            table.add_row("[dim]rollover[/]", "", *[_money(a["rollover"], a["currency"])
                                                    for a in summary["allocations"]])
        self.c.print()
        self.c.print(table)
        counts = {k: sum(1 for v in summary["verdicts"].values() if v["verdict"] == k)
                  for k in ("pay", "reject", "flag")}
        cost = summary["cost"]
        self.c.print(f"  verdicts: [green]{counts['pay']} pay[/] · [red]{counts['reject']} reject[/] · "
                     f"[yellow]{counts['flag']} flag[/]")
        openai_usd = f"${cost['openai_usd']:.4f}" if cost.get("openai_usd") is not None else "price unknown"
        self.c.print(f"  cost: JEV {cost['jev_tokens']:,} tokens (${cost['jev_usd']:.5f}) · "
                     f"OpenAI {cost['openai_tokens']:,} tokens ({cost['openai_model']}, {openai_usd}) · "
                     f"[bold]${cost.get('usd_per_pr', 0):.4f} per PR[/] · {cost['pr_seconds']:.0f}s of agent time")
        if summary["problems"]:
            self.c.print("[bold red]  invariants failed, nothing will be paid:[/]")
            for p in summary["problems"]:
                self.c.print(f"    - {p}")
        else:
            self.c.print("  invariants: [green]all hold[/] (sum = budget, caps, minimums, "
                         "verified receipts, no double pay)")
        self.c.print(f"  report: {summary['run_dir']}/report.md")
        if summary.get("trace_id"):
            self.c.print(f"  neatlogs trace: {summary['trace_id']}  (neatlogs trace get {summary['trace_id']})")
        if summary["status"] == "proposed":
            self.c.print(f"  next: review, then [bold]uv run linus approve {summary['run_id']}[/]")


def markdown(summary: dict) -> str:
    run_dir = Path(summary["run_dir"])
    lines = [
        f"# Linus close: {summary['repo']} · {summary['period']}",
        "",
        f"Run `{summary['run_id']}` · status **{summary['status']}**"
        + (f" · neatlogs trace `{summary['trace_id']}`" if summary.get("trace_id") else ""),
        "",
        "## Payouts",
        "",
    ]
    currencies = [a["currency"] for a in summary["allocations"]]
    lines.append("| Contributor | " + " | ".join(currencies) + " |")
    lines.append("|---|" + "---:|" * len(currencies))
    authors = sorted({c for a in summary["allocations"] for c in a["payouts"]})
    for who in authors:
        lines.append(f"| {who} | " + " | ".join(_money(a["payouts"].get(who, 0), a["currency"])
                                                for a in summary["allocations"]) + " |")
    rollover = [a for a in summary["allocations"] if a["rollover"]]
    if rollover:
        lines.append("| _rollover to next month_ | " + " | ".join(
            _money(a["rollover"], a["currency"]) for a in summary["allocations"]) + " |")
    lines += ["", "## Every PR", "", "| PR | Author | Verdict | Why | Points |", "|---|---|---|---|---:|"]
    for n, v in sorted(summary["verdicts"].items(), key=lambda kv: int(kv[0])):
        pts = f"{v['points']:g}" if v.get("points") else ""
        lines.append(f"| #{n} | {v['author']} | {v['verdict']} | {v['reason']} | {pts} |")

    lines += ["", "## Evidence", ""]
    for n in sorted(summary["verdicts"], key=int):
        path = run_dir / "receipts" / f"pr-{n}.json"
        if not path.exists():
            continue
        r = json.loads(path.read_text())
        lines.append(f"### #{n} {r['pr']['title']} ({r['pr']['author']})")
        lines.append("")
        lines.append(f"- Verdict: **{r['verdict']}**: {r['reason']}")
        gate = (r.get("gate") or {}).get("answers") or {}
        if gate:
            bits = [f"{k} p={v['noul']:.2f}" for k, v in gate.items() if "noul" in v]
            bits += [f"{k} level {max(v['probabilities'], key=lambda x: v['probabilities'][x])} "
                     f"(conf {v['confidence']:.2f})" for k, v in gate.items() if "score" in v]
            lines.append(f"- JEV ({r['gate'].get('jev_model', '')}): " + ", ".join(bits))
        if r.get("signals", {}).get("injection_hits"):
            lines.append(f"- Injection text found: `{r['signals']['injection_hits'][0][:120]}`")
        if r.get("signals", {}).get("removed_tests"):
            lines.append(f"- Tests removed by the PR: {', '.join(r['signals']['removed_tests'])}")
        if r.get("oracle"):
            o = r["oracle"]
            tries = ", ".join(f"try {a['attempt']}: base {a['base_outcome']}" for a in o["attempts"])
            lines.append(f"- Oracle for #{o['issue']}: `{o['test_path']}` sha256 `{(o['test_sha256'] or '')[:12]}` "
                         f"({len(o.get('cases') or [])} cases; {tries})")
            for c in o.get("dropped") or []:
                lines.append(f"  - JEV dropped generated case `{c['call']} == {c['expected']}` "
                             f"(consistent with issue p={c['jev']:.2f})")
        for run in (r.get("verification") or {}).get("runs", []):
            lines.append(f"- Sandbox `{run['label']}`: {run['outcome']} "
                         f"({run['passed']} passed, {run['failed']} failed, {run['duration_s']}s)")
        if r.get("anti_gaming"):
            lines.append(f"- Anti-gaming: similarity {r['anti_gaming']['similarity']:.0%} to #{r['anti_gaming']['winner']}")
        if r.get("points"):
            lines.append(f"- Points: {r['points']['explanation']}")
        lines.append(f"- Receipt: `receipts/pr-{n}.json`")
        lines.append("")

    cost = summary["cost"]
    lines += [
        "## Cost", "",
        f"- JEV: {cost['jev_tokens']:,} input tokens, ${cost['jev_usd']:.5f}",
        f"- OpenAI ({cost['openai_model']}): {cost['openai_tokens']:,} tokens"
        + (f", ${cost['openai_usd']:.4f}" if cost.get("openai_usd") is not None else ""),
        f"- Model cost per PR: ${cost.get('usd_per_pr', 0):.4f}",
        f"- Agent time: {cost['pr_seconds']:.0f}s across {cost['prs']} PRs",
        "",
        "## Invariants", "",
    ]
    if summary["problems"]:
        lines += [f"- FAILED: {p}" for p in summary["problems"]]
    else:
        lines.append("- All hold: payouts + rollover = budget, per-contributor cap, minimum payout, "
                     "every payout backed by a verified receipt, nobody paid twice, no excluded authors.")
    return "\n".join(lines) + "\n"
