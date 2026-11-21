"""SQLite history of completed experiments.

Only fully computed results are written, in one transaction, so an interrupted run
never appears as a completed one. The schema is created on first open.
"""
import json
import sqlite3


class ExperimentStore:
    def __init__(self, path):
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS experiments ("
                "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, scenario_id TEXT NOT NULL, "
                "seed INTEGER NOT NULL, result_json TEXT NOT NULL)")

    def save(self, result):
        with self._db:
            self._db.execute("INSERT INTO experiments VALUES (?, ?, ?, ?, ?)",
                             (result["id"], result["created_at"], result["config"]["scenario_id"],
                              result["config"]["seed"], json.dumps(result)))

    def get(self, exp_id):
        row = self._db.execute("SELECT result_json FROM experiments WHERE id = ?", (exp_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_summaries(self, limit=50, offset=0):
        rows = self._db.execute("SELECT result_json FROM experiments ORDER BY created_at DESC, id DESC "
                                "LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        out = []
        for (blob,) in rows:
            r = json.loads(blob)
            out.append({"id": r["id"], "created_at": r["created_at"], "scenario": r["scenario"],
                        "seed": r["config"]["seed"], "totals": r["totals"]})
        return out

    def close(self):
        self._db.close()
