"""The month-end close: one agent run from a month of PRs to a proposed ledger.

    plan -> per PR: diff checks, JEV gates, oracle, sandbox  (checkpointed)
         -> cross-PR anti-gaming -> scoring -> allocation + invariants
         -> receipts -> proposed ledger -> report

Every PR's progress is saved in SQLite as soon as a stage finishes, so a
crashed run resumes where it stopped. Nothing is paid until a maintainer
approves the proposed ledger (`linus approve`).
"""

import os
import time
from dataclasses import asdict
from datetime import datetime, timezone

import neatlogs

from linus import __version__, diffcheck, gates, ledger, sandbox, scoring
from linus import oracle as oracle_mod
from linus import verify as verify_mod
from linus.allocate import allocate, check_invariants
from linus.config import Settings
from linus.diffcheck import DiffSignals
from linus.jev import USD_PER_INPUT_TOKEN, Jev
from linus.llm import Generator
from linus.oracle import Oracle
from linus.policy import Policy
from linus.source import Issue, LocalSource, PullRequest
from linus.store import Store
from linus.tracing import annotate, step

VERDICT = {"verified": "pay", "not_fixed": "reject", "regression": "reject", "tampering": "reject",
           "needs_human": "flag", "inconclusive": "flag"}


def conflicting(answers: dict, signals: DiffSignals, oracle_run: dict) -> bool:
    """The sandbox says 'not fixed' but everything else says 'honest, complete fix'.

    Oracles are generated, so they can be wrong. When a minority of cases fail and
    JEV is confident the diff fully fixes the issue with no gaming signals, don't
    auto-reject: route it to a human. (Never auto-pays either.)
    """
    addresses = answers.get("addresses_issue", {}).get("probabilities", {})
    full_fix = float(addresses.get("2", addresses.get(2, 0))) >= 0.8
    honest = (answers.get("special_casing", {}).get("noul", 1) < 0.5
              and answers.get("claims_match", {}).get("noul", 0) >= 0.5
              and not signals.tampering)
    total = oracle_run["passed"] + oracle_run["failed"]
    minority = total > 0 and oracle_run["failed"] * 3 <= total
    return full_fix and honest and minority


def corroboration(answers: dict, signals: DiffSignals) -> str:
    """Cheap signals that agree with a sandbox rejection, for the contributor."""
    notes = []
    if signals.removed_tests:
        notes.append(f"deletes {', '.join(signals.removed_tests)}")
    if signals.added_skips:
        notes.append(f"adds {signals.added_skips} skip marker(s)")
    if answers.get("special_casing", {}).get("noul", 0) >= 0.8:
        notes.append(f"JEV: hard-codes literal inputs (p={answers['special_casing']['noul']:.2f})")
    if answers.get("claims_match", {}).get("noul", 1) <= 0.3:
        notes.append(f"JEV: PR body claims changes the diff doesn't make (p={answers['claims_match']['noul']:.2f})")
    return f"; {'; '.join(notes)}" if notes else ""


class Close:
    def __init__(self, settings: Settings, source: LocalSource, policy: Policy, ui,
                 run_id: str | None = None, crash_after: int | None = None,
                 reuse_oracles: bool = True) -> None:
        self.settings, self.source, self.policy, self.ui = settings, source, policy, ui
        self.reuse_oracles = reuse_oracles
        self.store = Store(settings.state_dir / "linus.db")
        self.jev = Jev(settings)
        self.gen = Generator(settings)
        self.crash_after = crash_after
        self.resumed = run_id is not None
        self.run_id = run_id or f"{source.period}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        self.run_dir = settings.state_dir / "runs" / self.run_id
        self.workdir = settings.state_dir / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)

    # -- per PR ---------------------------------------------------------------

    def _issue(self, pr: PullRequest) -> Issue | None:
        for n in pr.issue_numbers:
            issue = self.source.issues.get(n)
            if issue and set(issue.labels) & set(self.policy.issue_labels):
                return issue
        return None

    def _gate(self, pr: PullRequest, issue: Issue | None, data: dict) -> tuple[str | None, str]:
        diff = self.source.diff(pr)
        signals = diffcheck.analyze(diff, self.source.changes(pr), f"{pr.title}\n{pr.body}")
        data["signals"] = asdict(signals)
        data["issue"] = issue.number if issue else None
        if why := self.policy.excluded(pr.author):
            return "reject", why
        if signals.docs_only and not self.policy.include_docs:
            return "reject", "docs-only PRs are not eligible under bounty.yml"
        if not signals.docs_only and issue is None:
            return "reject", "does not fix an issue labelled for this bounty"
        gate = gates.run(self.jev, pr, issue, diff, signals)
        data["gate"] = asdict(gate)
        if gate.decision == "reject":
            return "reject", gate.reason
        return None, ""

    def _oracle(self, issue: Issue, pr: PullRequest, signals: DiffSignals) -> Oracle:
        cached = self.store.oracle(self.source.name, issue.number, pr.base_sha)
        if cached and (self.reuse_oracles or cached.get("run_id") == self.run_id):
            return Oracle.from_dict(cached)
        built = oracle_mod.build(self.gen, self.jev, self.source, issue, pr.base_sha,
                                 set(signals.code_files), self.workdir)
        self.store.save_oracle(self.source.name, issue.number, pr.base_sha,
                               built.to_dict() | {"run_id": self.run_id})
        return built

    def _finish(self, pr: PullRequest, span, data: dict, verdict: str, reason: str,
                started: float, tokens0: tuple[int, int]) -> None:
        data["verdict"], data["reason"] = verdict, reason
        data["seconds"] = round(time.monotonic() - started + data.get("seconds", 0), 2)
        jev_tokens = self.jev.input_tokens - tokens0[0]
        openai_tokens = self.gen.prompt_tokens + self.gen.completion_tokens - tokens0[1]
        cost = data.setdefault("cost", {"jev_tokens": 0, "openai_tokens": 0})
        cost["jev_tokens"] += jev_tokens
        cost["openai_tokens"] += openai_tokens
        self.store.save_item(self.run_id, pr.number, "done", verdict, data)
        annotate(span, verdict=verdict, reason=reason)
        self.ui.item(pr, verdict, reason, data)

    def process(self, pr: PullRequest, item: dict | None) -> None:
        data = item["data"] if item else {}
        stage = item["stage"] if item else "queued"
        started = time.monotonic()
        tokens0 = (self.jev.input_tokens, self.gen.prompt_tokens + self.gen.completion_tokens)
        with step(f"pr_{pr.number}", kind="AGENT", author=pr.author, title=pr.title) as span:
            issue = self._issue(pr)
            if stage == "queued":
                verdict, reason = self._gate(pr, issue, data)
                if verdict:
                    return self._finish(pr, span, data, verdict, reason, started, tokens0)
                self.store.save_item(self.run_id, pr.number, "gated", None, data)

            signals = DiffSignals(**data["signals"])
            oracle = None
            if not signals.docs_only:
                oracle = self._oracle(issue, pr, signals)
                data["oracle"] = {"issue": oracle.issue, "valid": oracle.valid,
                                  "test_sha256": oracle.test_sha256, "attempts": len(oracle.attempts)}
                if not oracle.valid:
                    return self._finish(pr, span, data, "flag",
                                        f"could not build a reproduction test for #{issue.number}",
                                        started, tokens0)
            result = verify_mod.verify(self.source, pr, oracle, signals, self.workdir)
            data["verification"] = asdict(result)
            verdict, reason = VERDICT[result.outcome], result.reason
            answers = data.get("gate", {}).get("answers", {})
            if result.outcome == "not_fixed" and conflicting(answers, signals, result.runs[0]):
                verdict = "flag"
                reason = (f"evidence conflicts: {result.reason}, but every cheap signal says this fully "
                          f"fixes #{issue.number}; a human should check the failing case")
            elif verdict == "reject":
                reason += corroboration(answers, signals)
            self._finish(pr, span, data, verdict, reason, started, tokens0)

    # -- across PRs -----------------------------------------------------------

    def anti_gaming(self, pulls: list[PullRequest], items: dict[int, dict]) -> None:
        """First verified fix for an issue wins; later ones are copies or too late."""
        with step("anti_gaming", kind="GUARDRAIL") as span:
            winners: dict[int, PullRequest] = {}
            prints: dict[int, set[str]] = {}
            changed = []
            for pr in pulls:
                item = items[pr.number]
                data = item["data"]
                if item["verdict"] != "pay" or DiffSignals(**data["signals"]).docs_only:
                    continue
                prints[pr.number] = diffcheck.change_fingerprint(self.source.diff(pr))
                issue = data["issue"]
                if issue not in winners:
                    winners[issue] = pr
                    continue
                first = winners[issue]
                sim = diffcheck.similarity(prints[first.number], prints[pr.number])
                if sim >= self.policy.duplicate_similarity:
                    reason = f"copy of #{first.number} ({sim:.0%} of changed lines identical), submitted later"
                else:
                    reason = f"issue #{issue} was already fixed by #{first.number}, submitted earlier"
                data["anti_gaming"] = {"winner": first.number, "similarity": round(sim, 3)}
                data["verdict"], data["reason"] = "reject", reason
                item["verdict"] = "reject"
                self.store.save_item(self.run_id, pr.number, "done", "reject", data)
                changed.append(pr.number)
                self.ui.item(pr, "reject", reason, data, late=True)
            annotate(span, rejected=changed)

    def settle(self, pulls: list[PullRequest], items: dict[int, dict]) -> dict:
        policy = self.policy
        with step("settle", kind="CHAIN") as span:
            points: dict[str, float] = {}
            paid_prs: dict[str, list[int]] = {}
            for pr in pulls:
                item = items[pr.number]
                if item["verdict"] != "pay":
                    continue
                data = item["data"]
                pts = scoring.score(policy, data["gate"]["answers"], DiffSignals(**data["signals"]))
                data["points"] = asdict(pts)
                self.store.save_item(self.run_id, pr.number, "done", "pay", data)
                points[pr.author] = points.get(pr.author, 0) + pts.points
                paid_prs.setdefault(pr.author, []).append(pr.number)

            primary = policy.primary_currency
            allocations = [allocate(points, primary, policy.budget[primary], policy.cap_fraction, policy.min_payout)]
            survivors = {c: points[c] for c in allocations[0].payouts}
            for currency, amount in list(policy.budget.items())[1:]:
                allocations.append(allocate(survivors, currency, amount, policy.cap_fraction, None))

            ledger_path = self.settings.state_dir / "ledger.jsonl"
            verified = {pr.number for pr in pulls if items[pr.number]["verdict"] == "pay"}
            with step("invariants", kind="GUARDRAIL") as guard:
                problems = check_invariants(
                    allocations, policy.cap_fraction, policy.min_payout, primary,
                    {c: paid_prs[c] for c in allocations[0].payouts}, verified,
                    ledger.paid_prs(ledger_path, self.source.name), set(policy.excluded_authors))
                annotate(guard, problems=problems, ok=not problems)
            status = "halted" if problems else "proposed"

            image = sandbox.image_id()
            receipt_hashes: dict[int, str] = {}
            for pr in pulls:
                receipt = self._receipt(pr, items[pr.number]["data"], allocations, image)
                _, digest = ledger.write_receipt(self.run_dir / "receipts", pr.number, receipt)
                receipt_hashes[pr.number] = digest

            entries = []
            if not problems:
                for a in allocations:
                    for who, cents in sorted(a.payouts.items()):
                        entries.append({
                            "type": "payout", "repo": self.source.name, "period": self.source.period,
                            "run_id": self.run_id, "contributor": who, "currency": a.currency,
                            "amount": cents / 100, "prs": paid_prs[who],
                            "receipts": [receipt_hashes[n] for n in paid_prs[who]],
                            "policy_sha256": policy.sha256,
                        })
                    if a.rollover_cents:
                        entries.append({"type": "rollover", "repo": self.source.name, "period": self.source.period,
                                        "run_id": self.run_id, "currency": a.currency,
                                        "amount": a.rollover_cents / 100})
                proposed = self.run_dir / "ledger.proposed.jsonl"
                proposed.write_text("".join(ledger.canonical(e).decode() + "\n" for e in entries))

            summary = self._summary(pulls, items, allocations, problems, status)
            self.store.finish_run(self.run_id, status, summary)
            annotate(span, status=status, problems=problems)
        return summary

    def _receipt(self, pr: PullRequest, data: dict, allocations, image: str) -> dict:
        oracle = None
        if data.get("oracle") and data.get("issue") is not None:
            cached = self.store.oracle(self.source.name, data["issue"], pr.base_sha)
            if cached:
                oracle = {k: cached.get(k) for k in ("issue", "valid", "test_path", "test_sha256", "test_code",
                                                     "model", "cases", "dropped")}
                oracle["attempts"] = [{"attempt": a["attempt"], "proposed": a.get("proposed"),
                                       "kept": a.get("kept"), "problem": a.get("problem"),
                                       "base_outcome": (a.get("base_run") or {}).get("outcome"),
                                       "base_failed": (a.get("base_run") or {}).get("failed")}
                                      for a in cached["attempts"]]
        payout = {a.currency: a.payouts.get(pr.author, 0) / 100 for a in allocations} \
            if data.get("verdict") == "pay" else {}
        return {
            "linus_version": __version__, "run_id": self.run_id, "repo": self.source.name,
            "period": self.source.period,
            "pr": {"number": pr.number, "title": pr.title, "author": pr.author, "created_at": pr.created_at,
                   "base_sha": pr.base_sha, "head_sha": pr.head_sha, "issues": list(pr.issue_numbers)},
            "verdict": data.get("verdict"), "reason": data.get("reason"),
            "signals": data.get("signals"), "gate": data.get("gate"), "oracle": oracle,
            "verification": data.get("verification"), "anti_gaming": data.get("anti_gaming"),
            "points": data.get("points"), "contributor_payout": payout,
            "sandbox_image": image, "policy_sha256": self.policy.sha256,
        }

    def _summary(self, pulls, items, allocations, problems, status) -> dict:
        jev_tokens = sum(i["data"].get("cost", {}).get("jev_tokens", 0) for i in items.values())
        openai_tokens = sum(i["data"].get("cost", {}).get("openai_tokens", 0) for i in items.values())
        seconds = sum(i["data"].get("seconds", 0) for i in items.values())
        return {
            "run_id": self.run_id, "repo": self.source.name, "period": self.source.period, "status": status,
            "problems": problems,
            "verdicts": {pr.number: {"author": pr.author, "verdict": items[pr.number]["verdict"],
                                     "reason": items[pr.number]["data"].get("reason"),
                                     "points": (items[pr.number]["data"].get("points") or {}).get("points")}
                         for pr in pulls},
            "allocations": [{"currency": a.currency, "budget": a.budget_cents / 100,
                             "payouts": {c: v / 100 for c, v in a.payouts.items()},
                             "rollover": a.rollover_cents / 100, "dropped": a.dropped} for a in allocations],
            "cost": {"jev_tokens": jev_tokens, "jev_usd": round(jev_tokens * USD_PER_INPUT_TOKEN, 6),
                     "openai_tokens": openai_tokens, "openai_model": self.settings.openai_model,
                     "pr_seconds": round(seconds, 1), "prs": len(pulls)},
            "run_dir": str(self.run_dir),
        }

    # -- the run --------------------------------------------------------------

    def run(self) -> dict:
        if self.resumed:
            if not self.store.run(self.run_id):
                raise ValueError(f"no run {self.run_id} to resume")
        else:
            self.store.create_run(self.run_id, self.source.name, self.source.period)
        pulls = self.source.pulls()
        with step("month_end_close", kind="WORKFLOW", run_id=self.run_id, repo=self.source.name,
                  period=self.source.period, prs=len(pulls), resumed=self.resumed) as root:
            self.ui.plan(self, pulls)
            processed = 0
            for pr in pulls:
                item = self.store.item(self.run_id, pr.number)
                if item and item["stage"] == "done":
                    self.ui.item(pr, item["verdict"], item["data"].get("reason", ""), item["data"], resumed=True)
                    continue
                self.process(pr, item)
                processed += 1
                if self.crash_after and processed >= self.crash_after:
                    self.ui.crash(self.run_id, processed)
                    neatlogs.flush()
                    os._exit(137)  # simulated hard crash: no cleanup, no finally blocks
            items = self.store.items(self.run_id)
            self.anti_gaming(pulls, items)
            summary = self.settle(pulls, items)
            annotate(root, status=summary["status"])
        self.ui.done(summary)
        return summary
