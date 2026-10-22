"""Small data objects and input normalization."""

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re


def clean(value):
    """Convert an optional text field to a stripped string or None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Task fields must be strings or null")
    text = value.strip()
    if len(text) > 4000:
        raise ValueError("Task fields must not exceed 4000 characters")
    return text or None


def parse_date(value):
    """Invalid dates stay unknown. We never guess a deadline."""
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Task:
    task_id: str
    project: str
    title: str
    owner: str | None
    due_date: date | None
    due_raw: str | None
    status: str
    last_update: str | None
    notes: str

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Each task must be a JSON object")
        task_id = clean(data.get("task_id"))
        if not task_id:
            raise ValueError("Task without task_id: refusing an unsafe send")
        due_raw = clean(data.get("due_date"))
        status = (clean(data.get("status")) or "").casefold()
        statuses = {
            "done": "Done",
            "blocked": "Blocked",
            "working on it": "Working on it",
            "not started": "Not Started",
        }
        updated = parse_date(clean(data.get("last_update")))
        return cls(
            task_id=task_id.upper(),
            project=clean(data.get("project")) or "",
            title=clean(data.get("task")) or "Untitled task",
            owner=clean(data.get("assigned_to")),
            due_date=parse_date(due_raw),
            due_raw=due_raw,
            status=statuses.get(status, "Unknown"),
            last_update=updated.isoformat() if updated else None,
            notes=clean(data.get("notes")) or "",
        )

    def fingerprint(self, decision, target):
        """Same task + same state + same action always gets the same ID."""
        state = {
            "task_id": self.task_id,
            "project": self.project,
            "title": self.title,
            "owner": self.owner,
            "due_date": self.due_raw,
            "status": self.status,
            "last_update": self.last_update,
            "notes": self.notes,
            "decision": decision,
            "target": target,
        }
        raw = json.dumps(state, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Action:
    task_id: str
    decision: str
    target: str | None
    reason: str
    message: str | None
    operation_id: str | None = None

    def to_dict(self, delivery_status):
        return {
            "task_id": self.task_id,
            "decision": self.decision,
            "target": self.target,
            "reason": self.reason,
            "message": self.message,
            "operation_id": self.operation_id,
            "delivery_status": delivery_status,
        }
