import json

from linus import ledger


def test_chain_detects_edits(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, [{"type": "payout", "amount": 1}, {"type": "payout", "amount": 2}])
    ledger.append(path, [{"type": "approval"}])
    assert ledger.verify_chain(path) == []

    lines = path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["amount"] = 200
    lines[1] = json.dumps(entry, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    assert ledger.verify_chain(path) == ["entry 2: contents were modified (hash mismatch)"]


def test_rewriting_an_entry_with_a_fresh_hash_breaks_the_next_link(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, [{"type": "payout", "amount": 1}, {"type": "payout", "amount": 2}])
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["amount"] = 100
    first["hash"] = ledger.sha256({k: v for k, v in first.items() if k != "hash"})
    lines[0] = json.dumps(first, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    assert ledger.verify_chain(path) == ["entry 2: prev hash does not match entry 1"]
