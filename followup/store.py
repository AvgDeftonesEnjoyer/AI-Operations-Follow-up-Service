"""SQLite mock delivery server and audit log. Safe across repeated processes."""

from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())  # Silent unless the CLI configures logging.
WARNING_EVENTS = {"read_retry", "send_retry", "send_failed", "send_deferred", "lookup_retry",
                  "lookup_failed", "llm_fallback", "run_failed"}


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
                CREATE TABLE IF NOT EXISTS classifications (
                    state_id TEXT PRIMARY KEY,
                    blocker TEXT NOT NULL CHECK (blocker IN ('none', 'materials', 'upstream'))
                )
            """)
            # The service's own record of confirmed actions, independent of the provider.
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS actions (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    target TEXT,
                    due_date TEXT,
                    as_of TEXT NOT NULL
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

    def get_classification(self, state_id: str):
        row = self.connection.execute(
            "SELECT blocker FROM classifications WHERE state_id = ?", (state_id,)
        ).fetchone()
        return row[0] if row else None

    def save_classification(self, state_id: str, blocker: str):
        # Concurrent workers must use the same winning classification.
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO classifications VALUES (?, ?)", (state_id, blocker)
            )
        return self.get_classification(state_id)

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

    def record_action(self, action, due_date: str | None, as_of: date):
        """Remember a confirmed delivery; the first date wins. Targets are kept casefolded."""
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO actions VALUES (?, ?, ?, ?, ?, ?)",
                (action.operation_id, action.task_id, action.decision,
                 (action.target or "").casefold(), due_date, as_of.isoformat()),
            )

    def owner_asked_on(self, task_id: str, owner: str | None, due_date: str | None):
        """The date this owner was first asked after this deadline passed, or None.

        A follow-up or a clarification counts; a question from before the deadline does not.
        A new owner or a moved deadline starts over with a fresh question.
        """
        row = self.connection.execute(
            "SELECT MIN(as_of) FROM actions WHERE task_id = ? "
            "AND decision IN ('follow_up_owner', 'clarify') AND target = ? AND due_date = ? "
            "AND as_of > due_date",
            (task_id, (owner or "").casefold(), due_date),
        ).fetchone()
        return date.fromisoformat(row[0]) if row[0] else None

    def log(self, event: str, operation_id: str | None = None, detail: str = ""):
        """Every audit event goes to SQLite and to the application log."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO events (timestamp, operation_id, event, detail) VALUES (?, ?, ?, ?)",
                (now_utc(), operation_id, event, detail),
            )
        level = logging.WARNING if event in WARNING_EVENTS else logging.INFO
        logger.log(level, "%s op=%s %s", event, operation_id or "-", detail)

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
