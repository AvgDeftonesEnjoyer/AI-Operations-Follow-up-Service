# AI Operations & Follow-up Service

A small Python 3.11+ service that reads project tasks, decides which ones need attention, and **simulates** sending safe follow-up messages. It uses predictable business rules, optional LLM note interpretation, and an SQLite delivery log to avoid sending duplicates.

No real messages are sent. **No third-party packages are required.**

## Run it

From the repository root:

```bash
python main.py
```

The command reads `tasks.json` with the assessment date **2026-10-21 (UTC)** and writes:

- `.data/decisions.json` — one decision for each task (`decision`, `target`, `reason`, `message`, `operation_id`, `delivery_status`).
- `.data/delivery_log.json` — messages actually recorded by the mock server, plus timestamped audit events.
- `.data/followups.sqlite` — durable state, so subsequent runs do not send duplicates.

Run the **same command again**. The first run sends 7 mock actions; the second sends 0 new actions. Three tasks need no action. Human-readable sample outputs from the first run are in `examples/`.

Use an unseen fixture and separate database like this:

```bash
python main.py --fixture path/to/other_tasks.json --db .data/other.sqlite
```

## Test it

```bash
python -m unittest discover -s tests -v
```

To demonstrate retries and a timeout **after a successful remote write**, use a fresh database:

```bash
python main.py --db .data/failure_demo.sqlite \
  --read-failures 429,503 --send-failures 503,429 \
  --timeout-after-commit-once
```

Notice `reconciled_after_timeout` in the audit events and only one delivery for each operation ID. Both reads and sends have at most **3 attempts**, with short exponential backoff. Non-retryable 4xx errors are not retried.

## Optional real LLM

The default is a deterministic local interpreter: no API key, network request, or cost.

```bash
export ANTHROPIC_API_KEY="your_key_here"
export ANTHROPIC_MODEL="claude-haiku-5-5"  # optional
python main.py --llm claude --db .data/claude_demo.sqlite
```

`ClaudeInterpreter` calls the Claude Messages API to **classify non-empty notes**, not to change task facts or send messages. It uses a small JSON Schema (`output_config.format`) to restrict output to a single blocker category. It accepts only `{"blocker_type":"none|materials|upstream"}` (one of these three exact values). Extra fields, invalid JSON, errors, or unsupported values trigger a safe deterministic fallback. Instructions inside task notes are treated as untrusted text. Message text is always assembled by Python from verified task fields, not copied from LLM output. The built-in HTTP client uses only Python standard libraries; Claude Code CLI is not needed.

The `.env.example` file lists the environment variable names; the app does not automatically load `.env` files. Export the values in your shell before running. Check `.data/delivery_log.json` for `llm_fallback` events if Claude is unavailable or returns an unusable response. Live API calls may cost money. The automated tests mock the network and do not require an API key.

## How to read the code

Read the files in this order:

1. `main.py` — command-line options and where results are written.
2. `followup/models.py` — normalizes input and computes a stable operation ID.
3. `followup/rules.py` — readable, deterministic business decisions.
4. `followup/ai.py` — optional Claude classification with JSON Schema, strict validation and safe fallback.
5. `followup/api.py` — fake task and delivery APIs (including failure simulation).
6. `followup/store.py` — SQLite mock remote delivery receipts and audit log.
7. `followup/service.py` — ties everything together; retries and reconciles errors.
8. `tests/test_followup.py` — scenarios explained through executable tests.

### Decision rules

Rules are applied in priority order:

| Situation | Decision | Target |
| --- | --- | --- |
| Task is `Done` | `no_action` | none |
| Missing owner | `clarify` | `coordination_queue` (a role, not a made-up person) |
| Missing/invalid due date or unknown status | `clarify` | known owner |
| Materials/warehouse dependency | `request_eta` | warehouse **only when named in notes**; otherwise known owner |
| Blocked or waiting for upstream work | `escalate` | known owner |
| At least 7 days overdue | `escalate` | known owner |
| 1–6 days overdue | `follow_up_owner` | known owner |
| Otherwise | `no_action` | none |

The 7-day escalation threshold and `coordination_queue` are explicit demo assumptions in `followup/rules.py`. Only the provided structured fields determine owner, status, and due date; notes may provide a blocker category. Invalid or missing dates are never guessed. Completed tasks do not generate follow-ups.

### Why repeated runs are safe

For an action, `Task.fingerprint()` hashes the normalized task state together with the decision and target. That becomes the **stable operation ID**. The fake delivery server stores this ID as a unique SQLite primary key.

The service checks the mock remote receipt **before** sending. After a timeout or transient error, it checks that receipt again before retrying. When the mock server commits a delivery but the response is lost, reconciliation finds the existing receipt and prevents a second send. Restarting the program or changing only the AI response does not create another send for the same task/action/state. A real change to task state can create a new follow-up.

Duplicate task IDs or tasks with no ID cause ingestion to fail **before any messages are sent**. All outbound messages and audit events are local; no real recipient addresses are required.

## Assumptions and limits

- The evaluation date defaults to `2026-10-21` in UTC, even if the computer's actual date differs. Override with `--as-of YYYY-MM-DD`.
- `warehouse` and `coordination_queue` are **mock internal routes**, not inferred email addresses. No integrations or contact lists are assumed.
- The project deliberately uses the Python standard library, SQLite, and a CLI. A web framework or queue would add complexity without helping this exercise.
- The sample SQLite mock server supports deduplication and lookup by operation ID. A real messaging provider must support an idempotency key or a reliable reconciliation endpoint; without that, exactly-once delivery cannot be guaranteed after an ambiguous timeout.
- For production and ~10,000 tasks/day: use a durable worker queue/outbox, separate delivery service, Postgres, structured metrics/alerts, bounded LLM concurrency and budgets, secret management, authenticated APIs, and appropriate access controls. Add a retry/dead-letter policy for persistent failures. The demo does not implement those infrastructure pieces.

No private keys or real customer data are included.
