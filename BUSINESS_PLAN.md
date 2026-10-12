# Linus business plan

Built in [cfo.ai](https://cfo.ai) on October 12, 2026 by Ari, cfo.ai's AI CFO,
from Linus's measured unit economics (`uv run linus economics`,
[`evals/economics.json`](evals/economics.json)). Planning only: no bank,
billing or GitHub data was used. The model lives in cfo.ai as three pages
(controls and simulator, operating plan, pricing and cash) with Main, Growth
and Downside scenarios, covering October 2026 to September 2028 in USD.

Ari's first finding corrected our own projection: at the assumed real-repo
cost of $0.0136 per PR, a flat $49 per repo breaks even at about 3,600 PRs a
month, not "around 10,000" as we had written.

## Recommendation

**$49 per repo per month including 100 verified-attempt PRs, then $0.25 per
extra PR.** It ties revenue to verification workload: flat pricing exposes
Linus to expensive repositories, and a pure bounty-pool fee exposes it to small
pools with heavy PR traffic. Bill in-scope verification attempts, not only
accepted fixes, because rejections cost money too, with spend caps, usage
alerts, abuse limits and clear rules for retries and duplicates. Payout
approval stays with the maintainer.

The commercial caveat: at 10,000 PRs a month the usage price is $2,524 per
repo. The margin works; whether maintainers will pay it is unproven.

## Unit economics

| Direct processing cost per PR | |
|---|---:|
| Model cost: measured $0.00086 × assumed 10× for real repos | $0.00860 |
| Hosted sandbox allowance (assumed; covers all 1.7 runs per PR) | $0.00500 |
| **Total** | **$0.01360** |

At the assumed launch workload (10 customers, one repo each, 300 PRs per
customer per month), gross cost is $22.08 per customer, or $0.0736 per PR,
including hosting and support.

| Pricing | Revenue / customer | Gross profit / customer | Gross margin |
|---|---:|---:|---:|
| Flat $49 | $49 | $26.92 | 54.9% |
| $49 + usage | $99 | $76.92 | 77.7% |
| 5% of an assumed $2,000 pool | $100 | $77.92 | 77.9% |

Break-even for flat $49: about 3,603 PRs per repo per month on direct
processing, about 3,382 including marginal hosting and support. At 10,000 PRs
per repo (shared hosting allocated across ten launch customers): flat loses
$105 per customer, the pool fee loses $54, usage pricing earns $2,370 gross
profit on $2,524 of revenue.

## Assumptions

All labelled and editable in the model; these are hypotheses, not traction.

- Two founders at $700 each per month, plus 10% payroll load.
- Hosting $150 per month plus $1 per customer; support $2 per customer per
  month; tooling $100 per month.
- No additional hires, financing, paid marketing or capital purchases.
- Every scenario starts with 10 customers and 300 PRs per customer per month.
- Monthly net customer growth: 5% Base, 12% Growth, 2% Downside. Monthly PR
  growth per customer: 3%, 5%, 1%.
- Starting cash: $50,000.

## Month 24, recommended usage pricing

| Scenario | Monthly revenue | Gross margin | Ending cash | First cash-positive month |
|---|---:|---:|---:|---|
| Base | $5,284 | 90.7% | $64,733 | August 2027 |
| Growth | $34,472 | 93.5% | $225,080 | March 2027 |
| Downside | $1,865 | 85.1% | $37,905 | not within 24 months |

Base starts at $871 monthly burn and reaches $3,154 monthly cash generation by
month 24, with a minimum cash balance of about $45,089. All three scenarios
stay funded through the 24 months on $50,000 of starting cash (runway beyond
the modelled horizon, not unlimited). Under flat pricing, Base ends with only
$24,730.

## Key risks

- **Production cost tails.** Demo averages don't establish costs for retries,
  long test suites or hostile repositories.
- **Security and payout liability.** A forged fix or a sandbox escape can cost
  far more than compute; this is why Linus is red-teamed (see the README).
- **Billing acceptance and abuse.** Define billable attempts, duplicates and
  retries; add spend caps and alerts.
- **Optimistic demand and capacity.** Growth assumes founder-led selling and no
  extra engineering hires.
- **Unbudgeted launch costs.** Payment rails, compliance, legal and insurance
  are excluded.

Ari checked every monthly financial output across the three scenarios against
independent arithmetic, checked the quarterly rollups, and verified that a cost
change moves cash without changing revenue. Next step it recommends: test usage
pricing against customer bill ceilings and heavier production-cost
assumptions, to see whether $0.25 per PR is commercially sustainable and not
just profitable on paper.
