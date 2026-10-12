"""`linus economics`: unit costs for the business plan, from real runs.

Measured values come from eval results (evals/results/*.json) and the run
summaries and per-PR items in the store. The headline is taken from cold runs,
where every oracle is generated from scratch, so it is the expensive case.
Projections multiply those measurements by assumptions that are passed in
explicitly and labelled as such in every output.
"""

import csv
import json
from pathlib import Path

from linus.jev import USD_PER_INPUT_TOKEN
from linus.llm import usd
from linus.store import Store

VOLUMES = (100, 1_000, 10_000)


def pr_cost_usd(cost: dict, openai_model: str) -> tuple[float, float | None]:
    """(JEV, OpenAI) dollars for one PR's recorded token counts; OpenAI is None
    when the model's price is unknown."""
    jev = cost.get("jev_tokens", 0) * USD_PER_INPUT_TOKEN
    return jev, usd(openai_model, cost.get("openai_in", 0), cost.get("openai_out", 0))


def sandbox_runs(items: dict) -> int:
    """Docker runs behind a run: every verification run, plus the base-code runs
    that built each distinct oracle (counted once per oracle, as on a cold run)."""
    count, oracles = 0, {}
    for item in items.values():
        data = item["data"]
        count += len((data.get("verification") or {}).get("runs") or [])
        if oracle := data.get("oracle"):
            oracles[(oracle.get("issue"), oracle.get("test_sha256"))] = oracle.get("attempts") or []
    for attempts in oracles.values():
        # Runs before v5 recorded only how many attempts there were, each one base run.
        count += attempts if isinstance(attempts, int) else sum(1 for a in attempts if a.get("base_outcome"))
    return count


def collect(evals_dir: Path, store: Store | None) -> list[dict]:
    """One row per run, merging an eval result with the stored summary of the same run."""
    runs: dict[str, dict] = {}
    for path in sorted(evals_dir.glob("results/*.json")):
        r = json.loads(path.read_text())
        prs = len(r.get("rows") or [])
        runs[r["run_id"]] = {
            "run_id": r["run_id"], "label": r.get("label", ""), "from": "eval", "repo": "",
            "cold": r.get("cold", True),  # `linus eval` always ran cold until --run existed
            "prs": prs, "jev_usd": r.get("jev_usd"), "openai_usd": r.get("openai_usd"),
            "agent_seconds": r["seconds_per_pr"] * prs if "seconds_per_pr" in r else None,
            "sandbox_runs": None,
        }
    for row in store.runs() if store else []:
        summary = json.loads(row["summary"] or "{}")
        cost = summary.get("cost")
        if row["status"] == "running" or not cost or not cost.get("prs"):
            continue
        run = runs.setdefault(row["id"], {"run_id": row["id"], "label": "", "from": "store",
                                          "cold": summary.get("cold")})
        if run["from"] == "eval":
            run["from"] = "eval+store"
        run |= {"repo": summary.get("repo", ""), "prs": cost["prs"], "jev_usd": cost.get("jev_usd"),
                "openai_usd": cost.get("openai_usd"), "agent_seconds": cost.get("pr_seconds"),
                "sandbox_runs": sandbox_runs(store.items(row["id"]))}
    rows = sorted(runs.values(), key=lambda r: r["run_id"])
    for r in rows:
        prs = max(r["prs"], 1)
        r["jev_usd_per_pr"] = r["jev_usd"] / prs if r["jev_usd"] is not None else None
        r["openai_usd_per_pr"] = r["openai_usd"] / prs if r["openai_usd"] is not None else None
        r["agent_seconds_per_pr"] = r["agent_seconds"] / prs if r["agent_seconds"] is not None else None
        r["sandbox_runs_per_pr"] = r["sandbox_runs"] / prs if r["sandbox_runs"] is not None else None
    return rows


def _pooled(rows: list[dict], key: str) -> float | None:
    """Total of `key` over total PRs, for the runs that recorded it."""
    have = [r for r in rows if r.get(key) is not None and r["prs"]]
    return sum(r[key] for r in have) / sum(r["prs"] for r in have) if have else None


def measure(rows: list[dict]) -> dict:
    cold = [r for r in rows if r["cold"] and r["jev_usd"] is not None and r["openai_usd"] is not None]
    basis = cold or [r for r in rows if r["jev_usd"] is not None and r["openai_usd"] is not None]
    jev, openai = _pooled(basis, "jev_usd"), _pooled(basis, "openai_usd")
    return {
        "basis": "cold runs" if cold else "all runs (no cold run recorded both costs)",
        "cold_runs_used": len(cold),
        "runs_used": len(basis),
        "prs_measured": sum(r["prs"] for r in basis),
        "jev_usd_per_run": sum(r["jev_usd"] for r in basis) / len(basis) if basis else None,
        "openai_usd_per_run": sum(r["openai_usd"] for r in basis) / len(basis) if basis else None,
        "jev_usd_per_pr": jev,
        "openai_usd_per_pr": openai,
        "model_usd_per_pr": jev + openai if basis else None,
        "agent_seconds_per_pr": _pooled(basis, "agent_seconds"),
        "sandbox_runs_per_pr": _pooled(rows, "sandbox_runs"),
    }


def project(measured: dict, price_per_repo: float, sandbox_usd_per_pr: float, real_repo_multiplier: float,
            volumes=VOLUMES) -> list[dict]:
    """One repo with `volume` PRs a month, priced at `price_per_repo` a month."""
    rows = []
    model_per_pr = (measured["model_usd_per_pr"] or 0) * real_repo_multiplier
    for volume in volumes:
        model, sandbox = model_per_pr * volume, sandbox_usd_per_pr * volume
        cost = model + sandbox
        rows.append({
            "prs_per_month": volume, "model_usd": round(model, 4), "sandbox_usd": round(sandbox, 4),
            "cost_usd": round(cost, 4), "cost_per_pr_usd": round(cost / volume, 6),
            "price_usd": price_per_repo, "gross_margin": round((price_per_repo - cost) / price_per_repo, 4)
            if price_per_repo else None,
            "agent_hours": round((measured["agent_seconds_per_pr"] or 0) * volume / 3600, 2),
            "sandbox_runs": round((measured["sandbox_runs_per_pr"] or 0) * volume),
        })
    return rows


def build(evals_dir: Path, store: Store | None, price_per_repo: float, sandbox_usd_per_pr: float,
          real_repo_multiplier: float) -> dict:
    rows = collect(evals_dir, store)
    measured = measure(rows)
    assumptions = {"price_per_repo_usd_month": price_per_repo, "sandbox_usd_per_pr": sandbox_usd_per_pr,
                   "real_repo_multiplier": real_repo_multiplier}
    return {
        "measured": measured,
        "assumed": assumptions,
        "projection": {
            "note": "One repo with this many PRs a month. Model cost = measured model $/PR x real_repo_multiplier "
                    "(assumed); sandbox cost = sandbox_usd_per_pr (assumed); price = price_per_repo (assumed); "
                    "agent hours and sandbox runs scale the measured per-PR values.",
            "rows": project(measured, price_per_repo, sandbox_usd_per_pr, real_repo_multiplier),
        },
        "runs": rows,
    }


def write(result: dict, directory: Path) -> tuple[Path, Path]:
    """economics.json, plus economics.csv as basis,section,metric,value rows for a spreadsheet."""
    json_path, csv_path = directory / "economics.json", directory / "economics.csv"
    json_path.write_text(json.dumps(result, indent=2))
    with csv_path.open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["basis", "section", "metric", "value"])
        for key, value in result["measured"].items():
            out.writerow(["measured", "unit", key, value])
        for key, value in result["assumed"].items():
            out.writerow(["assumed", "unit", key, value])
        for row in result["projection"]["rows"]:
            for key, value in row.items():
                if key != "prs_per_month":
                    out.writerow(["projected", f"{row['prs_per_month']} PRs/month", key, value])
        for run in result["runs"]:
            for key in ("prs", "jev_usd_per_pr", "openai_usd_per_pr", "agent_seconds_per_pr", "sandbox_runs_per_pr"):
                out.writerow(["measured", f"run {run['run_id']}", key, run[key]])
    return json_path, csv_path
