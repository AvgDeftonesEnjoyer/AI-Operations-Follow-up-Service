# AI Operations & Follow-up Service

A small Python service that reads project tasks, decides which ones need attention, and **simulates** sending follow-up messages. Deterministic rules own every fact and action; an optional LLM only classifies free-text notes. An SQLite store makes sure the same request is never sent twice — across reruns, retries, timeouts, and parallel workers. No real messages are sent.

## Requirements

Python 3.11 or newer (tested on 3.11 and 3.14). Standard library only, so there is nothing to install. `ANTHROPIC_API_KEY` is needed only for the optional `--llm claude` mode.

## Run it

```bash
python main.py            # reads tasks.json, evaluation date 2026-10-21 (UTC)
python main.py            # again: sends nothing new
```

The first run sends 7 mock actions and three tasks need none; the second run sends 0. It writes:

- `.data/decisions.json` — one record per task: `task_id`, `decision`, `target`, `reason`, `message`, `operation_id`, `delivery_status`;
- `.data/delivery_log.json` — the mock deliveries and the audit events;
- `.data/tasks.sqlite` — the state that prevents duplicates. Each fixture gets its own database (`.data/<fixture name>.sqlite`), so `python main.py --fixture other_tasks.json` never mistakes an unseen fixture for handled work.

Audit events also stream to stderr (`INFO` for sends, `WARNING` for retries, fallbacks, failures). Exit codes: `0` success, `1` a failed run or delivery (reports are still written; rerun to retry safely), `2` invalid arguments. Sample outputs are in `examples/`. Override the date with `--as-of YYYY-MM-DD`.

## Demos

```bash
# Retries and a timeout after a committed write (PowerShell: one line)
python main.py --db .data/failure_demo.sqlite --read-failures 429,503 --send-failures 503,429 --timeout-after-commit-once

# The safe fallback, without a key: a demo model that always answers outside the schema
python main.py --llm broken --db .data/broken_demo.sqlite

# The real model
export ANTHROPIC_API_KEY="..."     # PowerShell: $env:ANTHROPIC_API_KEY = "..."
python main.py --llm claude --db .data/claude_demo.sqlite
```

The failure demo logs `read_retry`, `send_retry`, and `reconciled_after_timeout`, and still makes exactly one delivery per operation ID (see `examples/failure_demo_log.json`). The broken model makes every note fall back to the keyword rules: each fallback is logged, counted in the report, and the actions stay the same.

## Test it

```bash
python -m unittest discover -s tests -v
```

`test_followup.py` covers the spec scenarios and rules; `test_edge_cases.py` notes, dates, escalation timing, and the Claude adapter; the other files cover note context, receipt lookups, and partial runs. The network is mocked, so no key is needed.

## How it decides

Applied in this order (`followup/rules.py`):

| Situation | Decision | Target |
| --- | --- | --- |
| `Done` | `no_action` | — |
| No owner (or a placeholder such as `TBD`, `Unassigned`, `PM to assign`) | `clarify` | `coordination_queue` — a role, never an invented person |
| No valid due date | `clarify` | owner |
| Overdue, and chased 3+ days ago | `escalate` | `coordination_queue` |
| Unknown status | `clarify` | owner |
| Waiting for materials, and `Blocked` or overdue | `request_eta` | `warehouse` if the notes name it, else the owner |
| Waiting on other work, and `Blocked` or overdue | `escalate` | `coordination_queue` |
| `Blocked` with no known cause | `follow_up_owner` | owner: what is blocking it? |
| Overdue | `follow_up_owner` | owner |
| Otherwise | `no_action` | — |

- **The owner first, then coordination.** An overdue task is chased once after its deadline (a follow-up or an ETA request). If it is still overdue three days later, coordination is asked for a recovery plan — whatever the notes say by then, so a tricked model or an owner's "finishing Friday" cannot silence it. Work waiting on another team goes to coordination straight away, because the owner cannot unblock it. After the deadline there is one escalation, whatever its cause.
- **Messages** start with the task ID, title, and project; reminders, ETA requests, and escalations give the due date. They never contain day counts. Facts are never guessed: a missing owner or date is asked for.
- **Notes** are classified by the model if available, otherwise by `followup/notes.py`: a blocker word ("awaiting", "short on", "not received yet") next to a materials word means `materials`; next to "upstream", "permit", or another task's ID it means `upstream`. Negated ("no backorder") and hypothetical ("if a delay occurs") clauses do not count, and a later "all materials have arrived" closes the blocker. When unsure the answer is `none`, so the owner gets the reminder.

## Why it never sends the same request twice

- **Operation ID from facts.** Each rule sets a `key` with the facts of its request (for example `overdue since 2026-10-18`); the ID is task + action + recipient + key, stored as a unique key by the mock provider. Renaming a task, editing notes, touching `last_update`, another spelling of the owner, or editing a message template does not resend.
- **Reconciliation.** The receipt is looked up before every send and again after any timeout or error. If the provider committed but the response was lost, the lookup finds it. A lookup that stays unavailable is reported `failed` — never treated as "not sent".
- **Bounded retries.** At most 3 attempts with exponential backoff and jitter for 429/5xx and timeouts; other 4xx errors are not retried. If temporary errors outlast the retries, the rest of the run is `deferred` instead of flooding the provider.
- **Pinned labels.** The first label decided for a note (model or fallback) is stored, so a different answer later cannot turn the same note into a second message. The first worker to store it wins.
- **Own action record.** Escalation timing reads the service's own `actions` table, not the provider's storage.
- **Honest reports.** Every decision is reported as soon as it is final, so a crash or Ctrl+C keeps finished results; an `already_sent` row shows the text that was actually delivered.

## The LLM (optional)

`followup/ai.py` calls the Claude Messages API (`claude-sonnet-5-5` by default, `ANTHROPIC_MODEL` to change) only for `Blocked` or overdue tasks, where the label can change the action.

- A JSON Schema (`output_config.format`) allows only `{"blocker_type": "none" | "materials" | "upstream"}`; the answer is validated again locally. Invalid JSON, extra fields, refusals, or truncated output fall back to the keyword rules.
- The model cannot change facts: owners, dates, status, recipients, and message text come from Python. Notes are sent as untrusted data.
- 429/5xx and timeouts get one retry, then the fallback; if Claude keeps failing it is skipped for the rest of the run. A bad request, a rejected key, or an unknown model (400/401/403/404) stops the run instead of hiding behind the fallback.
- The run report counts fallbacks (`llm_fallbacks`) and token use (`llm_usage`). Provider messages and keys are never logged; redirects are refused and responses are capped at 64 KiB.
- Checked live (2026-10-24, `claude-sonnet-5-5`): `tasks.json` gets the same actions as with the keyword rules, using 5 calls and about 1,900 input tokens, with no fallbacks; a rerun makes no calls. On ten tricky notes (injection, negation, resolved, hypothetical, Spanish, a part code) the model labeled all ten correctly and the keyword rules seven. Their three misses fall back to an owner reminder.

## Input handling

Fields may be strings, numbers (`"task_id": 1002` → `"1002"`), or null, up to 4,000 characters; inner spaces are collapsed. Dates are `YYYY-MM-DD` or ISO timestamps (taken in UTC). Statuses are case-insensitive with common synonyms (`Completed` → `Done`, `In Progress` → `Working on it`). Fixtures may be UTF-8 (with or without BOM), UTF-16, or UTF-32. A missing or duplicate task ID, or an unreadable field, stops the run before any send, naming the record and field — never the value.

## How to read the code

1. `main.py` — CLI, run report, exit codes.
2. `followup/models.py` — input normalization and the operation ID.
3. `followup/rules.py` — the decision table above.
4. `followup/notes.py` — offline note rules.
5. `followup/ai.py` — Claude adapter, validation, and fallback.
6. `followup/api.py` — mock task and delivery APIs with failure injection.
7. `followup/store.py` — SQLite: mock receipts, the action record, labels, audit log.
8. `followup/service.py` — ties it together: retries and reconciliation.

## Assumptions and trade-offs

- `warehouse` and `coordination_queue` are mock routes, not inferred addresses; the 3-day threshold is a demo setting.
- Standard library, SQLite, and a CLI only: a web framework or queue would not help this exercise.
- The LLM classifies notes instead of drafting messages, so it cannot invent owners, dates, or promises.
- Exactly-once delivery after an ambiguous timeout needs a provider with idempotency keys or a lookup endpoint; the mock provides both.
- One malformed record stops the whole batch before any send: safer than acting on partial input.
- A request sent while a task was on time is repeated once when the deadline passes ("overdue since …"), because the situation changed and escalation needs a request made after the deadline. Rerunning with the same data and date sends nothing.
- A fallback label decided during a model outage stays with that note text: consistency over re-classification. Edited notes are classified again; compare interpreters on separate databases.

## What is incomplete, and what changes for production

Incomplete: CLI only, no HTTP API or scheduler; the LLM was checked live on ten notes, not an evaluation set; English-only keyword rules (a part code shaped like a task ID, `ABC-1234`, reads as a task dependency); clarifications of missing facts are not escalated; `Retry-After` headers are not read.

For ~10,000 tasks/day:

- a durable queue/outbox, a separate delivery worker, Postgres, and the real provider's idempotency keys;
- quarantine for bad records and a dead-letter queue instead of failing the batch;
- metrics and alerts on failures, fallback rate, and token spend; LLM budgets, bounded concurrency, and the Batch API;
- a secrets manager, authenticated APIs, and access control for reports, which contain names;
- a migration plan for rule changes, since a new target changes the operation ID.
