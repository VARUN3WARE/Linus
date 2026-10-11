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
| neatlogs | One trace per close: workflow span, agent span per PR, JEV calls as LLM spans with tokens and cost, auto-captured OpenAI calls, Docker runs as tool spans, anti-gaming and invariant guardrails | Workflow `linus`. Cold v4 run: trace `d471472b34e4ee099f72c18f53fde268`. _Add dashboard screenshots of the v2 (failure) and v4 (fixed) runs._ |
| Entire | Enabled for the coding agent in this repo (`.entire/`, hooks in `.claude/settings.json`) | `entire checkpoint list`. _The first build session started before Entire was installed, so checkpoints begin with later sessions._ |
| cfo.ai | Pricing, cost per verified PR, margins, runway | _Business plan link pending; inputs below_ |
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

## Eval scorecard

See [`evals/SCORECARD.md`](evals/SCORECARD.md). Every row is a real run against
the demo month; ground truth is in `expected.json`, which the agent never sees.

| Metric | v1 | v2 | v3 | v4 (5 cold runs) |
|---|---|---|---|---|
| Accuracy | 100% | 91% | 91% | 100% |
| False-pay rate | 0% | 0% | 0% | 0% |
| Missed-good rate | 0% | 20% | 20% | 0% |
| Human-flag rate | 0% | 0% | 0% | 0% |
| Model cost per PR | n/a | n/a | n/a | $0.0010 |

## Business plan inputs for cfo.ai

Measured (demo month, cold runs):
- Model cost per PR: **$0.0010** (JEV ~$0.00006, OpenAI ~$0.0009).
- Agent time per PR: ~2.5 s, plus sandbox runs of ~1 s each.

Assumptions to model (not measured yet):
- Real repositories send more context per oracle: model cost $0.01 to $0.05 per PR.
- Hosted sandbox compute: $0.002 to $0.01 per PR.
- Pricing options: a percentage of the pool processed (Algora reportedly
  charges 20%; verify before citing), or flat tiers (for example $49/month for
  one repo up to 100 PRs, $199/month for five repos).
- Scenarios: base and growth customer counts, team cost, runway.

## Demo

- Video: _pending_
- `make judge`: builds the sandbox, runs unit tests, runs the close on the demo
  month and scores it.

## Known limitations

- The demo month is small and self-made; it shows the mechanisms, not
  generalization.
- Python + pytest projects only.
- PRs come from a local git repo plus a fixture; the read-only GitHub source is
  not built yet.
- Linus proposes payouts; it does not move money.

## Next steps

- Read-only GitHub source and a backtest on a real repository's past month.
- A red-team agent that writes new attack PRs; misses become eval cases.
- Credit for reviews and issue reproductions, not just code.

## Submission checklist

- [ ] Web check-in done, team confirmed
- [x] Public repo with hackathon-period commits
- [ ] neatlogs traces: before and after screenshots
- [ ] Entire checkpoints attached to commits
- [ ] cfo.ai business plan included
- [x] Failure recovery shown in a real run (v1 crash, resumed from checkpoints)
- [ ] Demo video, 3 minutes or less
- [ ] Building-in-public posts tagged #neatHack
- [ ] X quote post published, Oct 12, 7:00 to 11:55 PM IST
