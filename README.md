# AI Operations & Follow-up Service

A small Python 3.11+ service that reads project tasks, decides which ones need attention, and **simulates** sending safe follow-up messages. Deterministic rules own every fact and action; an optional LLM only classifies free-text notes. An SQLite delivery log prevents duplicate sends across runs, retries, and timeouts.

No real messages are sent.

## Requirements

- Python 3.11 or newer (tested on 3.11 and 3.14).
- No install step: the standard library is enough (`requirements.txt` is intentionally empty).
- `ANTHROPIC_API_KEY` only for the optional `--llm claude` mode.

## Run it

From the repository root:

```bash
python main.py
```

The command reads `tasks.json` with the assessment date **2026-10-21 (UTC)** and writes:

- `.data/decisions.json` — one decision per task (`decision`, `target`, `reason`, `message`, `operation_id`, `delivery_status`).
- `.data/delivery_log.json` — deliveries recorded by the mock server plus timestamped audit events.
- `.data/followups.sqlite` — durable state, so later runs do not send duplicates.

Audit events also stream to stderr as they happen (`INFO` for sends, `WARNING` for retries, fallbacks, and failures); the summary goes to stdout.

Run the **same command again**: the first run sends 7 mock actions, the second sends 0. Three tasks need no action. Sample outputs are in `examples/` (`decisions.json`, `delivery_log.json`, and `failure_demo_log.json` from the failure demo below).

Unseen fixture with a separate database:

```bash
python main.py --fixture path/to/other_tasks.json --db .data/other.sqlite
```

Exit codes: `0` success, `1` when the run fails or any delivery fails (both reports are still written), `2` for invalid arguments. Run again with the same database to retry failed actions without duplicating successful ones.

## Test it

```bash
python -m unittest discover -s tests -v
```

Retries and a timeout **after a successful remote write**, on a fresh database (on PowerShell, put it on one line):

```bash
python main.py --db .data/failure_demo.sqlite \
  --read-failures 429,503 --send-failures 503,429 \
  --timeout-after-commit-once
```

The console and the audit log show `read_retry`, `send_retry`, and `reconciled_after_timeout`, with exactly one delivery per operation ID. Reads and sends make at most **3 attempts** with short exponential backoff and jitter; non-retryable 4xx errors are not retried. Receipt lookups retry the same way; a lookup that stays unavailable is never read as "not sent": that action is reported `failed` (`lookup_failed`) without a blind send, and a later run reconciles it. If a temporary error outlasts the retries, the rest of that run's sends are reported as `deferred` instead of hammering the provider; the next run resumes them.

## Optional real LLM (Claude)

The default is a deterministic local interpreter: no API key, network, or cost.

```bash
export ANTHROPIC_API_KEY="your_key_here"       # PowerShell: $env:ANTHROPIC_API_KEY = "..."
export ANTHROPIC_MODEL="claude-sonnet-5-5"     # optional; this is the default
python main.py --llm claude --db .data/claude_demo.sqlite
```

`ClaudeInterpreter` (`followup/ai.py`) calls the Claude Messages API to **classify non-empty notes** — nothing else:

- **Structured output:** a JSON Schema (`output_config.format`) allows only `{"blocker_type": "none" | "materials" | "upstream"}`. The answer is validated again locally; extra fields, invalid JSON, refusals, truncated output, or unexpected content blocks trigger the deterministic fallback. Thinking blocks are ignored.
- **The model cannot change facts or actions.** Owners, dates, status, recipients, and message text come from Python, never from the model. Notes are sent as untrusted data, and the system prompt says to ignore instructions inside them.
- **Errors:** 429/5xx, timeouts, and network errors get one retry after 1 second, then the fallback; if that retry fails too, Claude is skipped for the rest of the run (`provider_unavailable`) instead of being called for every note. Configuration errors — 400 (bad request), 401/403 (rejected key), 404 (unknown model) — stop the run: every call would fail the same way, so silently running on the fallback would hide the problem. Each fallback is logged as `llm_fallback` with a short reason from a fixed list (`http_<status>`, `timeout`, `network_error`, `invalid_json`, `invalid_schema`, `truncated_output`, `refusal`, `unexpected_stop`, `unsupported_content`, `response_too_large`, `provider_unavailable`, `provider_error`); provider messages and keys are never logged.
- **Visible degradation and cost:** the run report stores `llm_fallbacks` (notes the model could not classify) and `llm_usage` (calls and token totals); the CLI prints a warning when any fallback happened.
- **Hardening:** redirects are refused (the key never leaves api.anthropic.com), and responses are capped at 64 KiB.

Only notes are sent to Anthropic, but notes can contain sensitive information: `--llm claude` is an explicit external-data boundary. The tests mock the network and need no key. The app does not load `.env`; `.env.example` only lists the variable names.

## How to read the code

1. `main.py` — CLI options, run report, exit codes.
2. `followup/models.py` — input validation, normalization, stable operation ID.
3. `followup/rules.py` — deterministic business decisions.
4. `followup/notes.py` — offline keyword classification and warehouse routing.
5. `followup/ai.py` — Claude adapter with schema validation and safe fallback.
6. `followup/api.py` — mock task and delivery APIs with failure injection.
7. `followup/store.py` — SQLite mock delivery receipts, classifications, audit log.
8. `followup/service.py` — ties everything together; retries and reconciliation.

### Decision rules

Applied in priority order:

| Situation | Decision | Target |
| --- | --- | --- |
| Task is `Done` | `no_action` | none |
| Missing owner (or a placeholder such as `TBD`, `Unassigned`, `Not assigned`) | `clarify` | `coordination_queue` (a role, not an invented person) |
| Missing/invalid due date or unknown status | `clarify` | known owner |
| Materials dependency, and the task is `Blocked` or overdue | `request_eta` | `warehouse` when the notes name it, otherwise the owner |
| Waiting on upstream work, and the task is `Blocked` or overdue | `escalate` | `coordination_queue`; the message names the owner |
| `Blocked` with no known cause | `clarify` | known owner (what is blocking it, and the ETA) |
| Still overdue 3+ days after the owner's follow-up | `escalate` | `coordination_queue`; the message names the owner and the follow-up date |
| Overdue | `follow_up_owner` | known owner |
| Otherwise (including a dependency on a task that is on time) | `no_action` | none |

The owner is asked first, except for work waiting on another team, which the owner cannot unblock: even a long-overdue task gets a follow-up, and escalates only if it is still overdue three days after that follow-up to the same owner for the same deadline (a new owner or a moved deadline starts over). `ESCALATE_AFTER_DAYS` and `coordination_queue` are demo assumptions in `followup/rules.py`. Every message starts with the task ID, title, and project; reminders, ETA requests, and escalations also give the due date, so the recipient needs no other context. Missing or invalid dates are never guessed.

A valid model classification is used as returned; without one, `followup/notes.py` judges each clause of the note (split at punctuation, `and`, `but`) with small English keyword lists:

- a waiting word (`awaiting`, `waiting`, `pending`, `backorder`, `out of stock`, `not yet delivered`…) or a shortage word (`shortage`, `short on`, `missing`, `delayed`, `need`…) next to a materials word (`materials`, `parts`, `cable`, `delivery`, `supplier`, `warehouse`…) means `materials`; a waiting word alone means `upstream`;
- negated (`no backorder`, `not delayed`) and hypothetical (`if a backorder occurs`, `could become a problem`) clauses do not count;
- a resolution (`resolved`, `delivered`, `completed`…) does not count when the same clause says it has not happened yet (`not yet received`); a later update closes the blocker it talks about: "all materials have arrived" closes materials blockers, "permit completed" closes the permit wait, a bare "now resolved" closes the last one, and "safety training completed" closes nothing;
- the warehouse is asked only when a still-active materials clause names it.

These are deliberately small rules, not a language parser; richer notes are the LLM's job.

### Why repeated runs are safe

- **Stable operation ID.** `Task.fingerprint()` hashes the normalized task state with the decision and target. The mock delivery server stores it as a unique SQLite key, so the same task/action/state is delivered once.
- **Reconciliation.** The service looks the receipt up **before** sending and again after any timeout or error. When the server committed a delivery but the response was lost, the lookup finds it and no second send happens.
- **Pinned classification.** The note label is stored per task state, so a model that answers differently on the next run cannot change the action and its operation ID. A fallback label is pinned too, until the task changes. Date rules are recomputed, so a reminder can later become an escalation.
- **Honest reports.** An `already_sent` row shows the reason and message that were actually delivered, not today's wording.
- **Concurrency.** Receipts and labels use `INSERT OR IGNORE`; a worker that loses the race reports `already_sent`.
- **Partial batches.** Each decision is reported as soon as it is final, so a mid-batch failure keeps finished results. A crash after a committed send is reported as `already_sent` on the next run.

### Input validation

All task fields must be strings or null, valid Unicode, at most 4,000 characters. Errors name the record and field (`Task #2: last_update must be a string or null`), never the value. Missing or duplicate task IDs abort ingestion **before any send**. Dates must be `YYYY-MM-DD`; invalid due dates trigger clarification. IDs are trimmed and uppercased. Statuses are case-insensitive, with common synonyms (`Completed`/`Closed`/`Finished` → `Done`, `In Progress` → `Working on it`); any other status asks the owner to clarify. Fixtures saved as UTF-8 (with or without a byte order mark), UTF-16, or UTF-32 are accepted. Fixture, database, and report paths must differ, so a report can never overwrite the delivery receipts.

## Assumptions and trade-offs

- The evaluation date defaults to `2026-10-21` UTC; override with `--as-of YYYY-MM-DD`.
- `warehouse` and `coordination_queue` are mock internal routes, not inferred addresses.
- Standard library, SQLite, and a CLI only: a web framework or queue would not help this exercise.
- The LLM classifies notes instead of drafting messages: templated messages cannot invent owners, dates, or promises.
- Exactly-once delivery after an ambiguous timeout needs a provider with an idempotency key or a lookup endpoint; the SQLite mock server provides both.
- One malformed record fails the whole batch before any send: safer than partial input, at the cost of availability.

## What is incomplete, and what changes for production

Incomplete in this timebox:

- CLI batch only; no HTTP API or scheduler.
- No live quality evaluation of the LLM classifier.
- Offline keyword rules are English-only and deliberately simple.
- ETA requests and clarifications are sent once per task state; they are not escalated automatically if nobody answers.
- No staleness rule based on `last_update`; retries do not read `Retry-After` headers.

For production at ~10,000 tasks/day:

- Durable queue/outbox and a separate delivery worker; Postgres instead of SQLite; the real provider's idempotency keys.
- Quarantine bad records instead of failing the batch; dead-letter queue for persistent delivery failures.
- Metrics and alerts on send failures, `llm_fallback` rate, and token spend; per-run and daily LLM budgets; bounded LLM concurrency, and the Batch API for non-urgent classification.
- Secrets manager, authenticated APIs, and role-based access to reports, which contain names and task details.
- A migration plan for rule changes: a new target changes the operation ID and would allow a new follow-up.
