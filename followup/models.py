"""Small data objects and input normalization."""

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re


# Common synonyms are accepted; anything else is "Unknown" and asks the owner to clarify.
STATUSES = {
    "done": "Done",
    "completed": "Done",
    "complete": "Done",
    "closed": "Done",
    "finished": "Done",
    "blocked": "Blocked",
    "working on it": "Working on it",
    "in progress": "Working on it",
    "not started": "Not Started",
}
# Placeholder text in the owner field means that nobody is assigned yet.
NO_OWNER = {"tbd", "tba", "n/a", "na", "none", "null", "unknown", "-", "?"}
NO_OWNER_PHRASE = re.compile(r"\b(unassigned|no owner|not (?:yet )?assigned|tb[ad])\b")


def clean(data, field):
    """Convert an optional text field to a stripped string or None.

    Errors name the field, never its value: fixtures may hold private data.
    """
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    text = value.strip()
    if len(text) > 4000:
        raise ValueError(f"{field} must not exceed 4000 characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate is valid JSON but would fail later, mid-batch, when stored.
        raise ValueError(f"{field} contains invalid Unicode") from None
    return text or None


def parse_date(value):
    """YYYY-MM-DD, or an ISO timestamp whose date part is used. Invalid dates stay unknown."""
    pattern = r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?"
    try:
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            return None
        return date.fromisoformat(value[:10])
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
        task_id = clean(data, "task_id")
        if not task_id:
            raise ValueError("Task without task_id: refusing an unsafe send")
        due_raw = clean(data, "due_date")
        # "In-Progress", "in_progress" and "In Progress" are the same status.
        status = re.sub(r"[-_\s]+", " ", (clean(data, "status") or "").casefold())
        owner = clean(data, "assigned_to")
        if owner and (owner.casefold() in NO_OWNER or NO_OWNER_PHRASE.search(owner.casefold())):
            owner = None
        updated = parse_date(clean(data, "last_update"))
        return cls(
            task_id=task_id.upper(),
            project=clean(data, "project") or "",
            title=clean(data, "task") or "Untitled task",
            owner=owner,
            due_date=parse_date(due_raw),
            due_raw=due_raw,
            status=STATUSES.get(status, "Unknown"),
            last_update=updated.isoformat() if updated else None,
            notes=clean(data, "notes") or "",
        )

    def fingerprint(self, decision, target):
        """Same task + same state + same action always gets the same ID.

        last_update is left out: touching a record without changing it must not resend.
        """
        state = {
            "task_id": self.task_id,
            "project": self.project,
            "title": self.title,
            "owner": self.owner,
            "due_date": self.due_raw,
            "status": self.status,
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
