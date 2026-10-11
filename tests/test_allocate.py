from linus.allocate import allocate, check_invariants


def test_proportional_and_exact_to_the_cent():
    a = allocate({"a": 30, "b": 30, "c": 45, "d": 30, "e": 5}, "USD", 2000, 0.3, 10)
    assert a.paid_cents + a.rollover_cents == 200_000
    assert a.payouts["a"] == a.payouts["b"] == 42_857
    assert a.payouts["c"] == 60_000  # capped at 30%
    assert a.rollover_cents == 4_286


def test_cap_excess_rolls_over_instead_of_inflating_small_shares():
    a = allocate({"big": 100, "tiny": 1}, "USD", 2000, 0.3, 10)
    assert a.payouts == {"big": 60_000, "tiny": 1_980}
    assert a.rollover_cents == 200_000 - 61_980


def test_shares_below_minimum_are_dropped():
    a = allocate({"a": 300, "b": 1, "c": 1}, "USD", 1000, 0.9, 10)
    assert a.payouts == {"a": 90_000}
    assert sorted(a.dropped) == ["b", "c"]


def test_nobody_eligible_rolls_everything_over():
    a = allocate({}, "USD", 2000, 0.3, 10)
    assert a.payouts == {} and a.rollover_cents == 200_000


def test_invariants_catch_unverified_and_double_payouts():
    a = allocate({"a": 1}, "USD", 100, 1.0, 10)
    problems = check_invariants([a], 1.0, 10, "USD", {"a": [7]}, verified_prs=set(),
                                already_paid={7}, excluded=set())
    assert "#7 paid without a verified receipt" in problems
    assert "#7 was already paid in an earlier approved run" in problems
