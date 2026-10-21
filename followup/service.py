"""Read -> normalize -> classify -> send. Bounded retries and reconciliation."""

from dataclasses import replace
from datetime import date
import time

from .ai import interpret_safely
from .api import APIError, AmbiguousTimeout
from .models import Task
from .rules import decide


MAX_ATTEMPTS = 3


def pause(attempt: int):
    """Short exponential backoff, capped to avoid request floods."""
    time.sleep(min(0.1 * 2 ** (attempt - 1), 0.5))


class Service:
    def __init__(self, task_api, delivery_api, store, ai, today: date):
        self.task_api = task_api
        self.delivery_api = delivery_api
        self.store = store
        self.ai = ai
        self.today = today

    def fetch_tasks(self):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                tasks = [Task.from_dict(row) for row in self.task_api.fetch()]
                ids = [task.task_id for task in tasks]
                if len(ids) != len(set(ids)):
                    raise ValueError("Duplicate task IDs in fixture: refusing inconsistent sends")
                return tasks
            except APIError as error:
                if not error.retryable or attempt == MAX_ATTEMPTS:
                    raise
                self.store.log("read_retry", detail=f"HTTP {error.status_code}; attempt {attempt}")
                pause(attempt)

    def deliver(self, action):
        key = action.operation_id
        body = {k: v for k, v in action.to_dict("pending").items()
                if k != "delivery_status"}

        # Reconciliation first: a previous run may have timed out after sending.
        if self.delivery_api.lookup(key) is not None:
            self.store.log("already_sent", key)
            return "already_sent"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self.delivery_api.send(key, body)
                self.store.log("sent", key)
                return "sent"
            except (APIError, AmbiguousTimeout) as error:
                # Even a timeout is ambiguous: look up the stable operation ID.
                if self.delivery_api.lookup(key) is not None:
                    self.store.log("reconciled_after_timeout", key)
                    return "reconciled"
                retryable = isinstance(error, AmbiguousTimeout) or error.retryable
                if not retryable or attempt == MAX_ATTEMPTS:
                    self.store.log("send_failed", key, str(error))
                    return "failed"
                self.store.log("send_retry", key, f"Attempt {attempt}: {error}")
                pause(attempt)

        return "failed"

    def run(self):
        results = []
        for task in self.fetch_tasks():
            # Missing fields and completed tasks never need a model request.
            blocker = "none"
            if task.status != "Done" and task.owner and task.due_date and task.notes:
                blocker, fell_back = interpret_safely(self.ai, task.notes)
                if fell_back:
                    self.store.log("llm_fallback", detail=f"Task {task.task_id}")

            action = decide(task, self.today, blocker)
            if action.decision == "no_action":
                results.append(action.to_dict("not_needed"))
                continue

            action = replace(
                action, operation_id=task.fingerprint(action.decision, action.target)
            )
            status = self.deliver(action)
            results.append(action.to_dict(status))
        return results
