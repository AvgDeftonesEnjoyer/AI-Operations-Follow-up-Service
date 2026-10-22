"""A failure in the middle of a batch must not erase results that were already final."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import main
from followup.api import APIError, DeliveryAPI
from followup.store import Store

ROW = dict(task_id="A", project="Demo", task="Inspect", assigned_to="Sam",
           due_date="2026-10-20", status="Working on it", last_update="2026-10-19",
           notes="An ambiguous operational note")


class PartialBatchTests(unittest.TestCase):
    def test_failure_mid_batch_keeps_finished_decisions(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "tasks.json"
            fixture.write_text(json.dumps([ROW, {**ROW, "task_id": "B"}]), encoding="utf-8")
            argv = ["main.py", "--fixture", str(fixture), "--db", str(root / "db.sqlite"),
                    "--decisions-out", str(root / "decisions.json"),
                    "--delivery-log-out", str(root / "log.json")]

            def run_cli():
                with patch("sys.argv", argv), redirect_stdout(StringIO()):
                    code = main.main()
                return code, json.loads((root / "decisions.json").read_text(encoding="utf-8"))

            # Both tasks need a send, and each send starts with a lookup: call 2 belongs to task B.
            real_lookup = DeliveryAPI.lookup
            calls = []

            def lookup_fails_on_second_task(api, key):
                calls.append(key)
                if len(calls) == 2:
                    raise APIError(503)
                return real_lookup(api, key)

            with patch.object(DeliveryAPI, "lookup", lookup_fails_on_second_task):
                code, report = run_cli()
            self.assertEqual(code, 1)
            self.assertEqual(report["run"]["status"], "failed")
            self.assertEqual(report["run"]["error"], "APIError")
            self.assertEqual([(d["task_id"], d["delivery_status"]) for d in report["decisions"]],
                             [("A", "sent")])

            # A restart sends only what is missing.
            code, report = run_cli()
            self.assertEqual(code, 0)
            self.assertEqual([(d["task_id"], d["delivery_status"]) for d in report["decisions"]],
                             [("A", "already_sent"), ("B", "sent")])
            store = Store(str(root / "db.sqlite"))
            self.assertEqual(len(store.deliveries()), 2)
            store.close()
