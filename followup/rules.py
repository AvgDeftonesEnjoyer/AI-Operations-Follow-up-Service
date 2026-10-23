"""Business rules. AI can label notes, but it never changes task facts."""

from datetime import date

from .models import Action, Task
from .notes import warehouse_dependency


ESCALATE_AFTER_DAYS = 3  # Days the owner has to answer a follow-up before escalation.
COORDINATION_QUEUE = "coordination_queue"  # A role, not an invented person.


def about(task: Task) -> str:
    """Task ID, title and project, so the recipient needs no other context."""
    text = f"{task.task_id} '{task.title}'"
    return f"{text} ({task.project})" if task.project else text


def decide(task: Task, today: date, blocker: str = "none",
           owner_asked_on: date | None = None) -> Action:
    """Choose one safe action. Missing facts take priority over reminders.

    owner_asked_on is the date this owner first got a follow-up for this deadline, if ever.
    Messages contain no day counts, so the same task state always gets the same text.
    """
    if task.status == "Done":
        return Action(task.task_id, "no_action", None, "Task is completed", None)

    if not task.owner:
        message = f"{about(task)} has no owner. Please assign one."
        reason = "Missing task owner"
        if not task.due_date:
            message += " Please also confirm its due date."
            reason = "Missing owner and invalid due date" if task.due_raw else "Missing owner and due date"
        return Action(task.task_id, "clarify", COORDINATION_QUEUE, reason, message)

    if not task.due_date:
        issue = "Invalid due date" if task.due_raw else "Missing due date"
        return Action(
            task.task_id, "clarify", task.owner, issue,
            f"{about(task)} has no valid due date. Please confirm it.",
        )

    if task.status == "Unknown":
        return Action(
            task.task_id, "clarify", task.owner, "Unrecognized task status",
            f"{about(task)} has an unrecognized status. Please confirm its current status.",
        )

    due = task.due_date.isoformat()
    overdue = today > task.due_date
    # A dependency mentioned in notes needs action only once the task is Blocked or overdue.
    needs_attention = task.status == "Blocked" or overdue

    if blocker == "materials" and needs_attention:
        # The warehouse is asked only when the notes name it; otherwise the owner chases the ETA.
        target = "warehouse" if warehouse_dependency(task.notes) else task.owner
        return Action(
            task.task_id, "request_eta", target, "Materials dependency",
            f"{about(task)} is waiting for materials (due {due}). Please confirm the delivery ETA.",
        )

    if blocker == "upstream" and needs_attention:
        # The owner cannot unblock work that waits on another team: coordination can.
        return Action(
            task.task_id, "escalate", COORDINATION_QUEUE, "Upstream dependency",
            f"{about(task)} is blocked (owner: {task.owner}, due {due}). "
            "Please help unblock it and agree an ETA with the owner.",
        )

    if task.status == "Blocked":
        # Blocked with no known cause: ask the owner rather than escalate a guess.
        return Action(
            task.task_id, "clarify", task.owner, "Blocker not explained",
            f"{about(task)} is marked Blocked (due {due}). Please say what is blocking it and give an ETA.",
        )

    if overdue and owner_asked_on:
        # The owner was asked already. An update since then (even the same day) is their answer.
        updated = date.fromisoformat(task.last_update) if task.last_update else None
        replied = updated is not None and updated >= owner_asked_on
        quiet_since = updated if replied else owner_asked_on
        if (today - quiet_since).days >= ESCALATE_AFTER_DAYS:
            asked = owner_asked_on.isoformat()
            return Action(
                task.task_id, "escalate", COORDINATION_QUEUE,
                f"Overdue since {due}; owner asked on {asked}",
                f"{about(task)} has been overdue since {due} (owner: {task.owner}); the owner was "
                f"asked on {asked}. Please agree a recovery plan and ETA with the owner.",
            )
        if replied:
            return Action(task.task_id, "no_action", None,
                          f"Owner updated the task on {updated.isoformat()}", None)

    if overdue:
        return Action(
            task.task_id, "follow_up_owner", task.owner, f"Overdue since {due}",
            f"{about(task)} was due on {due}. Please share the status and an updated ETA.",
        )

    return Action(task.task_id, "no_action", None, "No action required", None)
