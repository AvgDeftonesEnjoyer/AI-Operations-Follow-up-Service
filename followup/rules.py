"""Business rules. AI can label notes, but it never changes task facts."""

from datetime import date
import re

from .models import Action, Task


ESCALATE_AFTER_DAYS = 7
COORDINATION_QUEUE = "coordination_queue"  # A role, not an invented person.


def decide(task: Task, today: date, blocker: str = "none") -> Action:
    """Choose one safe action. Missing facts take priority over reminders."""
    if task.status == "Done":
        return Action(task.task_id, "no_action", None, "Task is completed", None)

    if not task.owner:
        also_missing_date = not task.due_date
        message = f"Please assign an owner to '{task.title}'."
        if also_missing_date:
            message += " Please also confirm its due date."
        reason = "Missing owner and due date" if also_missing_date else "Missing task owner"
        return Action(task.task_id, "clarify", COORDINATION_QUEUE, reason, message)

    if not task.due_date:
        issue = "Invalid due date" if task.due_raw else "Missing due date"
        return Action(
            task.task_id, "clarify", task.owner, issue,
            f"Please confirm the due date for '{task.title}'.",
        )

    if task.status == "Unknown":
        return Action(
            task.task_id, "clarify", task.owner, "Unrecognized task status",
            f"Please confirm the current status of '{task.title}'.",
        )

    if blocker == "materials":
        # Route to warehouse only when it was explicitly named in the note.
        target = "warehouse" if re.search(r"\bwarehouse\b", task.notes, re.I) else task.owner
        return Action(
            task.task_id, "request_eta", target, "Materials dependency",
            f"Please confirm the material delivery ETA for '{task.title}'.",
        )

    if task.status == "Blocked" or blocker == "upstream":
        reason = "Upstream dependency" if blocker == "upstream" else "Task is blocked"
        return Action(
            task.task_id, "escalate", task.owner, reason,
            f"'{task.title}' is blocked. Please identify what is needed to unblock it and provide an ETA.",
        )

    days_overdue = (today - task.due_date).days
    if days_overdue >= ESCALATE_AFTER_DAYS:
        return Action(
            task.task_id, "escalate", task.owner, f"Overdue by {days_overdue} days",
            f"'{task.title}' was due on {task.due_date.isoformat()}. Please provide a recovery plan and ETA.",
        )

    if days_overdue > 0:
        return Action(
            task.task_id, "follow_up_owner", task.owner,
            f"Overdue by {days_overdue} days",
            f"'{task.title}' was due on {task.due_date.isoformat()}. Please share the status and updated ETA.",
        )

    return Action(task.task_id, "no_action", None, "No action required", None)
