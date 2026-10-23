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

Receipt lookups also retry transient errors at most 3 times. An unavailable lookup is never treated as an absent receipt: the action is reported as failed without a blind send or resend. A later run can reconcile it safely. Lookup retries and exhausted failures appear as `lookup_retry` and `lookup_failed` audit events.

The CLI exits with code `1` when any delivery fails, after writing both reports. Invalid arguments exit with code `2`. Run again with the same database to retry failed actions without duplicating successful ones. On PowerShell, put the demo command on one line instead of using Bash backslashes.

Both reports include the same `run` ID, start/finish timestamps, status, and error category. An ingestion or setup failure after opening the database writes a failed run with an empty decisions list, so old successful reports are not mistaken for current results. Existing delivery receipts remain in the audit report. Database/output filesystem failures can still prevent reports from being written and produce a nonzero exit.

The CLI collects `Service.iter_results()` one decision at a time. If a later task fails unexpectedly, the report keeps the completed decisions and marks the run failed. `Service.run()` remains a list-returning convenience method. Input validation still finishes for the entire fixture before the first delivery. A crash between a delivery commit and its yielded result can leave that current action out of the report; the durable receipt remains the source of truth for reconciliation.

## Optional real LLM

The default is a deterministic local interpreter: no API key, network request, or cost.

```bash
export ANTHROPIC_API_KEY="your_key_here"
export ANTHROPIC_MODEL="claude-sonnet-5-5"  # optional; this is the default API model
python main.py --llm claude --db .data/claude_demo.sqlite
```

`ClaudeInterpreter` calls the Claude Messages API to **classify non-empty notes**, not to change task facts or send messages. It uses a small JSON Schema (`output_config.format`) to restrict output to a single blocker category. It accepts only `{"blocker_type":"none|materials|upstream"}` (one of these three exact values). Extra fields, invalid JSON, errors, or unsupported values trigger a safe deterministic fallback. Instructions inside task notes are treated as untrusted text. Message text is always assembled by Python from verified task fields, not copied from LLM output. The built-in HTTP client uses only Python standard libraries; Claude Code CLI is not needed.

The `.env.example` file lists the environment variable names; the app does not automatically load `.env` files. Export the values in your shell before running. Check `.data/delivery_log.json` for `llm_fallback` events if Claude is unavailable or returns an unusable response. Live API calls may cost money. The automated tests mock the network and do not require an API key.

Fallback events include a safe reason such as `timeout`, `http_401`, `invalid_json`, `invalid_schema`, or `truncated_output`. `interpret_safely()` returns `(category, reason)`, with `reason=None` on success. Unknown failures use `provider_error`; raw exception messages and provider response bodies are never copied into these audit events.

Sonnet 5.5 is the default for `--llm claude`. Set `ANTHROPIC_MODEL=claude-opus-5-5` to use Opus instead. Model selection does not change the business rules or validation. The default CLI mode is still the offline stub; selecting a model does not enable network calls by itself. Cached classifications are reused even when the model changes, so compare models using separate demo databases. No live quality comparison between these models has been run for this project.

Claude may return thinking blocks before its answer. The adapter ignores `thinking` and `redacted_thinking` blocks and requires exactly one final `text` block with valid classification JSON. Requests use low effort and a 1,024-token output budget to leave room for thinking. Refusals, truncated output, extra text blocks, or unsupported block types still trigger fallback.

In PowerShell, use `$env:ANTHROPIC_API_KEY = "your_key_here"`. Only notes are sent to Anthropic, but notes can contain sensitive information; the Claude option is an explicit external-data boundary. The HTTP adapter refuses redirects, bounds response size to 64 KiB, and closes error responses. Local reports contain task titles and recipient names: treat them and the SQLite database as sensitive operational data.

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
| Materials/warehouse dependency | `request_eta` | warehouse only when an active materials clause explicitly links it to the dependency; otherwise known owner |
| Blocked or waiting for upstream work | `escalate` | known owner |
| At least 7 days overdue | `escalate` | known owner |
| 1–6 days overdue | `follow_up_owner` | known owner |
| Otherwise | `no_action` | none |

The 7-day escalation threshold and `coordination_queue` are explicit demo assumptions in `followup/rules.py`. Only the provided structured fields determine owner, status, and due date; notes may provide a blocker category. Invalid or missing dates are never guessed. Completed tasks do not generate follow-ups.

`followup/notes.py` holds the small phrase rules shared by the offline interpreter and warehouse routing. A valid model classification is used as returned; phrase matching is the fallback, not an override. Hypothetical or resolved clauses are skipped, and “no update” or “no ETA” does not negate an active blocker. Merely mentioning warehouse does not select it as the recipient.

The offline rules collect blocker mentions before deciding whether they remain active. A named resolution updates earlier mentions of that category; a short “now resolved” updates the latest mention. Completing unrelated work, such as safety training, does not clear a blocker. Clauses joined by “and” are evaluated separately so “installation is not started” does not negate an adjacent materials delay. Leading conditions and inline hypothetical blockers are skipped; a separate trailing condition clears only the immediately preceding blocker clause in the same sentence, for explicit delivery conditions such as “if the delivery slips”. Courtesy phrases such as “if possible” do not clear it.

Resolution rules recognize explicit delivery/arrival statements, including “warehouse delivered all materials” and “all materials have arrived”. Negative statements such as “not all materials have arrived” do not resolve an existing blocker. Negation applies directly to the recognized dependency phrase; a later “no confirmation of dispatch” does not cancel an active materials delay.

Materials take priority over upstream dependencies regardless of sentence order. The fallback recognizes “missing materials”, “waiting on materials”, and “materials have not arrived”, as well as backorders and shortages. “No ETA”, “not yet resolved”, and polite “could you” requests do not resolve a blocker. Warehouse routing requires a direct connection between the materials dependency and warehouse; unrelated warehouse mentions leave the ETA request with the known owner.

These are deliberately small English phrase rules, not a general language parser. A category-wide resolution cannot distinguish multiple independent material dependencies without structured dependency IDs. For richer or ambiguous notes, use the LLM adapter; its classification still needs evaluation on representative data. Changing the model alone does not refresh classifications already cached in an existing database.

Existing databases keep their cached classifications and delivery history. To evaluate changed note rules from scratch, use a separate demo database; do not delete operational delivery history to force reclassification. Changing routing rules can change the action target and therefore its operation ID, so review previously processed tasks before applying rule changes to an existing operational database.

### Why repeated runs are safe

For an action, `Task.fingerprint()` hashes the normalized task state together with the decision and target. That becomes the **stable operation ID**. The fake delivery server stores this ID as a unique SQLite primary key.

Before this step, the interpreted blocker is persisted by normalized task state. Repeated runs reuse it instead of querying a possibly inconsistent model. Concurrent workers use the first stored classification. A fallback result is also pinned until the task changes; this favors stable actions over automatically reclassifying after provider recovery. Date rules are still recomputed, so an overdue reminder may later become an escalation. Existing databases gain the classification table automatically, but old deliveries lack classification history: review them before switching provider on a pre-upgrade database.

The service checks the mock remote receipt **before** sending. After a timeout or transient error, it checks that receipt again before retrying. When the mock server commits a delivery but the response is lost, reconciliation finds the existing receipt and prevents a second send. Restarting the program or changing only the AI response does not create another send for the same task/action/state. A real change to task state can create a new follow-up.

The mock send API returns whether it created a receipt. If another worker won the race after the initial lookup, the replay is logged as `already_sent`, so delivery counts and audit events agree.

Duplicate task IDs or tasks with no ID cause ingestion to fail **before any messages are sent**. All outbound messages and audit events are local; no real recipient addresses are required.

All supplied task fields must be strings or null, with at most 4,000 characters per field. Invalid types abort ingestion before any sends. IDs are trimmed and uppercased. Dates must use `YYYY-MM-DD`; invalid due dates trigger clarification, and invalid last updates become unknown. Last update is part of the state, not a separate staleness rule. The fallback recognizes a small set of English blocker phrases and conservatively skips negated/resolved clauses; it is not a general language parser. A JSON Schema constrains model output shape, not semantic truth or all possible prompt injections.

Fixture, database, and both report paths must differ. Reports are replaced atomically one at a time; they are snapshots, while SQLite receipts remain the source of truth after a crash. The two reports are not one transaction.

## Assumptions and limits

- The evaluation date defaults to `2026-10-21` in UTC, even if the computer's actual date differs. Override with `--as-of YYYY-MM-DD`.
- `warehouse` and `coordination_queue` are **mock internal routes**, not inferred email addresses. No integrations or contact lists are assumed.
- The project deliberately uses the Python standard library, SQLite, and a CLI. A web framework or queue would add complexity without helping this exercise.
- The sample SQLite mock server supports deduplication and lookup by operation ID. A real messaging provider must support an idempotency key or a reliable reconciliation endpoint; without that, exactly-once delivery cannot be guaranteed after an ambiguous timeout.
- For production and ~10,000 tasks/day: use a durable worker queue/outbox, separate delivery service, Postgres, structured metrics/alerts, bounded LLM concurrency and budgets, secret management, authenticated APIs, and appropriate access controls. Add a retry/dead-letter policy for persistent failures. The demo does not implement those infrastructure pieces.

The review found no private-key, Anthropic-key, GitHub-token, or AWS-access-key pattern matches in the fetched Git history. This targeted scan cannot prove the absence of every possible secret.
