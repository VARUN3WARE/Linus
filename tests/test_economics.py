"""`linus economics` and the eval's mapping onto a published demo, offline."""

import csv
import json

import pytest

from linus import economics, evals
from linus.store import Store


def _eval_result(directory, run_id, prs, jev, openai, seconds, cold=None):
    result = {"run_id": run_id, "label": run_id, "jev_usd": jev, "openai_usd": openai,
              "seconds_per_pr": seconds, "rows": [{}] * prs}
    if cold is not None:
        result["cold"] = cold
    (directory / "results").mkdir(parents=True, exist_ok=True)
    (directory / "results" / f"{run_id}.json").write_text(json.dumps(result))


def _stored_run(store, run_id, prs, items, cold=False):
    store.create_run(run_id, "o/r", "2026-10")
    for n, data in items.items():
        store.save_item(run_id, n, "done", "pay", data)
    store.finish_run(run_id, "proposed", {"repo": "o/r", "cold": cold, "cost": {
        "prs": prs, "jev_usd": 0.001, "openai_usd": 0.0, "pr_seconds": 4.0}})


ORACLE = {"issue": 1, "test_sha256": "a", "attempts": [{"base_outcome": "error"}, {"base_outcome": "fail"}]}


def test_sandbox_runs_count_each_oracle_build_once():
    items = {1: {"data": {"oracle": ORACLE, "verification": {"runs": [{}, {}]}}},
             2: {"data": {"oracle": ORACLE, "verification": {"runs": [{}]}}},
             3: {"data": {}},
             4: {"data": {"oracle": {"issue": 2, "test_sha256": "b", "attempts": 3}}}}  # before v5: a count
    assert economics.sandbox_runs(items) == 2 + 1 + 2 + 3


def test_measured_costs_come_from_cold_runs(tmp_path):
    evals_dir = tmp_path / "evals"
    _eval_result(evals_dir, "cold-1", 10, 0.001, 0.010, 2.0)
    _eval_result(evals_dir, "cold-2", 10, 0.003, 0.010, 4.0, cold=True)
    _eval_result(evals_dir, "old", 10, 0.001, None, 2.0)  # from before OpenAI dollars were tracked
    store = Store(tmp_path / "linus.db")
    _stored_run(store, "warm", 2, {1: {"oracle": ORACLE, "verification": {"runs": [{}, {}]}},
                                   2: {"verification": {"runs": [{}]}}})

    result = economics.build(evals_dir, store, 49, 0.005, 10)
    m = result["measured"]
    assert m["basis"] == "cold runs" and m["cold_runs_used"] == 2 and m["prs_measured"] == 20
    assert m["jev_usd_per_pr"] == pytest.approx(0.0002)
    assert m["openai_usd_per_pr"] == pytest.approx(0.001)
    assert m["model_usd_per_pr"] == pytest.approx(0.0012)
    assert m["jev_usd_per_run"] == pytest.approx(0.002) and m["openai_usd_per_run"] == pytest.approx(0.01)
    assert m["agent_seconds_per_pr"] == pytest.approx(3.0)
    assert m["sandbox_runs_per_pr"] == pytest.approx(5 / 2)  # 3 verification runs + 2 oracle attempts, 2 PRs
    warm = next(r for r in result["runs"] if r["run_id"] == "warm")
    assert warm["from"] == "store" and warm["cold"] is False

    row = next(r for r in result["projection"]["rows"] if r["prs_per_month"] == 1000)
    assert row["model_usd"] == pytest.approx(0.0012 * 10 * 1000)
    assert row["sandbox_usd"] == pytest.approx(5.0)
    assert row["cost_usd"] == pytest.approx(17.0)
    assert row["gross_margin"] == pytest.approx((49 - 17) / 49, abs=1e-4)
    assert row["agent_hours"] == pytest.approx(3000 / 3600, abs=0.01) and row["sandbox_runs"] == 2500


def test_eval_and_store_rows_for_the_same_run_merge(tmp_path):
    evals_dir = tmp_path / "evals"
    _eval_result(evals_dir, "r1", 2, 0.001, 0.002, 2.0)
    store = Store(tmp_path / "linus.db")
    _stored_run(store, "r1", 2, {1: {"verification": {"runs": [{}]}}})
    [row] = economics.collect(evals_dir, store)
    assert row["from"] == "eval+store" and row["cold"] is True and row["sandbox_runs"] == 1


def test_outputs_label_measured_and_assumed_values(tmp_path):
    evals_dir = tmp_path / "evals"
    _eval_result(evals_dir, "r1", 4, 0.001, 0.002, 2.0)
    result = economics.build(evals_dir, None, 49, 0.005, 10)
    json_path, csv_path = economics.write(result, tmp_path)
    saved = json.loads(json_path.read_text())
    assert saved["assumed"] == {"price_per_repo_usd_month": 49, "sandbox_usd_per_pr": 0.005,
                                "real_repo_multiplier": 10}
    assert [r["prs_per_month"] for r in saved["projection"]["rows"]] == [100, 1000, 10000]
    rows = list(csv.DictReader(csv_path.open()))
    bases = {(r["basis"], r["metric"]) for r in rows}
    assert ("measured", "model_usd_per_pr") in bases and ("assumed", "price_per_repo_usd_month") in bases
    assert ("projected", "gross_margin") in bases


def test_eval_maps_published_prs_to_ground_truth_by_branch():
    expected = {"101": {"verdict": "pay", "why": "a"}, "102": {"verdict": "reject", "why": "b"}}
    summary = {"verdicts": {6: {"branch": "pr/101"}, 7: {"branch": "pr/102"},
                            8: {"branch": "someone/try-to-fool-it"}, 9: {"branch": "101"}}}
    assert evals.by_branch(expected, summary) == {"6": expected["101"], "7": expected["102"]}
