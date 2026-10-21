"""Command-line entry point: python main.py --help"""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path

from followup.ai import OpenAIInterpreter, StubAI
from followup.api import DeliveryAPI, TaskAPI
from followup.service import Service
from followup.store import Store


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
    parser.add_argument("--llm", choices=["stub", "openai"], default="stub")
    parser.add_argument("--read-failures", default="", help="E.g. 429,503")
    parser.add_argument("--send-failures", default="", help="E.g. 503,429")
    parser.add_argument("--timeout-after-commit-once", action="store_true")
    args = parser.parse_args()

    store = Store(args.db)
    try:
        reader = TaskAPI(args.fixture, status_codes(args.read_failures))
        sender = DeliveryAPI(
            store, status_codes(args.send_failures), args.timeout_after_commit_once
        )
        ai = OpenAIInterpreter() if args.llm == "openai" else StubAI()
        service = Service(reader, sender, store, ai, date.fromisoformat(args.as_of))
        decisions = service.run()
        write_json(args.decisions_out, {"as_of": args.as_of, "decisions": decisions})
        write_json(args.delivery_log_out, {
            "deliveries": store.deliveries(), "events": store.events(),
        })
        counts = Counter(item["delivery_status"] for item in decisions)
        print(f"Processed {len(decisions)} tasks: {dict(counts)}")
        print(f"Decisions: {args.decisions_out}")
        print(f"Delivery / audit log: {args.delivery_log_out}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
