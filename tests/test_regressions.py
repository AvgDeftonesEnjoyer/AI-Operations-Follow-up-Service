"""Regressions found during review; no network or credentials required."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from followup.ai import AIError, ClaudeInterpreter, NoRedirects, simple_interpretation
from followup.api import DeliveryAPI, TaskAPI
from followup.models import Task
from followup.service import Service
from followup.store import Store


ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 21)
ROW = dict(task_id="A", project="Demo", task="Inspect", assigned_to="Sam",
           due_date="2026-10-20", status="Working on it", last_update="2026-10-19",
           notes="An ambiguous operational note")


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "tasks.json"
        self.fixture.write_text(json.dumps([ROW]), encoding="utf-8")
        self.db = self.root / "state.sqlite"
        self.store = Store(str(self.db))
        self.addCleanup(self.store.close)

    def run_service(self, ai, today=TODAY):
        return Service(TaskAPI(str(self.fixture)), DeliveryAPI(self.store),
                       self.store, ai, today).run()

    def test_changed_model_answer_cannot_send_again_for_unchanged_state(self):
        ai = Mock()
        ai.interpret.side_effect = ["none", "materials"]
        first = self.run_service(ai)
        second = self.run_service(ai)
        self.assertEqual(second[0]["delivery_status"], "already_sent")
        self.assertEqual(first[0]["operation_id"], second[0]["operation_id"])
        self.assertEqual(ai.interpret.call_count, 1)
        self.assertEqual(len(self.store.deliveries()), 1)

    def test_cached_classification_survives_restart(self):
        self.run_service(Mock(interpret=Mock(return_value="none")))
        other = Store(str(self.db))
        try:
            ai = Mock(interpret=Mock(return_value="materials"))
            result = Service(TaskAPI(str(self.fixture)), DeliveryAPI(other), other, ai, TODAY).run()
            self.assertEqual(result[0]["delivery_status"], "already_sent")
            ai.interpret.assert_not_called()
        finally:
            other.close()

    def test_new_task_state_reclassifies(self):
        ai = Mock(interpret=Mock(return_value="none"))
        self.run_service(ai)
        self.fixture.write_text(json.dumps([{**ROW, "notes": "Updated information"}]))
        self.run_service(ai)
        self.assertEqual(ai.interpret.call_count, 2)
        self.assertEqual(len(self.store.deliveries()), 2)

    def test_cached_label_does_not_freeze_date_rules(self):
        ai = Mock(interpret=Mock(return_value="none"))
        self.run_service(ai)
        result = self.run_service(ai, date(2026, 10, 27))
        self.assertEqual(result[0]["decision"], "escalate")
        self.assertEqual(len(self.store.deliveries()), 2)
        ai.interpret.assert_called_once()

    def test_bad_field_types_fail_before_any_delivery(self):
        for field in ROW:
            for value in [False, 123, ["Sam"], {"name": "Sam"}]:
                with self.subTest(field=field, value=value):
                    self.fixture.write_text(json.dumps([ROW, {**ROW, field: value}]))
                    with self.assertRaises(ValueError):
                        self.run_service(Mock())
                    self.assertEqual(self.store.deliveries(), [])

    def test_dates_are_strict_and_last_update_is_normalized(self):
        for value in ["20261020", "2026-W43-2", "2026-02-30"]:
            self.assertIsNone(Task.from_dict({**ROW, "due_date": value}).due_date)
        self.assertIsNone(Task.from_dict({**ROW, "last_update": "invalid"}).last_update)
        self.assertEqual(Task.from_dict({**ROW, "last_update": " 2026-10-19 "}).last_update,
                         "2026-10-19")

    def test_oversized_fields_are_rejected_not_silently_truncated(self):
        with self.assertRaises(ValueError):
            Task.from_dict({**ROW, "notes": "a" * 4001})

    def test_resolved_or_negated_notes_are_not_active_blockers(self):
        for notes in ["Warehouse delivered all materials.", "No upstream dependency.",
                      "Backorder resolved.", "No material shortage.",
                      "Warehouse confirmed receipt.", "Upstream splice completed."]:
            with self.subTest(notes=notes):
                self.assertEqual(simple_interpretation(notes), "none")
        self.assertEqual(simple_interpretation("Warehouse confirmed backorder, no ETA."), "materials")
        self.assertEqual(simple_interpretation("Awaiting materials from warehouse"), "materials")
        self.assertEqual(simple_interpretation("Unable to proceed until upstream splice confirmed"),
                         "upstream")

    def test_parallel_workers_share_classification_and_delivery(self):
        def worker(label):
            store = Store(str(self.db))
            try:
                ai = Mock(interpret=Mock(return_value=label))
                return Service(TaskAPI(str(self.fixture)), DeliveryAPI(store), store, ai, TODAY).run()
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ["none", "materials"]))
        self.assertEqual(results[0][0]["operation_id"], results[1][0]["operation_id"])
        self.assertEqual(len(self.store.deliveries()), 1)

    def test_note_edge_cases_produce_safe_actions_and_no_duplicates(self):
        from followup.ai import StubAI

        cases = [
            ("If a warehouse backorder occurs, contact procurement.", "follow_up_owner", "Sam"),
            ("Awaiting materials from supplier. Warehouse is not involved.", "request_eta", "Sam"),
            ("Awaiting materials from warehouse, no update yet.", "request_eta", "warehouse"),
            ("Awaiting materials from warehouse; no ETA.", "request_eta", "warehouse"),
            ("Warehouse backorder resolved. Awaiting materials from supplier.", "request_eta", "Sam"),
            ("Warehouse confirmed backorder, no ETA.", "request_eta", "warehouse"),
            ("No material shortage.", "follow_up_owner", "Sam"),
            ("Warehouse delivered all materials.", "follow_up_owner", "Sam"),
        ]
        self.fixture.write_text(json.dumps([
            {**ROW, "task_id": str(index), "notes": notes}
            for index, (notes, _, _) in enumerate(cases)
        ]), encoding="utf-8")
        results = self.run_service(StubAI())
        for result, (notes, decision, target) in zip(results, cases):
            with self.subTest(notes=notes):
                self.assertEqual((result["decision"], result["target"]), (decision, target))
        again = self.run_service(StubAI())
        self.assertTrue(all(row["delivery_status"] == "already_sent" for row in again))
        self.assertEqual(len(self.store.deliveries()), len(cases))

    def test_provider_failure_uses_corrected_note_fallback(self):
        self.fixture.write_text(json.dumps([
            {**ROW, "notes": "Awaiting materials from warehouse, no update yet."}
        ]), encoding="utf-8")
        result = self.run_service(Mock(interpret=Mock(side_effect=AIError("Unavailable"))))
        self.assertEqual((result[0]["decision"], result[0]["target"]),
                         ("request_eta", "warehouse"))
        self.assertTrue(any(event["event"] == "llm_fallback" for event in self.store.events()))

    def cli(self, *extra):
        return subprocess.run([sys.executable, str(ROOT / "main.py"),
                               "--fixture", str(self.fixture), "--db", str(self.db),
                               "--decisions-out", str(self.root / "decisions.json"),
                               "--delivery-log-out", str(self.root / "log.json"), *extra],
                              capture_output=True, text=True, timeout=20)

    def test_failed_delivery_has_nonzero_exit_and_written_report(self):
        result = self.cli("--send-failures", "400")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads((self.root / "decisions.json").read_text())
        self.assertEqual(report["decisions"][0]["delivery_status"], "failed")
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(len(self.store.deliveries()), 1)

    def test_exhausted_send_retries_are_bounded(self):
        sender = DeliveryAPI(self.store, fail_first=[503] * 5)
        with patch("followup.service.pause") as pause:
            result = Service(TaskAPI(str(self.fixture)), sender, self.store,
                             Mock(interpret=Mock(return_value="none")), TODAY).run()
        self.assertEqual(sender.calls, 3)
        self.assertEqual(pause.call_count, 2)
        self.assertEqual(result[0]["delivery_status"], "failed")
        self.assertEqual(self.store.deliveries(), [])

    def test_output_cannot_overwrite_fixture_or_database(self):
        original = self.fixture.read_bytes()
        for path in [self.fixture, self.db, self.root / "log.json"]:
            result = self.cli("--decisions-out", str(path))
            self.assertEqual(result.returncode, 2)
        self.assertEqual(self.fixture.read_bytes(), original)
        self.assertEqual(self.store.deliveries(), [])

    def test_api_redirect_is_refused(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {},
                                                       "https://example.com"))

    def test_oversized_provider_response_is_rejected(self):
        with patch("urllib.request.OpenerDirector.open", return_value=BytesIO(b"x" * 65537)):
            with self.assertRaises(AIError):
                ClaudeInterpreter(api_key="test-key").interpret("Note")


if __name__ == "__main__":
    unittest.main()
