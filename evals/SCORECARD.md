# Linus eval scorecard

Every row is a real run of `linus eval` against the demo bounty month (11 PRs, 6 of them attacks, through v4; 13 PRs, 8 of them attacks, from v5 on). Ground truth: `expected.json`, which the agent never sees.

| When (UTC) | Label | Accuracy | False-pay | Missed-good | Human-flag | JEV $ | OpenAI tokens | Agent s/PR | Run | Trace |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2026-10-11 08:37 | v1 (crashed in settle, resumed from checkpoint) | 100% | 0% | 0% | 0% | $0.00049 | 5,191 | 2.4 | `2026-09-20261011-083603` | - |
| 2026-10-11 08:38 | v2 (recalibrated points, evidence in reasons, cold oracles) | 91% | 0% | 20% | 0% | $0.00049 | 5,327 | 2.3 | `2026-09-20261011-083831` | - |
| 2026-10-11 08:41 | v3 (JEV audits every generated test case) | 91% | 0% | 20% | 0% | $0.00067 | 6,199 | 2.6 | `2026-09-20261011-084057` | - |
| 2026-10-11 08:42 | v4 (JSON-rendered cases, reject tests that fail for the wrong reason) | 100% | 0% | 0% | 0% | $0.00068 | 6,241 | 2.6 | `2026-09-20261011-084212` | - |
| 2026-10-11 08:43 | v4 repeat 2 (stability) | 100% | 0% | 0% | 0% | $0.00069 | 6,313 | 2.8 | `2026-09-20261011-084253` | - |
| 2026-10-11 08:43 | v4 repeat 3 (stability) | 100% | 0% | 0% | 0% | $0.00068 | 6,038 | 2.5 | `2026-09-20261011-084327` | - |
| 2026-10-11 08:46 | v4 + per-PR cost tracking | 100% | 0% | 0% | 0% | $0.00068 | 6,284 | 2.5 | `2026-09-20261011-084630` | - |
| 2026-10-11 08:47 | v4 cold run, per-PR cost tracked | 100% | 0% | 0% | 0% | $0.00069 | 6,122 | 2.2 | `2026-09-20261011-084734` | - |
