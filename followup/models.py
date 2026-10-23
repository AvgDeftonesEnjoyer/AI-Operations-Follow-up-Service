"""Small data objects and input normalization."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
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
NO_OWNER = {"tbd", "tba", "tbc", "n/a", "na", "none", "null", "unknown", "pending", "vacant",
            "nobody", "open", "-", "?"}
# Phrases, not names: "Peter Noone" or "Tbc Ltd" are people (TBD alone is in NO_OWNER).
NO_OWNER_PHRASE = re.compile(
    r"\b(unassigned|no owner|no one|no assignee|needs? (?:an )?owner|unknown owner|none assigned|"
    r"not (?:yet )?assigned|to (?:be )?assign(?:ed)?|(?:pending|awaiting) assignment|"
    r"to be (?:determined|confirmed))\b"
)


def clean(data, field):
    """Convert an optional field to text with single spaces, or None.

    Numbers become text ("task_id": 1002 -> "1002"); lists, objects and booleans are refused.
    Errors name the field, never its value: fixtures may hold private data.
    """
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    text = " ".join(value.split())  # "John  Smith" and "John Smith" are the same owner.
    if len(text) > 4000:
        raise ValueError(f"{field} must not exceed 4000 characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate is valid JSON but would fail later, mid-batch, when stored.
        raise ValueError(f"{field} contains invalid Unicode") from None
    return text or None


def parse_date(value):
    """YYYY-MM-DD, or an ISO timestamp (taken in UTC when it has an offset). Invalid stays unknown."""
    pattern = r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?"
    try:
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            return None
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (moment.astimezone(timezone.utc) if moment.tzinfo else moment).date()
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

    def state_id(self):
        """Key for the notes classification: new note text (or a new Blocked status) is classified anew."""
        return digest({"task_id": self.task_id, "notes": self.notes_with_status})

    @property
    def notes_with_status(self):
        """What the classifier reads: a Blocked status is itself a blocker cue for the notes."""
        return f"Blocked: {self.notes}" if self.status == "Blocked" and self.notes else self.notes

    @property
    def heading(self):
        """Task ID, title and project: how every message names the task."""
        text = f"{self.task_id} '{self.title}'"
        return f"{text} ({self.project})" if self.project else text

    @property
    def due_key(self):
        """The deadline as one spelling: "2026-10-18" and "2026-10-18T00:00:00Z" are the same."""
        return self.due_date.isoformat() if self.due_date else self.due_raw

    def operation_id(self, action):
        """One ID per task, action, recipient and request facts: never the same request twice.

        Built from the facts (action.key), not the wording, so renaming the task, editing notes,
        touching last_update, another spelling of the owner, or editing a message template
        never resends.
        """
        return digest({
            "task_id": self.task_id, "decision": action.decision,
            "target": (action.target or "").casefold(), "key": action.key.casefold(),
        })


def digest(fields: dict) -> str:
    raw = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Action:
    task_id: str
    decision: str
    target: str | None
    reason: str
    message: str | None
    operation_id: str | None = None
    key: str = ""  # The facts of the request (not its wording); see Task.operation_id.

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
