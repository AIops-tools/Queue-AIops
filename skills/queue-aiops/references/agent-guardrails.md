# Agent guardrails — running queue-aiops with a smaller / local model

If you drive these tools with a local model (Llama, Qwen, Mistral … via Goose,
Ollama, LM Studio, or any OpenAI-compatible runtime), you will get noticeably
better results with a short system prompt. This page gives you one, and — more
importantly — tells you which guardrails you **no longer need to write**, because
the tool now enforces them itself.

The distinction matters. A guardrail in a prompt is a request. A guardrail in the
harness is a guarantee. Anything below that we could move into the harness, we did.

## What the tool now enforces — do not waste prompt budget on these

| You might be tempted to prompt | Why you don't need to |
|---|---|
| "Work read-only, never touch a queue" | Set `QUEUE_READ_ONLY=1`. The eight write tools (`declare_queue`, `delete_queue`, `purge_queue`, `set_policy`, `delete_policy`, `redis_config_set`, `redis_kill_client`, `undo_apply`) are then **not registered at all** — they never appear in the tool list, so the model cannot call one even if it tries. The `@governed_tool` harness independently refuses writes, so the CLI is covered too. |
| "Don't invent a value when a field is missing" | RabbitMQ omits `idle_since` for an active queue; a Redis primary reports no `master_link_status`; an unnamed client has no name. Those come back as `null`, never as `""`. |
| "Tell me if the output was cut off" | `redis_slowlog`, `redis_clients`, `redis_big_keys`, `list_queues`, `list_connections`, `list_channels` and `list_policies` all return `{"<items>": [...], "returned": N, "limit": L, "truncated": true/false}`. Truncation is measured — one extra entry is requested from Redis — not guessed from a length coincidence. |
| "Preserve the ordering / tell me what's most urgent" | `rabbitmq_queue_backlog_rca`, `redis_memory_pressure_rca`, `redis_latency_rca` and `connection_churn_analysis` rank findings worst-first, each carrying the measured number it was based on. Priority is in the payload, not implied by list position. |
| "Don't run KEYS * on production" | `redis_big_keys` uses SCAN under a hard key budget and sizes only an evenly-spaced subset with MEMORY USAGE. There is no code path that can issue `KEYS *`. `coveragePct` reports how partial the walk was. |
| "Confirm before anything destructive" | `delete_queue` and `purge_queue` require a `--dry-run`-able preview plus double confirmation at the CLI, and a named approver (`QUEUE_AUDIT_APPROVED_BY`) for high-risk tiers. |
| "Log what you did" | Every governed call is audited to `~/.queue-aiops/audit.db` regardless of what the model says it did. |

## What still needs a prompt

These are model-behaviour problems the harness cannot fix from the outside.
Copy this into your agent's system prompt:

```text
You operate a RabbitMQ broker or a Redis instance through the queue-aiops MCP
tools.

TOOL USE
- Before answering any question about the current broker, you MUST call a tool.
  Never answer from memory or assumption.
- Actually invoke the tool. Do not describe the call you would make, and do not
  emit an example JSON response in place of calling it.
- If a tool call fails, report the real error verbatim. Never fill the gap with
  a plausible-sounding answer.

READING RESULTS
- Read the whole result before concluding. If a result contains a "truncated"
  field that is true, say so and re-run with a higher limit. The slowest command
  or the deepest queue may be the one just past the cut-off.
- A null field means the broker did not report that value. Report it as "not
  available" — never infer it.
- Report values exactly as returned. Redis counters from INFO are cumulative
  since the instance started — compare rates, never quote a raw total as
  "operations today". SLOWLOG durations are microseconds.
- "coveragePct" on a big-key sample is how much of the keyspace was walked. A
  large key found in a 3% sample is evidence there are large keys; it is not
  evidence that it is the largest key. Say which you mean.

SCOPE
- Separate observation from interpretation. State what the tools returned, then
  any interpretation, clearly marked as such.
- A queue with a backlog and zero consumers is a stopped/absent consumer, not a
  slow broker. Check the consumer count before blaming throughput.
- Unacked messages piling up is a consumer that is not acknowledging — a
  different fault from a queue nothing is reading. Do not conflate them.
- A node memory or disk alarm blocks publishers on the WHOLE broker via flow
  control, not just the queue you were looking at. Report it as global.
- Do not confuse a queue with an exchange, a vhost with a queue name, a channel
  with a connection, or a Redis key with a queue.
- RabbitMQ and Redis are different systems. Do not suggest a RabbitMQ policy on
  a Redis target; the platform is in every result.
```

## Recommended setup for a local model

```bash
# Read-only until you trust the setup — this is enforced, not advisory.
export QUEUE_READ_ONLY=1
queue-aiops doctor
```

Then, when you are ready to allow writes, unset it and set an approver so the
high-risk tier has an accountable name on it:

```bash
unset QUEUE_READ_ONLY
export QUEUE_AUDIT_APPROVED_BY="your.name@example.com"
export QUEUE_AUDIT_RATIONALE="draining the dead-letter queue, ticket OPS-881"
```

Read-only is worth the extra step here because the destructive operations
destroy data rather than configuration: `purge_queue` discards messages that no
undo token can bring back.

## If your model still struggles

Some behaviours are model-capacity limits rather than prompt problems:

- **Multi-tool workflows time out or drift.** Prefer the RCA tools —
  `rabbitmq_queue_backlog_rca` correlates queue depth, consumer count, rates and
  node alarms inside one call, so the model does not have to chain
  `list_queues`, `list_connections` and `node_health` and keep vhost/queue name
  pairs straight.
- **The model ignores later tool results in a long context.** The slowlog and
  the queue list are the big payloads. Use `--count` / a vhost filter
  deliberately rather than pulling everything.
- **The model describes calls instead of making them.** This is usually a
  runtime/tool-calling-format mismatch, not a prompt problem — check that your
  client advertises the tools in the format your model was trained on.

Feedback on running this with a specific local model is genuinely useful —
open an issue at
[github.com/AIops-tools/Queue-AIops](https://github.com/AIops-tools/Queue-AIops/issues)
with the model, runtime, and what went wrong.
