"""Command-line entry point: python main.py --help"""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import os
import tempfile

from followup.ai import ClaudeInterpreter, StubAI
from followup.api import DeliveryAPI, TaskAPI
from followup.service import Service
from followup.store import Store


def status_codes(text):
    return [int(code.strip()) for code in text.split(",") if code.strip()]


def write_json(filename, content):
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A failed write must not truncate the previous report.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as file:
            temporary = Path(file.name)
            file.write(json.dumps(content, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Safe AI task follow-up demo")
    parser.add_argument("--fixture", default="tasks.json")
    parser.add_argument("--db", default=".data/followups.sqlite")
    parser.add_argument("--decisions-out", default=".data/decisions.json")
    parser.add_argument("--delivery-log-out", default=".data/delivery_log.json")
    parser.add_argument("--as-of", default="2026-10-21", help="UTC evaluation date (YYYY-MM-DD)")
    parser.add_argument("--llm", choices=["stub", "claude"], default="stub")
    parser.add_argument("--read-failures", default="", help="E.g. 429,503")
    parser.add_argument("--send-failures", default="", help="E.g. 503,429")
    parser.add_argument("--timeout-after-commit-once", action="store_true")
    args = parser.parse_args()

    paths = [Path(value).resolve() for value in
             (args.fixture, args.db, args.decisions_out, args.delivery_log_out)]
    for index, path in enumerate(paths):
        for other in paths[:index]:
            if path == other or (path.exists() and other.exists() and path.samefile(other)):
                parser.error("Fixture, database, and output paths must all be different")
    try:
        today = date.fromisoformat(args.as_of)
        read_failures = status_codes(args.read_failures)
        send_failures = status_codes(args.send_failures)
    except ValueError:
        parser.error("Invalid date or failure status codes")

    store = Store(args.db)
    try:
        reader = TaskAPI(args.fixture, read_failures)
        sender = DeliveryAPI(
            store, send_failures, args.timeout_after_commit_once
        )
        ai = ClaudeInterpreter() if args.llm == "claude" else StubAI()
        service = Service(reader, sender, store, ai, today)
        decisions = service.run()
        write_json(args.decisions_out, {"as_of": args.as_of, "decisions": decisions})
        write_json(args.delivery_log_out, {
            "deliveries": store.deliveries(), "events": store.events(),
        })
        counts = Counter(item["delivery_status"] for item in decisions)
        print(f"Processed {len(decisions)} tasks: {dict(counts)}")
        print(f"Decisions: {args.decisions_out}")
        print(f"Delivery / audit log: {args.delivery_log_out}")
        return 1 if counts["failed"] else 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
