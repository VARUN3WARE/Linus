"""Receipts and the tamper-evident ledger.

A receipt is canonical JSON of everything behind one PR's verdict; its
SHA-256 goes into the ledger. The ledger is append-only JSONL where each
entry carries the hash of the one before it, so editing any past entry breaks
every hash after it.
"""

import hashlib
import json
from pathlib import Path

GENESIS = "0" * 64


def canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()


def sha256(data: dict) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()


def write_receipt(directory: Path, number: int, receipt: dict) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"pr-{number}.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str))
    return path, sha256(receipt)


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append(path: Path, entries: list[dict]) -> list[dict]:
    existing = read(path)
    prev = existing[-1]["hash"] if existing else GENESIS
    seq = existing[-1]["seq"] + 1 if existing else 1
    written = []
    with path.open("a") as f:
        for entry in entries:
            body = {**entry, "seq": seq, "prev": prev}
            body["hash"] = sha256(body)
            f.write(json.dumps(body, sort_keys=True) + "\n")
            written.append(body)
            prev, seq = body["hash"], seq + 1
    return written


def verify_chain(path: Path) -> list[str]:
    problems = []
    prev = GENESIS
    for i, entry in enumerate(read(path), start=1):
        body = {k: v for k, v in entry.items() if k != "hash"}
        if entry.get("prev") != prev:
            problems.append(f"entry {i}: prev hash does not match entry {i - 1}")
        if sha256(body) != entry.get("hash"):
            problems.append(f"entry {i}: contents were modified (hash mismatch)")
        prev = entry.get("hash", "")
    return problems


def _cents(amount) -> int:
    return round(float(amount) * 100)


def _payout_problems(entry: dict, receipts_dir: Path) -> list[str]:
    """Each paid PR must have an unchanged receipt that says pay, for this author."""
    who, prs, hashes = entry.get("contributor"), entry.get("prs") or [], entry.get("receipts") or []
    if not prs or len(prs) != len(hashes):
        return [f"{who}: payout lists {len(prs)} PRs but {len(hashes)} receipts"]
    problems = []
    for number, digest in zip(prs, hashes):
        path = receipts_dir / f"pr-{number}.json"
        if not path.exists():
            problems.append(f"#{number}: no receipt in this run")
            continue
        receipt = json.loads(path.read_text())
        if sha256(receipt) != digest:
            problems.append(f"#{number}: receipt does not match the hash in the proposal")
        if (receipt.get("pr") or {}).get("author") != who:
            problems.append(f"#{number}: paid to {who} but authored by {(receipt.get('pr') or {}).get('author')}")
        if receipt.get("verdict") != "pay":
            problems.append(f"#{number}: receipt verdict is {receipt.get('verdict')}, not pay")
    return problems


def proposal_problems(raw: bytes, summary: dict, receipts_dir: Path) -> list[str]:
    """Why a proposed ledger must not be appended, if anything. It must be the
    exact file the close wrote, and its entries must still hold the invariants."""
    expected = summary.get("proposal_sha256")
    if not expected:
        return ["the run has no recorded proposal hash; re-run the close"]
    if hashlib.sha256(raw).hexdigest() != expected:
        return ["ledger.proposed.jsonl changed since the close wrote it"]
    budgets = {a["currency"]: _cents(a["budget"]) for a in summary.get("allocations", [])}
    totals = dict.fromkeys(budgets, 0)
    problems = []
    for entry in (json.loads(line) for line in raw.decode().splitlines() if line.strip()):
        if (entry.get("run_id"), entry.get("repo")) != (summary.get("run_id"), summary.get("repo")):
            problems.append(f"entry for run {entry.get('run_id')} / {entry.get('repo')} is not from this run")
        currency = entry.get("currency")
        if currency not in totals:
            problems.append(f"entry in {currency}, which is not in this run's budget")
            continue
        totals[currency] += _cents(entry.get("amount", 0))
        if entry.get("type") == "payout":
            problems += _payout_problems(entry, receipts_dir)
        elif entry.get("type") != "rollover":
            problems.append(f"unexpected entry type {entry.get('type')!r}")
    for currency, cents in budgets.items():
        if totals[currency] != cents:
            problems.append(f"{currency}: payouts + rollover = {totals[currency] / 100:.2f}, "
                            f"budget is {cents / 100:.2f}")
    return problems


def paid_prs(path: Path, repo: str) -> set[int]:
    return {n for e in read(path) if e.get("type") == "payout" and e.get("repo") == repo
            for n in e.get("prs", [])}
