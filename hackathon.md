# Linus at neatHack

## Problem

Open-source companies want outside contributors and will pay bounties, but
money attracts AI slop: PRs that pass CI, look plausible and fix nothing. curl
ended its bug bounty in January 2026 for exactly this reason. Maintainers can't
verify every PR by hand, and splitting a monthly budget fairly is manual and
contentious.

## What Linus does

An agent that runs the month-end close for a bounty pool: it verifies every PR
in the period with reproducible evidence, rejects or flags what it can't prove,
allocates a fixed budget with a deterministic formula and hard invariants, and
writes a tamper-evident ledger for the maintainer to approve.

## Team

- Varun ([@VARUN3WARE](https://github.com/VARUN3WARE))

## Tech stack

- Python 3.10+, uv, Typer, Rich, SQLite
- JEV (TypeSafe System One) for every cheap, typed decision
- OpenAI `gpt-5.4-mini` only for generating reproduction test cases
- Docker sandbox (no network, read-only mounts) for running untrusted PR code
- neatlogs for tracing, Entire for coding-agent checkpoints, cfo.ai for the
  business plan

## Partner tool evidence

| Tool | How we use it | Evidence |
|---|---|---|
| neatlogs | One trace per close: workflow span, agent span per PR, JEV calls as LLM spans with tokens and cost, auto-captured OpenAI calls, sandbox runs as tool spans, anti-gaming and invariant guardrails | Workflow `linus`. Before/after pairs: v4 paying two forgeries `b810aeed4d1619f5140600f600284d89` → v5 `c7560a80a11201b97d01923f5fe9bb57`; v5's sandbox certifying #116 `e67b7aaaf8a3921d2a772faaa3629c73` → v6 `3952c9122cbadec1de92d2447b270a9f`. Live GitHub v6 close `dcf6aab1eeeb52ffa00bbc41e8684924`. _Add dashboard screenshots._ |
| Entire | Enabled for the coding agent in this repo (`.entire/`, agent hooks) with PII (email/phone) redaction on | Every code commit from Oct 12 has an `Entire-Checkpoint` trailer; 7 checkpoints are pushed to GitHub (`refs/entire/checkpoints/...`, `entire checkpoint list`). The Oct 11 session ran with the parent folder as its working directory, so the repo's hooks never loaded and it has none. |
| cfo.ai | Pricing, cost per verified PR, margins, runway | [`BUSINESS_PLAN.md`](BUSINESS_PLAN.md): 24-month model built in cfo.ai by Ari from `evals/economics.json` (Main/Growth/Downside, pricing comparison, cash simulator, risks). Ari corrected our break-even from ~10,000 to ~3,600 PRs/repo/month. _Share link: pending._ |
| JEV | Gates, oracle audit, scoring levels | Every receipt records JEV's answers and model version (`jev-1.13.0`) |

## Build log

### 2026-10-11

- Scaffold: config, neatlogs tracing, `linus doctor`. Read the JEV docs end to
  end: JEV is a decision model (Noul / Choice / Score), not a chat model, so the
  design became "JEV decides, OpenAI generates, tests prove, code pays".
- Pipeline: demo month (11 PRs, 6 attacks, CI green on all), JEV gates, oracle
  per issue, Docker sandbox, anti-gaming, allocation with invariants, receipts,
  hash-chained ledger, SQLite checkpoints, evals.
- **v1:** every verdict correct, then crashed while settling: JEV score keys came
  back as strings after a JSON round-trip. Fixed, then `linus close --resume
  latest` finished the run from per-PR checkpoints with zero repeated API calls.
- Payout calibration: v1's point table had 3x gaps between adjacent difficulty
  levels, so JEV's trivial-vs-small uncertainty became 3x payout differences
  for near-identical one-line fixes. Smoothed the table.
- Allocation bug found by a unit check: with a 30% cap, a 1-point contributor
  received $600 because capped excess was redistributed. Excess now rolls over.
- **v2 (cold oracles):** rejected a correct PR. The generated oracle expected
  `slugify("Café & Tea / Coffee") == "caf--tea-coffee"`, contradicting the
  issue's single-dash rule. Missed-good 20%.
- **v3:** JEV audits every generated case against the issue (drops that case at
  p=0.07, keeps the correct one at p=0.87). Exposed a rendering bug: expected
  values rendered as code (`== hello-world`), so the oracle "reproduced" the
  bug by crashing with NameError.
- **v4:** expected values rendered from JSON with repr(); an oracle only counts
  if it fails on base for a real reason, not NameError/ImportError/SyntaxError.
  100% accuracy, 0% false-pay, 0% missed-good on 5 consecutive cold runs.
- Sandbox/JEV conflict routing: when the oracle says "not fixed" but every
  cheap signal says "honest, complete fix", flag for a human instead of
  rejecting.
- Receipts re-verify with no LLM calls (`linus receipt`); ledger edits are
  detected (`linus ledger`); re-running an approved month halts on the
  double-pay invariant.

### 2026-10-12

- Audit of the whole project by six independent reviewers (each finding re-checked by a
  second agent). It found that Entire had recorded nothing (wrong working directory),
  overstated README claims, and a high-severity hole: a PR could forge its own green run.
  README claims corrected first.
- Every code change from here on was made by a coding-agent session started inside the repo,
  so each commit carries an Entire checkpoint. Turned on Entire's PII redaction after finding
  that headless sessions carry the user's email.
- **Forgery round:** added #112 (conftest that forces pytest's exit status to 0) and #113 (an
  int whose `==` is always True, submitted before dave-k's honest fix). **v4 paid both** and
  #113 took dave-k's bounty: 77% accuracy, 25% false-pay, 20% missed-good
  (trace `b810aeed...`).
- **v5:** oracle run from a Linus-owned directory with Linus's pytest config and no PR
  conftest, strict JSON result types, JUnit cross-checks, harness/outcome-manipulation diff
  checks, per-PR crash isolation, approve bound to the exact proposal, anti-gaming across
  flagged PRs, invalid oracles not cached. 100% / 0% / 0% (trace `c7560a80...`).
- **Live on GitHub:** `linus demo --publish` created
  [VARUN3WARE/textkit-bounty](https://github.com/VARUN3WARE/textkit-bounty) with real PRs;
  `linus close --github ... --comment` posted a verdict with evidence on every PR and a
  leaderboard issue. `linus economics` exports measured unit economics for cfo.ai.
- **Red-team round:** three attacking agents against a frozen v5, every finding reproduced
  independently. Confirmed: import-time patching of the oracle's comparison helper via
  `sys.modules`, a second package copy under `src/` in flat repos, six false rejections of
  honest PRs, and logic bugs (infrastructure failures settling a month as rollover, among
  others). Added #114, #115 and a disguised #116; **v5's sandbox certified #116**
  (trace `e67b7aaa...`).
- **v6:** the expected values never enter the sandbox (calls go in, raw results come out,
  the host compares); base tests run from their own tree and every base test id must appear;
  import roots from the base tree; narrower diff rules so honest code is never rejected as
  forgery; infrastructure failures halt the run and stay resumable; a month is settled once;
  evals can never be approved. The headless session doing v6 ran out of credit midway; it was
  finished from a session outside the repo. 100% / 0% / 0% on all 16 PRs
  (trace `3952c912...`), and on the live repo.
- **cfo.ai:** Ari (cfo.ai's AI CFO) built a 24-month model from `linus economics`: recommends
  $49/repo including 100 PRs then $0.25/PR; base case cash-positive Aug 2027, $64.7k at month
  24 from $50k; it caught that flat pricing breaks even near 3,600 PRs/month, not 10,000.
  Written up in BUSINESS_PLAN.md.

## Eval scorecard

See [`evals/SCORECARD.md`](evals/SCORECARD.md). Every row is a real run against
the demo month; ground truth is in `expected.json`, which the agent never sees.

| Metric | v1 | v2 | v3 | v4 (5 cold runs) | v4 vs forgeries | v5 | v6 |
|---|---|---|---|---|---|---|---|
| PRs | 11 | 11 | 11 | 11 | 13 | 13 | 16 |
| Accuracy | 100% | 91% | 91% | 100% | 77% | 100% | 100% |
| False-pay rate | 0% | 0% | 0% | 0% | 25% | 0% | 0% |
| Missed-good rate | 0% | 20% | 20% | 0% | 20% | 0% | 0% |
| Human-flag rate | 0% | 0% | 0% | 0% | 0% | 0% | 0% |

## Business plan inputs for cfo.ai

Measured by `uv run linus economics` over 8 cold runs, 110 PRs (`evals/economics.json`):
- Model cost per PR: **$0.00086** (JEV $0.000054, OpenAI $0.00080).
- Agent time 2.1 s per PR; 1.7 sandbox runs per PR.
- Projection at the default assumptions ($49/repo/month, $0.005 sandbox per PR, 10x model
  cost on real repos, $0.0136/PR): gross margin 97% at 100 PRs/month, 72% at 1,000; flat
  $49/repo breaks even at about 3,600 PRs/month (cfo.ai's Ari corrected our first
  "around 10,000" framing), so pricing has to scale with PR volume.

Assumptions to model (not measured yet):
- Real repositories send more context per oracle: model cost $0.01 to $0.05 per PR.
- Hosted sandbox compute: $0.002 to $0.01 per PR.
- Pricing options: a percentage of the pool processed (Algora reportedly
  charges 20%; verify before citing), or flat tiers (for example $49/month for
  one repo up to 100 PRs, $199/month for five repos).
- Scenarios: base and growth customer counts, team cost, runway.

## Demo

- Live repo: https://github.com/VARUN3WARE/textkit-bounty (verdicts on every PR,
  leaderboard in issue #19)
- Video: _pending_
- `make judge`: builds the sandbox, runs unit tests, runs the close on the demo
  month and scores it.

## Known limitations

- The demo month is small and self-made; it shows the mechanisms, not
  generalization.
- Python + pytest projects only; projects that must be installed to run their tests are not
  supported yet.
- A red team found what it found; more attacks surely exist.
- Linus proposes payouts; it does not move money.

## Next steps

- Backtest on a real repository's past month.
- Run the red team continuously; every confirmed attack becomes an eval case.
- Run each oracle call in its own container for stronger isolation.
- Credit for reviews and issue reproductions, not just code.

## Submission checklist

- [ ] Web check-in done, team confirmed
- [x] Public repo with hackathon-period commits
- [ ] neatlogs traces: before and after screenshots
- [x] Entire checkpoints attached to commits
- [x] cfo.ai business plan included (BUSINESS_PLAN.md)
- [x] Failure recovery shown in a real run (v1 crash, resumed from checkpoints)
- [x] Live demo on GitHub with real PRs and posted verdicts
- [x] Before/after evidence for two rounds of attacks (eval rows + trace ids)
- [ ] Demo video, 3 minutes or less
- [ ] Building-in-public posts tagged #neatHack
- [ ] X quote post published, Oct 12, 7:00 to 11:55 PM IST
