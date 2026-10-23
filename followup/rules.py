"""Business rules. AI can label notes, but it never changes task facts."""

from datetime import date

from .models import Action, Task
from .notes import warehouse_dependency


ESCALATE_AFTER_DAYS = 3  # Days a chased task gets before coordination is asked to step in.
COORDINATION_QUEUE = "coordination_queue"  # A role, not an invented person.


def needs_attention(task: Task, today: date) -> bool:
    """A dependency in the notes matters only once the task is Blocked or overdue."""
    return task.status == "Blocked" or today > task.due_date


def decide(task: Task, today: date, blocker: str = "none",
           chased_on: date | None = None) -> Action:
    """Choose one safe action. Missing facts take priority over reminders.

    chased_on is when the owner or the warehouse was first asked after this deadline, if ever.
    Each action's key names the facts of the request, not its wording: the operation ID is
    built from it, so editing a message template never resends anything.
    """
    if task.status == "Done":
        return Action(task.task_id, "no_action", None, "Task is completed", None)

    if not task.owner:
        message = f"{task.heading} has no owner. Please assign one."
        reason = "Missing task owner"
        if not task.due_date:
            message += " Please also confirm its due date."
            reason = "Missing owner and invalid due date" if task.due_raw else "Missing owner and due date"
        return Action(task.task_id, "clarify", COORDINATION_QUEUE, reason, message,
                      key=f"no owner; due known: {bool(task.due_date)}")

    if not task.due_date:
        issue = "Invalid due date" if task.due_raw else "Missing due date"
        return Action(task.task_id, "clarify", task.owner, issue,
                      f"{task.heading} has no valid due date. Please confirm it.", key="no due date")

    if task.status == "Unknown":
        return Action(task.task_id, "clarify", task.owner, "Unrecognized task status",
                      f"{task.heading} has an unrecognized status. Please confirm its current status.",
                      key="unknown status")

    due = task.due_date.isoformat()
    overdue = today > task.due_date
    # Requests say "overdue since" once the deadline passes, so the recipient is asked again
    # then and the escalation clock starts from that request.
    when = f"overdue since {due}" if overdue else f"due {due}"

    # Chased after the deadline and still overdue days later: coordination steps in, whatever
    # the notes say now. An upstream wait already went to coordination (below), so it is not
    # escalated a second time.
    if (overdue and chased_on and blocker != "upstream"
            and (today - chased_on).days >= ESCALATE_AFTER_DAYS):
        chased = chased_on.isoformat()
        return Action(
            task.task_id, "escalate", COORDINATION_QUEUE, f"Overdue since {due}; chased on {chased}",
            f"{task.heading} has been overdue since {due} (owner: {task.owner}) and was first "
            f"chased on {chased}. Please agree a recovery plan and ETA with the owner.",
            key=f"overdue since {due}; chased on {chased}",
        )

    if blocker == "materials" and needs_attention(task, today):
        # The warehouse is asked only when the notes name it; otherwise the owner chases the ETA.
        target = "warehouse" if warehouse_dependency(task.notes) else task.owner
        return Action(
            task.task_id, "request_eta", target, "Materials dependency",
            f"{task.heading} is waiting for materials ({when}). Please confirm the delivery ETA.",
            key=f"materials; {when}",
        )

    if blocker == "upstream" and needs_attention(task, today):
        # The owner cannot unblock work that waits on another team: coordination can.
        return Action(
            task.task_id, "escalate", COORDINATION_QUEUE, "Upstream dependency",
            f"{task.heading} is blocked (owner: {task.owner}, due {due}). "
            "Please help unblock it and agree an ETA with the owner.",
            key=f"upstream; owner {task.owner}; due {due}",
        )

    if task.status == "Blocked":
        # Blocked with no known cause: ask the owner rather than escalate a guess.
        return Action(
            task.task_id, "follow_up_owner", task.owner, "Blocked without a known cause",
            f"{task.heading} is marked Blocked ({when}). Please say what is blocking it and give an ETA.",
            key=f"blocked; {when}",
        )

    if overdue:
        return Action(
            task.task_id, "follow_up_owner", task.owner, f"Overdue since {due}",
            f"{task.heading} was due on {due}. Please share the status and an updated ETA.",
            key=f"overdue since {due}",
        )

    return Action(task.task_id, "no_action", None, "No action required", None)
