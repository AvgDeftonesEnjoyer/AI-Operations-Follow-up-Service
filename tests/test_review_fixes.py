"""Regression checks for note context, partial runs, and safe diagnostics."""

from contextlib import redirect_stdout
from datetime import date
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import main
from followup.ai import AIError, ClaudeInterpreter, StubAI, interpret_safely
from followup.api import DeliveryAPI, TaskAPI
from followup.models import Task
from followup.notes import simple_interpretation, warehouse_dependency
from followup.rules import decide
from followup.service import Service
from followup.store import Store


ROW = dict(task_id="A", task="Install cable", project="Demo", assigned_to="Sam",
           due_date="2026-10-25", status="Working on it", last_update="2026-10-20", notes="")
TODAY = date(2026, 10, 21)


class NoteContextTests(unittest.TestCase):
    def test_resolved_and_conditional_notes_do_not_send_premature_eta_requests(self):
        for note in [
            "Awaiting materials from warehouse, now resolved.",
            "Awaiting materials from warehouse, all materials delivered today.",
            "Awaiting materials from warehouse. All materials delivered today.",
            "Awaiting materials from warehouse if the delivery slips.",
            "Awaiting materials from warehouse, if the delivery slips.",
            "If the delivery slips, awaiting materials from warehouse could become a problem.",
        ]:
            with self.subTest(note=note):
                label = simple_interpretation(note)
                self.assertEqual(label, "none")
                self.assertFalse(warehouse_dependency(note))
                action = decide(Task.from_dict({**ROW, "notes": note}), TODAY, label)
                self.assertEqual(action.decision, "no_action")

    def test_resolutions_do_not_erase_other_or_new_dependencies(self):
        cases = [
            ("Awaiting materials from warehouse, no ETA.", "materials"),
            ("Awaiting materials from warehouse, not yet resolved.", "materials"),
            ("Awaiting materials from warehouse, could you confirm ETA?", "materials"),
            ("Backorder resolved, but awaiting materials from warehouse.", "materials"),
            ("Backorder resolved. Waiting for permit approval.", "upstream"),
            ("Waiting for permit approval. Warehouse backorder resolved.", "upstream"),
            ("Awaiting materials from warehouse. Waiting for permit approval. All materials delivered.", "upstream"),
            ("Awaiting materials from warehouse. Waiting for permit approval, now resolved.", "materials"),
            ("Awaiting materials from supplier. Warehouse is not involved.", "materials"),
            ("Waiting for permit approval. Awaiting materials from warehouse.", "materials"),
        ]
        for note, expected in cases:
            with self.subTest(note=note):
                self.assertEqual(simple_interpretation(note), expected)


class PartialRunTests(unittest.TestCase):
    def test_cli_keeps_completed_decisions_and_restart_does_not_duplicate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, db = root / "tasks.json", root / "state.sqlite"
            decisions, log = root / "decisions.json", root / "log.json"
            fixture.write_text(json.dumps([
                {**ROW, "task_id": "DONE", "status": "Done"},
                {**ROW, "task_id": "SENT", "due_date": "2026-10-20"},
                {**ROW, "task_id": "LATER", "due_date": "2026-10-20"},
            ]), encoding="utf-8")
            args = ["main.py", "--fixture", str(fixture), "--db", str(db),
                    "--decisions-out", str(decisions), "--delivery-log-out", str(log)]
            original = DeliveryAPI.lookup

            def fail_second_lookup(sender, key):
                if sender.calls == 1:
                    raise OSError("private-provider-detail")
                return original(sender, key)

            output = StringIO()
            with patch.object(sys, "argv", args), patch.object(DeliveryAPI, "lookup", fail_second_lookup):
                with redirect_stdout(output):
                    self.assertEqual(main.main(), 1)
            result = json.loads(decisions.read_text())
            self.assertEqual([row["delivery_status"] for row in result["decisions"]], ["not_needed", "sent"])
            self.assertEqual(result["run"]["status"], "failed")
            self.assertIn("Processed 2 tasks", output.getvalue())
            self.assertNotIn("private-provider-detail", log.read_text() + output.getvalue())
            self.assertEqual(len(json.loads(log.read_text())["deliveries"]), 1)
            with patch.object(sys, "argv", args), redirect_stdout(StringIO()):
                self.assertEqual(main.main(), 0)
            self.assertEqual([row["delivery_status"] for row in json.loads(decisions.read_text())["decisions"]],
                             ["not_needed", "already_sent", "sent"])
            self.assertEqual(len(json.loads(log.read_text())["deliveries"]), 2)

    def test_streaming_still_validates_whole_input_before_sending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "tasks.json"
            fixture.write_text(json.dumps([{**ROW, "due_date": "2026-10-20"}, {"task": "Missing ID"}]))
            store = Store(str(root / "state.sqlite"))
            try:
                stream = Service(TaskAPI(str(fixture)), DeliveryAPI(store), store, StubAI(), TODAY).iter_results()
                with self.assertRaises(ValueError):
                    next(stream)
                self.assertEqual(store.deliveries(), [])
            finally:
                store.close()


class FallbackReasonTests(unittest.TestCase):
    def test_network_errors_have_safe_categories_and_http_bodies_are_closed(self):
        body = BytesIO(b"private-provider-detail")
        cases = [
            (HTTPError("https://example.com", 401, "private-provider-detail", {}, body), "http_401"),
            (HTTPError("https://example.com", 429, "private-provider-detail", {}, None), "http_429"),
            (TimeoutError("private-provider-detail"), "timeout"),
            (URLError(TimeoutError("private-provider-detail")), "timeout"),
            (URLError("private-provider-detail"), "network_error"),
        ]
        for failure, expected in cases:
            with self.subTest(expected=expected), patch("urllib.request.OpenerDirector.open", side_effect=failure):
                self.assertEqual(interpret_safely(ClaudeInterpreter(api_key="test-key"), "Warehouse backorder"),
                                 ("materials", expected))
        self.assertTrue(body.closed)

    def test_response_failures_have_distinct_reasons(self):
        def response(text, stop="end_turn"):
            return json.dumps({"stop_reason": stop, "content": [{"type": "text", "text": text}]}).encode()

        cases = [
            (b"not JSON", "invalid_json"),
            (response("not JSON"), "invalid_json"),
            (response('{"blocker_type":42}'), "invalid_schema"),
            (response('{}', "max_tokens"), "truncated_output"),
            (response('{}', "refusal"), "refusal"),
            (json.dumps({"stop_reason": "end_turn", "content": [{"type": "tool_use"}]}).encode(), "unsupported_content"),
            (b"x" * 65537, "response_too_large"),
        ]
        for raw, expected in cases:
            with self.subTest(expected=expected), patch("urllib.request.OpenerDirector.open", return_value=BytesIO(raw)):
                self.assertEqual(interpret_safely(ClaudeInterpreter(api_key="test-key"), "Warehouse backorder"),
                                 ("materials", expected))

    def test_audit_records_only_allowed_reasons(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "tasks.json"
            store = Store(str(root / "state.sqlite"))
            try:
                for index, failure in enumerate([
                    AIError("private-provider-detail", "timeout"),
                    AIError("private-provider-detail", "private-provider-detail"),
                    RuntimeError("private-provider-detail"),
                ]):
                    fixture.write_text(json.dumps([{**ROW, "task_id": str(index), "notes": "Warehouse backorder"}]))
                    ai = Mock(interpret=Mock(side_effect=failure))
                    Service(TaskAPI(str(fixture)), DeliveryAPI(store), store, ai, TODAY).run()
                events = [event for event in store.events() if event["event"] == "llm_fallback"]
                self.assertEqual([event["detail"].split("reason=")[1] for event in events],
                                 ["timeout", "provider_error", "provider_error"])
                self.assertNotIn("private-provider-detail", json.dumps(store.events()))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
