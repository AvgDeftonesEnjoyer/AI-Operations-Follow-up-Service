"""Business rules. AI can label notes, but it never changes task facts."""

from datetime import date

from .models import Action, Task
from .notes import warehouse_dependency


ESCALATE_AFTER_DAYS = 7
COORDINATION_QUEUE = "coordination_queue"  # A role, not an invented person.


def decide(task: Task, today: date, blocker: str = "none") -> Action:
    """Choose one safe action. Missing facts take priority over reminders."""
    if task.status == "Done":
        return Action(task.task_id, "no_action", None, "Task is completed", None)

    if not task.owner:
        message = f"Please assign an owner to '{task.title}'."
        reason = "Missing task owner"
        if not task.due_date:
            message += " Please also confirm its due date."
            reason = "Missing owner and invalid due date" if task.due_raw else "Missing owner and due date"
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
        # A mention alone does not make the warehouse responsible.
        target = "warehouse" if warehouse_dependency(task.notes) else task.owner
        return Action(
            task.task_id, "request_eta", target, "Materials dependency",
            f"Please confirm the material delivery ETA for '{task.title}'.",
        )

    days_overdue = (today - task.due_date).days
    # Escalations go to the coordination role: the owner has already been asked directly.
    # A dependency mentioned in notes needs no escalation while the task is still on time.
    if task.status == "Blocked" or (blocker == "upstream" and days_overdue > 0):
        reason = "Upstream dependency" if blocker == "upstream" else "Task is blocked"
        return Action(
            task.task_id, "escalate", COORDINATION_QUEUE, reason,
            f"'{task.title}' (owner: {task.owner}) is blocked. "
            "Please help unblock it and agree an ETA with the owner.",
        )

    if days_overdue >= ESCALATE_AFTER_DAYS:
        return Action(
            task.task_id, "escalate", COORDINATION_QUEUE, f"Overdue by {days_overdue} days",
            f"'{task.title}' (owner: {task.owner}) was due on {task.due_date.isoformat()}. "
            "Please agree a recovery plan and ETA with the owner.",
        )

    if days_overdue > 0:
        days = "1 day" if days_overdue == 1 else f"{days_overdue} days"
        return Action(
            task.task_id, "follow_up_owner", task.owner, f"Overdue by {days}",
            f"'{task.title}' was due on {task.due_date.isoformat()}. Please share the status and updated ETA.",
        )

    return Action(task.task_id, "no_action", None, "No action required", None)
