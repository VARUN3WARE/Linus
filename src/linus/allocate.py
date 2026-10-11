"""Turns points into money, in integer cents, then checks hard invariants.

Shares are proportional to points. Nobody gets more than the per-contributor
cap; the excess rolls over to next month rather than inflating anyone else's
share. Shares below the minimum are dropped and the rest recomputed.
"""

from dataclasses import dataclass, field


@dataclass
class Allocation:
    currency: str
    budget_cents: int
    payouts: dict[str, int] = field(default_factory=dict)
    rollover_cents: int = 0
    dropped: list[str] = field(default_factory=list)

    @property
    def paid_cents(self) -> int:
        return sum(self.payouts.values())


def _shares(points: dict[str, float], total: int, cap: int) -> dict[str, float]:
    weight = sum(points.values())
    return {c: min(total * p / weight, float(cap)) for c, p in points.items()}


def _round(shares: dict[str, float], cap: int) -> dict[str, int]:
    """Largest-remainder rounding: cents sum to round(sum(shares)), none above cap."""
    floors = {c: int(v) for c, v in shares.items()}
    leftover = round(sum(shares.values())) - sum(floors.values())
    order = sorted(shares, key=lambda c: (shares[c] - floors[c], c), reverse=True)
    for c in order:
        if leftover <= 0:
            break
        if floors[c] < cap:
            floors[c] += 1
            leftover -= 1
    return floors


def allocate(points: dict[str, float], currency: str, budget: float,
             cap_fraction: float, min_payout: float | None) -> Allocation:
    budget_cents = round(budget * 100)
    cap = int(budget_cents * cap_fraction)
    active = {c: p for c, p in points.items() if p > 0}
    alloc = Allocation(currency, budget_cents)
    while active:
        shares = _shares(active, budget_cents, cap)
        if min_payout is None:
            break
        below = [c for c, v in shares.items() if v < min_payout * 100]
        if not below:
            break
        smallest = min(below, key=lambda c: shares[c])
        alloc.dropped.append(smallest)
        del active[smallest]
    else:
        shares = {}
    alloc.payouts = {c: v for c, v in _round(shares, cap).items() if v > 0}
    alloc.rollover_cents = budget_cents - alloc.paid_cents
    return alloc


def check_invariants(allocations: list[Allocation], cap_fraction: float, min_payout: float,
                     primary: str, paid_prs: dict[str, list[int]], verified_prs: set[int],
                     already_paid: set[int], excluded: set[str]) -> list[str]:
    """Every rule a payout must satisfy. Any violation halts the run."""
    problems = []
    for a in allocations:
        if a.paid_cents + a.rollover_cents != a.budget_cents:
            problems.append(f"{a.currency}: payouts + rollover != budget")
        if a.rollover_cents < 0:
            problems.append(f"{a.currency}: paid more than the budget")
        cap = int(a.budget_cents * cap_fraction)
        for who, cents in a.payouts.items():
            if cents > cap:
                problems.append(f"{a.currency}: {who} above the per-contributor cap")
            if a.currency == primary and cents < min_payout * 100:
                problems.append(f"{a.currency}: {who} below the minimum payout")
            if who in excluded:
                problems.append(f"{who} is excluded but was paid")
            prs = paid_prs.get(who, [])
            if not prs:
                problems.append(f"{who} paid without any PR")
            for n in prs:
                if n not in verified_prs:
                    problems.append(f"#{n} paid without a verified receipt")
                if n in already_paid:
                    problems.append(f"#{n} was already paid in an earlier approved run")
    return problems
