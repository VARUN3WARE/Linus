# Linus at neatHack

## Problem

Open-source companies want outside contributors and are willing to pay
bounties, but money attracts AI slop. Maintainers can't afford to verify every
PR by hand, and splitting a monthly budget fairly is manual and contentious.

## What Linus does

An agent that runs the month-end close for a bounty pool: it verifies every PR
in the period with reproducible evidence, rejects or flags what it can't prove,
allocates a fixed budget with a deterministic formula, and writes a
tamper-evident ledger for the maintainer to approve.

## Team

- Varun ([@VARUN3WARE](https://github.com/VARUN3WARE))

## Tech stack

- Python 3.10+, uv, Typer
- LLMs: OpenAI and JEV (fast model for gates, strong model for verification)
- GitHub REST API, Docker for sandboxed test runs, SQLite for state
- neatlogs for tracing, Entire for coding-agent checkpoints, cfo.ai for the
  business plan

## Partner tool evidence

| Tool | How we use it | Evidence |
|---|---|---|
| neatlogs | Traces every LLM call, tool call and retry; before/after eval runs | _links to traces_ |
| Entire | Checkpoints for every coding-agent session that built Linus | _checkpoint links_ |
| cfo.ai | Pricing, cost per verified PR, margins, runway | _business plan link_ |

## Build log

### 2026-10-11

- Repo skeleton: config loading, neatlogs-wrapped LLM clients, `linus doctor`
  to check keys, APIs, tracing, Docker and Entire.

## Eval scorecard

_Filled in from real runs only._

| Metric | v1 | v2 |
|---|---|---|
| False-pay rate | | |
| Missed-good rate | | |
| Human-flag rate | | |
| Cost per PR | | |

## Demo

- Video: _pending_
- `make judge`: _pending_

## Known limitations

- _Fill in honestly before submission._

## Next steps

- _Pending_

## Submission checklist

- [ ] Web check-in done, team confirmed
- [ ] Public repo with hackathon-period commits
- [ ] neatlogs traces: before and after
- [ ] Entire checkpoints attached to commits
- [ ] cfo.ai business plan included
- [ ] Failure recovery shown in a real run
- [ ] Demo video, 3 minutes or less
- [ ] Building-in-public posts tagged #neatHack
- [ ] X quote post published, Oct 12, 7:00–11:55 PM IST
