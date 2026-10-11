# Linus

**Month-end close for open-source bounties. Linus pays only for proven work.**

Bounty programs attract AI slop: plausible-looking PRs that don't fix anything
(curl shut its bounty down in January 2026 for exactly this reason). Linus is an
agent that takes a month of PRs and a fixed budget and produces a payout ledger
where every line is backed by reproducible evidence:

1. Plans a queue of every PR in the period and saves its state per PR.
2. Runs cheap gates (CI, issue linkage, prompt-injection scan).
3. Verifies survivors in Docker: a reproduction test must fail on the base
   branch and pass on the PR branch, and tests must not be deleted or gamed.
4. Scores each PR with evidence, then checks for gaming (split PRs, duplicates).
5. Allocates the budget with a fixed formula and hard invariants.
6. Writes a receipt per payout to a tamper-evident, hash-chained ledger.
7. Hands the ledger to the maintainer to approve, edit, or dispute.

Built for [neatHack](https://neatlogs.com/hackathon) with neatlogs (tracing),
Entire (agent session checkpoints) and cfo.ai (business plan).

## Setup

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
cp .env.example .env   # then fill in the keys
uv run linus doctor    # checks keys, APIs, tracing, Docker and Entire
```

Rules for a bounty pool live in [`bounty.yml`](bounty.yml).

## Status

Work in progress during neatHack (October 10–12, 2026). See
[`hackathon.md`](hackathon.md) for the build log.
