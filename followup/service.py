"""Read -> normalize -> classify -> send. Bounded retries and reconciliation."""

from dataclasses import replace
from datetime import date
import random
import time

from .ai import interpret_safely
from .api import APIError, AmbiguousTimeout
from .models import Task
from .rules import decide, needs_attention


MAX_ATTEMPTS = 3


def pause(attempt: int):
    """Short exponential backoff, capped to avoid request floods.

    Jitter keeps parallel workers from retrying in lockstep.
    """
    time.sleep(min(0.1 * 2 ** (attempt - 1), 0.5) * random.uniform(0.5, 1))


def is_retryable(error):
    """A timeout or a 429/5xx may succeed later; other API errors will not."""
    return isinstance(error, AmbiguousTimeout) or error.retryable


class Service:
    def __init__(self, task_api, delivery_api, store, ai, today: date):
        self.task_api = task_api
        self.delivery_api = delivery_api
        self.store = store
        self.ai = ai
        self.today = today
        self.fallbacks = 0  # Notes the LLM could not classify in this run.
        self.delivery_down = False  # Set when retries run out on a temporary delivery error.

    def fetch_tasks(self):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                rows = self.task_api.fetch()
                break
            except APIError as error:
                if not error.retryable or attempt == MAX_ATTEMPTS:
                    raise
                self.store.log("read_retry", detail=f"HTTP {error.status_code}; attempt {attempt}")
                pause(attempt)

        tasks = []
        for number, row in enumerate(rows, 1):
            try:
                tasks.append(Task.from_dict(row))
            except ValueError as error:
                # Name the record so an unseen fixture can be fixed quickly.
                raise ValueError(f"Task #{number}: {error}") from None
        ids = [task.task_id for task in tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate task IDs in fixture: refusing inconsistent sends")
        return tasks

    def deliver(self, action):
        """Send at most once per operation ID. Returns the delivery status."""
        key = action.operation_id
        # The provider kept failing earlier in this run: do not flood it; the next run resumes.
        if self.delivery_down:
            self.store.log("send_deferred", key)
            return "deferred"
        try:
            return self._deliver(action)
        except (APIError, AmbiguousTimeout) as error:
            # An unavailable lookup is not evidence that nothing was sent: send nothing.
            self.store.log("lookup_failed", key, type(error).__name__)
            if is_retryable(error):
                self.delivery_down = True
            return "failed"

    def lookup(self, key):
        """Receipt lookup with the same bounded retries as sends."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self.delivery_api.lookup(key)
            except (APIError, AmbiguousTimeout) as error:
                if not is_retryable(error) or attempt == MAX_ATTEMPTS:
                    raise
                self.store.log("lookup_retry", key, f"Attempt {attempt}")
                pause(attempt)

    def _deliver(self, action):
        key = action.operation_id
        body = {k: v for k, v in action.to_dict("pending").items()
                if k != "delivery_status"}

        # Reconciliation first: a previous run may have timed out after sending.
        if self.lookup(key) is not None:
            self.store.log("already_sent", key)
            return "already_sent"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                created = self.delivery_api.send(key, body)
                status = "sent" if created else "already_sent"
                self.store.log(status, key)
                return status
            except (APIError, AmbiguousTimeout) as error:
                # Even a timeout is ambiguous: look up the stable operation ID.
                if self.lookup(key) is not None:
                    self.store.log("reconciled_after_timeout", key)
                    return "reconciled"
                if not is_retryable(error) or attempt == MAX_ATTEMPTS:
                    self.store.log("send_failed", key, str(error))
                    if is_retryable(error):  # Temporary errors outlasted every retry.
                        self.delivery_down = True
                    return "failed"
                self.store.log("send_retry", key, f"Attempt {attempt}: {error}")
                pause(attempt)

        return "failed"

    def classify(self, task):
        """The blocker label for a task's notes, asked of the model only when it can matter.

        The label is pinned per note text, fallback or not, and the first worker to pin wins:
        the same notes can never lead to two different messages, even after the model
        recovers. Edited notes are classified afresh.
        """
        if task.status in {"Done", "Unknown"} or not (task.owner and task.due_date and task.notes):
            return "none"  # Missing facts or a finished task: the notes cannot change the action.
        if not needs_attention(task, self.today):
            return "none"  # On time and not blocked: no action either way, so no model cost.
        state_id = task.state_id()
        stored = self.store.get_classification(state_id)
        if stored is not None:
            return stored
        label, fallback_reason = interpret_safely(self.ai, task.notes)
        if fallback_reason is not None:
            self.fallbacks += 1
            self.store.log("llm_fallback", detail=f"Task {task.task_id}; reason={fallback_reason}")
        return self.store.save_classification(state_id, label)

    def iter_results(self):
        """Yield each result as soon as it is final, so a later failure cannot erase it."""
        for task in self.fetch_tasks():
            chased_on = self.store.chased_on(task.task_id, task.owner, task.due_key)
            action = decide(task, self.today, self.classify(task), chased_on)
            if action.decision == "no_action":
                yield action.to_dict("not_needed")
                continue

            action = replace(action, operation_id=task.operation_id(action))
            status = self.deliver(action)
            if status in {"sent", "reconciled", "already_sent"}:
                # Our own record of what was delivered: escalation timing must not depend on
                # what a real provider keeps.
                self.store.record_action(action, task.due_key, self.today)
            yield action.to_dict(status)

    def run(self):
        return list(self.iter_results())
