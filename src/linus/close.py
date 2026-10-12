"""The month-end close: one agent run from a month of PRs to a proposed ledger.

    plan -> per PR: diff checks, JEV gates, oracle, sandbox  (checkpointed)
         -> cross-PR anti-gaming -> scoring -> allocation + invariants
         -> receipts -> proposed ledger -> report

Every PR's progress is saved in SQLite as soon as a stage finishes, so a
crashed run resumes where it stopped. Nothing is paid until a maintainer
approves the proposed ledger (`linus approve`).
"""

import hashlib
import os
import secrets
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
from linus.llm import Generator, usd
from linus.oracle import Oracle
from linus.policy import Policy
from linus.source import Issue, LocalSource, PullRequest
from linus.store import Store
from linus.tracing import annotate, step, trace_id_of

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
        notes.append(f"JEV: the PR body's claims match the diff only p={answers['claims_match']['noul']:.2f}")
    return f"; {'; '.join(notes)}" if notes else ""


def oracle_record(oracle: Oracle) -> dict:
    """The oracle exactly as used, frozen into the PR's item at verification time:
    receipts are built from this, so a later run can't swap the shared cache under it."""
    record = {k: getattr(oracle, k) for k in ("issue", "valid", "format", "test_path", "test_sha256", "test_code",
                                              "model", "imports", "cases", "dropped")}
    record["attempts"] = [{"attempt": a["attempt"], "proposed": a.get("proposed"), "kept": a.get("kept"),
                           "problem": a.get("problem"),
                           "base_outcome": (a.get("base_run") or {}).get("outcome"),
                           "base_failed": (a.get("base_run") or {}).get("failed")}
                          for a in oracle.attempts]
    return record


class Halted(RuntimeError):
    """The sandbox infrastructure failed mid-close. The PR it was judging keeps
    its last saved stage, the run stays running, and `--resume` retries it."""

    def __init__(self, run_id: str, pr: int, cause: Exception) -> None:
        super().__init__(f"sandbox infrastructure failed on #{pr}: {cause}. Nothing was judged for it and run "
                         f"{run_id} is still running: fix Docker, then `uv run linus close --resume latest`.")
        self.run_id, self.pr = run_id, pr


class Close:
    def __init__(self, settings: Settings, source: LocalSource, policy: Policy, ui,
                 run_id: str | None = None, crash_after: int | None = None,
                 reuse_oracles: bool = True, measurement: bool = False) -> None:
        self.settings, self.source, self.policy, self.ui = settings, source, policy, ui
        self.reuse_oracles = reuse_oracles
        # An eval measures verdicts; it never proposes money, so it may re-run a
        # settled period and ends as status "eval", which approve refuses.
        self.measurement = measurement
        self.store = Store(settings.state_dir / "linus.db")
        self.jev = Jev(settings)
        self.gen = Generator(settings)
        self.crash_after = crash_after
        self.resumed = run_id is not None
        # The random suffix keeps two runs started in the same second apart.
        self.run_id = run_id or (f"{source.period}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
                                 f"-{secrets.token_hex(2)}")
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
        if signals.manipulates_outcome:
            return "reject", "changes test outcomes instead of the code: " + "; ".join(signals.outcome_hacks[:3])
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
        # Only a valid oracle in the current format is reused: a failed build may have
        # been bad luck or a Docker problem. Infrastructure errors propagate before
        # anything is cached.
        cached = self.store.oracle(self.source.name, issue.number, pr.base_sha)
        current = cached and cached.get("valid") and cached.get("format") == oracle_mod.FORMAT
        if current and (self.reuse_oracles or cached.get("run_id") == self.run_id):
            return Oracle.from_dict(cached)
        built = oracle_mod.build(self.gen, self.jev, self.source, issue, pr.base_sha,
                                 set(signals.code_files), self.workdir)
        self.store.save_oracle(self.source.name, issue.number, pr.base_sha,
                               built.to_dict() | {"run_id": self.run_id})
        return built

    def _finish(self, pr: PullRequest, span, data: dict, verdict: str, reason: str,
                started: float, tokens0: tuple[int, int, int]) -> None:
        data["verdict"], data["reason"] = verdict, reason
        data["seconds"] = round(time.monotonic() - started + data.get("seconds", 0), 2)
        cost = data.setdefault("cost", {})
        for key, value in (("jev_tokens", self.jev.input_tokens - tokens0[0]),
                           ("openai_in", self.gen.prompt_tokens - tokens0[1]),
                           ("openai_out", self.gen.completion_tokens - tokens0[2])):
            cost[key] = cost.get(key, 0) + value
        self.store.save_item(self.run_id, pr.number, "done", verdict, data)
        annotate(span, verdict=verdict, reason=reason)
        self.ui.item(pr, verdict, reason, data)

    def process(self, pr: PullRequest, item: dict | None) -> None:
        """Judge one PR. Any failure in the PR itself flags it for a human and the
        close goes on: one malformed PR must not stop the month, or crash every
        resume. A Docker failure says nothing about the PR, so it must not become
        a verdict: the PR keeps its last saved stage and the close stops, resumable."""
        data = item["data"] if item else {}
        stage = item["stage"] if item else "queued"
        started = time.monotonic()
        tokens0 = (self.jev.input_tokens, self.gen.prompt_tokens, self.gen.completion_tokens)
        with step(f"pr_{pr.number}", kind="AGENT", author=pr.author, title=pr.title) as span:
            try:
                verdict, reason = self._judge(pr, data, stage)
            except sandbox.InfrastructureError as exc:
                saved = self.store.item(self.run_id, pr.number)
                data["error"] = f"InfrastructureError: {exc}"
                self.store.save_item(self.run_id, pr.number, saved["stage"] if saved else stage, None, data)
                annotate(span, halted=str(exc))
                raise Halted(self.run_id, pr.number, exc) from exc
            except Exception as exc:  # noqa: BLE001 - recorded on the PR, the run continues
                data["error"] = f"{type(exc).__name__}: {exc}"
                verdict, reason = "flag", f"could not process this PR: {data['error']}"
            self._finish(pr, span, data, verdict, reason[:500], started, tokens0)

    def _judge(self, pr: PullRequest, data: dict, stage: str) -> tuple[str, str]:
        issue = self._issue(pr)
        if stage == "queued":
            verdict, reason = self._gate(pr, issue, data)
            if verdict:
                return verdict, reason
            self.store.save_item(self.run_id, pr.number, "gated", None, data)

        signals = DiffSignals(**data["signals"])
        oracle = None
        if not signals.docs_only:
            oracle = self._oracle(issue, pr, signals)
            data["oracle"] = oracle_record(oracle)
            if not oracle.valid:
                return "flag", f"could not build a reproduction test for #{issue.number}"
        result = verify_mod.verify(self.source, pr, oracle, signals, self.workdir)
        data["verification"] = asdict(result)
        verdict, reason = VERDICT[result.outcome], result.reason
        answers = data.get("gate", {}).get("answers", {})
        if result.outcome == "not_fixed" and conflicting(answers, signals, result.runs[0]):
            verdict = "flag"
            reason = (f"evidence conflicts: {result.reason}, but every cheap signal says this fully "
                      f"fixes #{issue.number}; a human should check the failing case")
        elif signals.changes_dependencies and result.outcome in ("verified", "not_fixed", "regression"):
            # Its new packages aren't in the sandbox, so a failure may only mean a
            # missing import; and adding dependencies is the maintainer's call.
            verdict = "flag"
            reason = (f"{result.reason}, but the PR changes dependencies "
                      f"({', '.join(signals.dependency_files)}); the sandbox only has the default branch's, "
                      f"so a human should check them")
        elif verdict == "reject":
            reason += corroboration(answers, signals)
        elif verdict == "pay" and signals.touches_test_harness:
            verdict = "flag"
            reason = (f"{result.reason}, but the PR edits the test harness "
                      f"({', '.join(signals.harness_files)}); a human should check it doesn't change "
                      f"how tests run")
        return verdict, reason

    # -- across PRs -----------------------------------------------------------

    def anti_gaming(self, pulls: list[PullRequest], items: dict[int, dict]) -> None:
        """Every PR is compared with every earlier PR on the same issue (docs-only
        PRs with earlier docs-only PRs), whatever their verdicts: a copy of a
        rejected PR is still a copy. The first verified fix for an issue wins, and
        a later fix waits while an earlier one is with a human."""
        with step("anti_gaming", kind="GUARDRAIL") as span:
            earlier: dict[object, list[PullRequest]] = {}
            prints: dict[int, set[str]] = {}
            changed = []
            for pr in pulls:
                item = items.get(pr.number)
                if not item or not item["data"].get("signals"):
                    continue
                data = item["data"]
                docs = DiffSignals(**data["signals"]).docs_only
                group = "docs" if docs else data.get("issue")
                if group is None:
                    continue
                prints[pr.number] = diffcheck.change_fingerprint(self.source.diff(pr))
                before = earlier.setdefault(group, [])
                found = self._against_earlier(pr, before, items, prints, docs)
                before.append(pr)
                if not found:
                    continue
                verdict, reason, other, sim = found
                data["anti_gaming"] = {"winner": other, "similarity": round(sim, 3)}
                data["verdict"], data["reason"] = verdict, reason
                item["verdict"] = verdict
                self.store.save_item(self.run_id, pr.number, "done", verdict, data)
                changed.append(pr.number)
                self.ui.item(pr, verdict, reason, data, late=True)
            annotate(span, changed=changed)

    def _against_earlier(self, pr: PullRequest, before: list[PullRequest], items: dict[int, dict],
                         prints: dict[int, set[str]], docs: bool) -> tuple[str, str, int, float] | None:
        """A new (verdict, reason, other PR, similarity) for `pr`, or None to keep its verdict."""
        if not before:
            return None
        sims = {p.number: diffcheck.similarity(prints[p.number], prints[pr.number]) for p in before}
        closest = max(before, key=lambda p: sims[p.number])
        verdict = items[pr.number]["verdict"]
        if sims[closest.number] >= self.policy.duplicate_similarity and verdict != "reject":
            return ("reject", f"copy of #{closest.number} ({sims[closest.number]:.0%} of changed lines "
                    f"identical), submitted later", closest.number, sims[closest.number])
        if docs or verdict != "pay":
            return None
        issue = items[pr.number]["data"]["issue"]
        if paid := next((p for p in before if items[p.number]["verdict"] == "pay"), None):
            return ("reject", f"issue #{issue} was already fixed by #{paid.number}, submitted earlier",
                    paid.number, sims[paid.number])
        if flagged := next((p for p in before if items[p.number]["verdict"] == "flag"), None):
            return ("flag", f"verified, but waiting on #{flagged.number}'s human review: it targets "
                    f"#{issue} and was submitted earlier", flagged.number, sims[flagged.number])
        return None

    def _refuse_if_settled(self) -> None:
        """A period is paid once: a second run would book the budget again."""
        if self.measurement:
            return
        approved = self.store.approved_run(self.source.name, self.source.period)
        if approved and approved != self.run_id:
            raise ValueError(f"{self.source.name} {self.source.period} was already settled by approved run "
                             f"{approved}; refusing to close it again")

    def settle(self, pulls: list[PullRequest], items: dict[int, dict]) -> dict:
        self._refuse_if_settled()
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
            status = "halted" if problems else ("eval" if self.measurement else "proposed")

            try:
                image = sandbox.image_id()
            except sandbox.InfrastructureError:
                image = "unavailable"
            receipt_hashes: dict[int, str] = {}
            for pr in pulls:
                receipt = self._receipt(pr, items[pr.number]["data"], allocations, image)
                _, digest = ledger.write_receipt(self.run_dir / "receipts", pr.number, receipt)
                receipt_hashes[pr.number] = digest

            entries = []
            proposal_sha256 = None
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
                raw = "".join(ledger.canonical(e).decode() + "\n" for e in entries).encode()
                self.run_dir.mkdir(parents=True, exist_ok=True)  # no receipts were written for an empty month
                (self.run_dir / "ledger.proposed.jsonl").write_bytes(raw)
                # `linus approve` refuses a proposal whose bytes no longer match this.
                proposal_sha256 = hashlib.sha256(raw).hexdigest()

            summary = self._summary(pulls, items, allocations, problems, status)
            summary["proposal_sha256"] = proposal_sha256
            self.store.finish_run(self.run_id, status, summary)
            annotate(span, status=status, problems=problems)
        return summary

    def _receipt(self, pr: PullRequest, data: dict, allocations, image: str) -> dict:
        oracle = data.get("oracle")  # frozen when the PR was verified, see oracle_record()
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
        def total(key: str) -> int:
            return sum(i["data"].get("cost", {}).get(key, 0) for i in items.values())

        jev_tokens, openai_in, openai_out = total("jev_tokens"), total("openai_in"), total("openai_out")
        openai_usd = usd(self.settings.openai_model, openai_in, openai_out)
        jev_usd = jev_tokens * USD_PER_INPUT_TOKEN
        seconds = sum(i["data"].get("seconds", 0) for i in items.values())
        return {
            "run_id": self.run_id, "repo": self.source.name, "period": self.source.period, "status": status,
            "source": getattr(self.source, "kind", "local"), "cold": not self.reuse_oracles,
            "oracle_format": oracle_mod.FORMAT,
            "policy": {"origin": self.policy.origin, "sha256": self.policy.sha256},
            "problems": problems,
            "verdicts": {pr.number: {"author": pr.author, "verdict": items[pr.number]["verdict"],
                                     "reason": items[pr.number]["data"].get("reason"),
                                     "points": (items[pr.number]["data"].get("points") or {}).get("points"),
                                     "branch": pr.branch}
                         for pr in pulls},
            "allocations": [{"currency": a.currency, "budget": a.budget_cents / 100,
                             "payouts": {c: v / 100 for c, v in a.payouts.items()},
                             "rollover": a.rollover_cents / 100, "dropped": a.dropped} for a in allocations],
            "cost": {"jev_tokens": jev_tokens, "jev_usd": round(jev_usd, 6),
                     "openai_tokens": openai_in + openai_out, "openai_in": openai_in, "openai_out": openai_out,
                     "openai_usd": round(openai_usd, 6) if openai_usd is not None else None,
                     "openai_model": self.settings.openai_model,
                     "usd_per_pr": round((jev_usd + (openai_usd or 0)) / max(len(pulls), 1), 6),
                     "pr_seconds": round(seconds, 1), "prs": len(pulls)},
            "run_dir": str(self.run_dir),
            "trace_id": getattr(self, "trace_id", ""),
        }

    # -- the run --------------------------------------------------------------

    def _check_resumable(self) -> None:
        existing = self.store.run(self.run_id)
        if not existing:
            raise ValueError(f"no run {self.run_id} to resume")
        if existing["status"] != "running":
            # A finished run's status (proposed, halted, approved) must never be overwritten.
            raise ValueError(f"run {self.run_id} is {existing['status']}; only unfinished runs can be resumed")
        # Verdicts reached under an older oracle format (e.g. v5's in-sandbox
        # comparison, which a PR could patch) must not be carried into this one.
        formats = {existing.get("oracle_format")} | {
            (i["data"].get("oracle") or {}).get("format", existing.get("oracle_format"))
            for i in self.store.items(self.run_id).values() if i["data"].get("oracle")}
        if formats != {oracle_mod.FORMAT}:
            old = sorted(str(f) for f in formats - {oracle_mod.FORMAT})
            raise ValueError(f"run {self.run_id} was judged under oracle format {', '.join(old)}, and this Linus "
                             f"judges with format {oracle_mod.FORMAT}; its verdicts can't be resumed. Start a "
                             f"fresh close (`uv run linus close`).")

    def run(self) -> dict:
        if self.resumed:
            self._check_resumable()
        self._refuse_if_settled()
        pulls = self.source.pulls()  # before the run exists: a source that can't be read leaves no empty run
        if not self.resumed:
            self.store.create_run(self.run_id, self.source.name, self.source.period, oracle_mod.FORMAT)
        with step("month_end_close", kind="WORKFLOW", run_id=self.run_id, repo=self.source.name,
                  period=self.source.period, prs=len(pulls), resumed=self.resumed) as root:
            self.trace_id = trace_id_of(root)
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
