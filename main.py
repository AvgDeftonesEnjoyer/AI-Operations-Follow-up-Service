"""Command-line entry point: python main.py --help"""

import argparse
from collections import Counter
from datetime import date
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from followup.ai import AIError, ClaudeInterpreter, StubAI
from followup.api import DeliveryAPI, TaskAPI
from followup.service import Service
from followup.store import Store, now_utc


def status_codes(text):
    return [int(code.strip()) for code in text.split(",") if code.strip()]


def write_json(filename, content):
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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

    # A report written over the database would erase delivery receipts and allow duplicates.
    paths = [Path(value).resolve() for value in
             (args.fixture, args.db, args.decisions_out, args.delivery_log_out)]
    for index, path in enumerate(paths):
        for other in paths[:index]:
            if path == other or (path.exists() and other.exists() and path.samefile(other)):
                parser.error("Fixture, database, and output paths must all be different")
    if args.llm == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        parser.error("ANTHROPIC_API_KEY is required with --llm claude")
    try:
        today = date.fromisoformat(args.as_of)
        read_failures = status_codes(args.read_failures)
        send_failures = status_codes(args.send_failures)
    except ValueError:
        parser.error("Invalid date or failure status codes")

    store = Store(args.db)
    try:
        run = {"id": uuid4().hex, "started_at": now_utc(), "status": "completed", "error": None}
        decisions = []
        ai = service = None
        try:
            reader = TaskAPI(args.fixture, read_failures)
            sender = DeliveryAPI(store, send_failures, args.timeout_after_commit_once)
            ai = ClaudeInterpreter() if args.llm == "claude" else StubAI()
            service = Service(reader, sender, store, ai, today)
            for decision in service.iter_results():
                decisions.append(decision)
            if any(row["delivery_status"] == "failed" for row in decisions):
                run.update(status="failed", error="Delivery failure")
        except (Exception, KeyboardInterrupt) as exc:  # Ctrl+C still writes honest reports.
            # Only our own errors carry their text (record number, field name, HTTP status);
            # anything else is reported by type, so task data or credentials cannot leak.
            text = f": {exc}" if isinstance(exc, (ValueError, AIError)) else ""
            run.update(status="failed", error=type(exc).__name__ + text)
        run["finished_at"] = now_utc()
        run["llm_usage"] = getattr(ai, "usage", None)
        run["llm_fallbacks"] = getattr(service, "fallbacks", 0)
        store.log("run_failed" if run["status"] == "failed" else "run_completed",
                  detail=f"Run {run['id']}: {run['error'] or 'OK'}")
        write_json(args.decisions_out, {"as_of": args.as_of, "run": run, "decisions": decisions})
        write_json(args.delivery_log_out, {
            "run": run, "deliveries": store.deliveries(), "events": store.events(),
        })
        counts = Counter(item["delivery_status"] for item in decisions)
        print(f"Processed {len(decisions)} tasks: {dict(counts)}")
        if run["llm_usage"]:
            print(f"LLM usage: {run['llm_usage']}")
        if run["llm_fallbacks"]:
            print(f"Warning: {run['llm_fallbacks']} note(s) used the offline fallback "
                  "(see llm_fallback events)")
        if run["status"] == "failed":
            print(f"Run failed: {run['error']}")
        print(f"Decisions: {args.decisions_out}")
        print(f"Delivery / audit log: {args.delivery_log_out}")
        return 1 if run["status"] == "failed" else 0
    finally:
        store.close()


if __name__ == "__main__":
    # Audit events stream to stderr as they happen; the summary goes to stdout.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
