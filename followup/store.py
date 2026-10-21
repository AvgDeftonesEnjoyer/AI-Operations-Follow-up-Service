"""SQLite mock delivery server and audit log. Safe across repeated processes."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, filename: str):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(filename, timeout=10)
        self.connection.execute("PRAGMA busy_timeout = 10000")
        with self.connection:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    operation_id TEXT PRIMARY KEY,
                    action_json TEXT NOT NULL,
                    delivered_at TEXT NOT NULL
                )
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    operation_id TEXT,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL
                )
            """)

    def close(self):
        self.connection.close()

    def get_delivery(self, operation_id: str):
        row = self.connection.execute(
            "SELECT action_json FROM deliveries WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_delivery(self, operation_id: str, action: dict):
        """Simulated remote server: a replayed operation never creates two sends."""
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
                (operation_id, json.dumps(action, ensure_ascii=False), now_utc()),
            )
        return cursor.rowcount == 1

    def log(self, event: str, operation_id: str | None = None, detail: str = ""):
        with self.connection:
            self.connection.execute(
                "INSERT INTO events (timestamp, operation_id, event, detail) VALUES (?, ?, ?, ?)",
                (now_utc(), operation_id, event, detail),
            )

    def deliveries(self):
        rows = self.connection.execute(
            "SELECT operation_id, action_json, delivered_at FROM deliveries ORDER BY rowid"
        ).fetchall()
        return [
            {"operation_id": op, "action": json.loads(body), "delivered_at": at}
            for op, body, at in rows
        ]

    def events(self):
        rows = self.connection.execute(
            "SELECT timestamp, operation_id, event, detail FROM events ORDER BY id"
        ).fetchall()
        return [
            {"timestamp": at, "operation_id": op, "event": event, "detail": detail}
            for at, op, event, detail in rows
        ]
