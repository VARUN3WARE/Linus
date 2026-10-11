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


def paid_prs(path: Path, repo: str) -> set[int]:
    return {n for e in read(path) if e.get("type") == "payout" and e.get("repo") == repo
            for n in e.get("prs", [])}
