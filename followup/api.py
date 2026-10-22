"""Mock APIs with injectable failures for local runs and tests."""

import json
from pathlib import Path


class APIError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Mock API returned HTTP {status_code}")

    @property
    def retryable(self):
        return self.status_code == 429 or 500 <= self.status_code <= 599


class AmbiguousTimeout(Exception):
    """Request timed out; the remote side MAY have committed it."""


class TaskAPI:
    def __init__(self, fixture: str, fail_first: list[int] | None = None):
        self.fixture = Path(fixture)
        self.fail_first = list(fail_first or [])
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.fail_first:
            raise APIError(self.fail_first.pop(0))
        with self.fixture.open(encoding="utf-8") as file:
            tasks = json.load(file)
        if not isinstance(tasks, list):
            raise ValueError("Fixture must be an array of tasks")
        return tasks


class DeliveryAPI:
    def __init__(self, store, fail_first: list[int] | None = None,
                 timeout_after_commit_once: bool = False):
        self.store = store
        self.fail_first = list(fail_first or [])
        self.timeout_after_commit_once = timeout_after_commit_once
        self.calls = 0

    def lookup(self, operation_id: str):
        """Mock equivalent of GET /deliveries/by-idempotency-key/{key}."""
        return self.store.get_delivery(operation_id)

    def send(self, operation_id: str, action: dict):
        """Return whether this call created a delivery, rather than replayed one."""
        self.calls += 1
        if self.fail_first:
            raise APIError(self.fail_first.pop(0))
        inserted = self.store.save_delivery(operation_id, action)
        if inserted and self.timeout_after_commit_once:
            self.timeout_after_commit_once = False
            raise AmbiguousTimeout("Response lost AFTER remote commit")
        return inserted
