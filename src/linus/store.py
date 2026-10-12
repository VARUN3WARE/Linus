"""SQLite state. Every PR's progress is saved the moment a stage finishes, so a
crashed run resumes where it stopped instead of re-verifying (or re-paying).
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created_at TEXT, repo TEXT, period TEXT,
    status TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS items (
    run_id TEXT, pr INTEGER, stage TEXT, verdict TEXT, data TEXT, updated_at TEXT,
    PRIMARY KEY (run_id, pr)
);
CREATE TABLE IF NOT EXISTS oracles (
    repo TEXT, issue INTEGER, base_sha TEXT, data TEXT,
    PRIMARY KEY (repo, issue, base_sha)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "oracle_format" not in columns:  # databases from before v6; their runs read as format NULL
            with self.db:
                self.db.execute("ALTER TABLE runs ADD COLUMN oracle_format INTEGER")

    def create_run(self, run_id: str, repo: str, period: str, oracle_format: int | None = None) -> None:
        """oracle_format: the oracle format the run's verdicts are reached under;
        a run can only be resumed by a Linus that judges the same way."""
        with self.db:
            self.db.execute("INSERT INTO runs (id, created_at, repo, period, status, summary, oracle_format) "
                            "VALUES (?, ?, ?, ?, 'running', '{}', ?)", (run_id, now(), repo, period, oracle_format))

    def approved_run(self, repo: str, period: str) -> str | None:
        row = self.db.execute("SELECT id FROM runs WHERE repo = ? AND period = ? AND status = 'approved' "
                              "ORDER BY created_at LIMIT 1", (repo, period)).fetchone()
        return row["id"] if row else None

    def run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY created_at DESC")]

    def latest_unfinished(self, repo: str, period: str) -> str | None:
        row = self.db.execute(
            "SELECT id FROM runs WHERE repo = ? AND period = ? AND status = 'running' "
            "ORDER BY created_at DESC LIMIT 1", (repo, period)).fetchone()
        return row["id"] if row else None

    def finish_run(self, run_id: str, status: str, summary: dict) -> None:
        with self.db:
            self.db.execute("UPDATE runs SET status = ?, summary = ? WHERE id = ?",
                            (status, json.dumps(summary, default=str), run_id))

    def item(self, run_id: str, pr: int) -> dict | None:
        row = self.db.execute("SELECT * FROM items WHERE run_id = ? AND pr = ?", (run_id, pr)).fetchone()
        if not row:
            return None
        return {"stage": row["stage"], "verdict": row["verdict"], "data": json.loads(row["data"])}

    def items(self, run_id: str) -> dict[int, dict]:
        rows = self.db.execute("SELECT * FROM items WHERE run_id = ?", (run_id,))
        return {r["pr"]: {"stage": r["stage"], "verdict": r["verdict"], "data": json.loads(r["data"])}
                for r in rows}

    def save_item(self, run_id: str, pr: int, stage: str, verdict: str | None, data: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (run_id, pr) DO UPDATE SET "
                "stage = excluded.stage, verdict = excluded.verdict, data = excluded.data, "
                "updated_at = excluded.updated_at",
                (run_id, pr, stage, verdict, json.dumps(data, default=str), now()))

    def oracle(self, repo: str, issue: int, base_sha: str) -> dict | None:
        row = self.db.execute("SELECT data FROM oracles WHERE repo = ? AND issue = ? AND base_sha = ?",
                              (repo, issue, base_sha)).fetchone()
        return json.loads(row["data"]) if row else None

    def save_oracle(self, repo: str, issue: int, base_sha: str, data: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO oracles VALUES (?, ?, ?, ?)",
                            (repo, issue, base_sha, json.dumps(data, default=str)))
