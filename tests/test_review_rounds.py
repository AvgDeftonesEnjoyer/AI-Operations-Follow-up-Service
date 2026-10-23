"""Edge cases: note boundaries, receipt lookup failures, batch recovery."""

from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from followup.ai import AIError
from followup.api import APIError, AmbiguousTimeout, DeliveryAPI, TaskAPI
from followup.models import Task
from followup.notes import simple_interpretation, warehouse_dependency
from followup.rules import decide
from followup.service import Service
from followup.store import Store


class NoteBoundaryTests(unittest.TestCase):
    def test_courtesy_and_unrelated_conditions_preserve_blocker(self):
        cases = [
            "If possible, confirm the ETA. Awaiting materials from warehouse.",
            "If possible confirm ETA for awaiting materials from warehouse.",
            "Awaiting materials from warehouse. Notify Sam, if the delivery slips.",
            "Awaiting materials from warehouse, notify Sam, if the delivery slips.",
            "Awaiting materials from warehouse with no confirmation of dispatch.",
        ]
        for note in cases:
            with self.subTest(note=note):
                self.assertEqual(simple_interpretation(note), "materials")
                self.assertTrue(warehouse_dependency(note))

    def test_explicit_arrival_resolves_old_materials_blocker(self):
        for update in [
            "Warehouse delivered all materials.",
            "All materials have arrived.",
            "All materials have been delivered.",
            "The materials are now delivered.",
        ]:
            with self.subTest(update=update):
                note = "Awaiting materials from warehouse. " + update
                self.assertEqual(simple_interpretation(note), "none")
                self.assertFalse(warehouse_dependency(note))

    def test_negative_arrival_does_not_resolve_old_blocker(self):
        for update in [
            "Warehouse has not delivered all materials.",
            "All materials have not arrived.",
            "All materials have not been delivered.",
            "The materials are not yet delivered.",
            "No materials delivered.",
            "Not all materials have arrived.",
        ]:
            with self.subTest(update=update):
                self.assertEqual(simple_interpretation(
                    "Awaiting materials from warehouse. " + update), "materials")

    def test_resolution_and_negation_matrix(self):
        for blocker, kind, resolution in [
            ("Awaiting materials from warehouse", "materials", "All materials have arrived"),
            ("Materials shortage", "materials", "Materials shortage resolved"),
            ("Waiting for permit approval", "upstream", "Permit completed"),
        ]:
            for separator in [". ", "; ", ", ", " and ", " but "]:
                for transform in [str.lower, str.upper, lambda s: s.replace(" ", "  ")]:
                    with self.subTest(blocker=blocker, separator=separator, transform=transform):
                        active = transform(blocker + separator + "Safety training completed")
                        self.assertEqual(simple_interpretation(active), kind)
                        resolved = transform(blocker + separator + resolution)
                        self.assertEqual(simple_interpretation(resolved), "none")

    def test_independent_dependencies_survive_resolution_in_either_order(self):
        for first, second in [
            ("Awaiting materials from warehouse", "Waiting for permit approval"),
            ("Waiting for permit approval", "Awaiting materials from warehouse"),
        ]:
            note = first + ". " + second + ". "
            self.assertEqual(simple_interpretation(note + "All materials have arrived."), "upstream")
            self.assertEqual(simple_interpretation(note + "Permit completed."), "materials")


class LookupFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "state.sqlite"))
        self.addCleanup(self.store.close)
        task = Task.from_dict(dict(task_id="A", task="Inspect", assigned_to="Sam",
                                   due_date="2026-10-20", status="Working on it"))
        action = decide(task, date(2026, 10, 21))
        self.action = replace(action, operation_id=task.fingerprint(action.decision, action.target))

    def service(self, sender):
        return Service(None, sender, self.store, None, date(2026, 10, 21))

    @patch("followup.service.pause")
    def test_lookup_transient_errors_retry_before_send(self, pause):
        sender = Mock()
        sender.lookup.side_effect = [APIError(429), APIError(503), None]
        sender.send.return_value = True
        self.assertEqual(self.service(sender).deliver(self.action), "sent")
        self.assertEqual(sender.lookup.call_count, 3)
        self.assertEqual(pause.call_count, 2)
        sender.send.assert_called_once()

    @patch("followup.service.pause")
    def test_unavailable_lookup_never_causes_blind_send(self, pause):
        for error, attempts in [(APIError(503), 3), (APIError(401), 1),
                                (AmbiguousTimeout("Unavailable"), 3)]:
            with self.subTest(error=type(error).__name__, attempts=attempts):
                sender = Mock()
                sender.lookup.side_effect = error
                self.assertEqual(self.service(sender).deliver(self.action), "failed")
                self.assertEqual(sender.lookup.call_count, attempts)
                sender.send.assert_not_called()

    @patch("followup.service.pause")
    def test_unavailable_reconciliation_keeps_committed_receipt_for_restart(self, pause):
        sender = DeliveryAPI(self.store, timeout_after_commit_once=True)
        with patch.object(sender, "lookup", side_effect=[None, APIError(503), APIError(503), APIError(503)]):
            self.assertEqual(self.service(sender).deliver(self.action), "failed")
        self.assertEqual(sender.calls, 1)
        self.assertEqual(len(self.store.deliveries()), 1)
        self.assertEqual(self.service(sender).deliver(self.action), "already_sent")
        self.assertEqual(sender.calls, 1)

    @patch("followup.service.pause")
    def test_temporary_reconciliation_error_recovers_without_resend(self, pause):
        sender = DeliveryAPI(self.store, timeout_after_commit_once=True)
        receipt = self.action.to_dict("sent")
        with patch.object(sender, "lookup", side_effect=[None, APIError(429), receipt]):
            self.assertEqual(self.service(sender).deliver(self.action), "reconciled")
        self.assertEqual(sender.calls, 1)


class BatchRecoveryTests(unittest.TestCase):
    @patch("followup.service.pause")
    def test_each_delivery_can_fail_and_recover_without_duplicates(self, pause):
        fixture = Path(__file__).resolve().parents[1] / "tasks.json"
        for failure_position in range(7):
            with self.subTest(position=failure_position), TemporaryDirectory() as directory:
                store = Store(str(Path(directory) / "state.sqlite"))
                try:
                    sender = DeliveryAPI(store)
                    original_send = sender.send
                    position = 0

                    def send(key, action):
                        nonlocal position
                        current, position = position, position + 1
                        if current == failure_position:
                            raise APIError(400)
                        return original_send(key, action)

                    # Exercise the same batch through the provider-failure fallback.
                    ai = Mock(interpret=Mock(side_effect=AIError("Unavailable")))
                    service = Service(TaskAPI(str(fixture)), sender, store, ai, date(2026, 10, 21))
                    with patch.object(sender, "send", side_effect=send):
                        first = service.run()
                    self.assertEqual(sum(r["delivery_status"] == "failed" for r in first), 1)
                    self.assertEqual(len(store.deliveries()), 6)
                    second = service.run()
                    self.assertEqual(sum(r["delivery_status"] == "sent" for r in second), 1)
                    self.assertEqual(len(store.deliveries()), 7)
                    third = service.run()
                    self.assertEqual(sum(r["delivery_status"] == "already_sent" for r in third), 7)
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
