<!-- mcp-name: io.github.AIops-tools/queue-aiops -->

# Queue AIops

**Governed AI-ops for redis + rabbitmq.** queue-aiops is for the team running
their own cache and message broker — a redis that "suddenly eats memory", a
rabbitmq whose queues quietly grow until publishers block — without an
enterprise observability suite. It gives an AI agent (or a human at the CLI) a
governed toolset over both: transparent root-cause analyses for memory
pressure, latency, queue backlog, and connection churn, plus the handful of
writes an operator actually needs (config set, client kill, purge/delete
queue, policies) — every call audited, budgeted, risk-tiered, and undo-recorded
by the built-in governance harness.

> **Disclaimer**: Community-maintained open-source project, **not affiliated
> with, endorsed by, or sponsored by the Redis or RabbitMQ projects or their
> respective owners.** Redis and RabbitMQ are trademarks of their respective owners.
>
> **Verification**: behaviour is covered by a mock-based test suite; not yet validated
> against live brokers. Both redis and rabbitmq are free/self-hostable (one lab container
> or package install each), so a lab check is easy — `queue-aiops doctor` is the fastest
> live probe, and [docs/VERIFICATION.md](docs/VERIFICATION.md) is the checklist.

## Quick start

```bash
uv tool install queue-aiops

queue-aiops init      # wizard: pick platform (redis/rabbitmq), host/port, encrypted secret
queue-aiops doctor    # config + secret + connectivity check (PING / /api/overview)
queue-aiops overview  # one-shot health summary for the default target
```

Then the interesting parts:

```bash
queue-aiops analyze memory     # redis memory-pressure RCA (maxmemory, eviction, frag, big keys)
queue-aiops analyze latency    # redis latency RCA (slowlog digest, fork/AOF stalls)
queue-aiops analyze backlog    # rabbitmq queue-backlog RCA (consumers, unacked, watermarks)
queue-aiops analyze churn      # connection churn, both platforms
queue-aiops redis bigkeys      # SCAN-budgeted big-key sample (never KEYS *)
queue-aiops rabbitmq queues    # deepest backlog first
```

## What this tool does, and does not, decide

It delivers broker operations — reads and writes — accurately and efficiently,
and records every one of them. It does **not** decide whether a write is allowed
to happen. That is the agent's judgement, or the permission of the account you
connect it with: give the Redis connection an ACL user restricted to read
commands, or the RabbitMQ management user only the `monitoring` tag, and the
writes fail at the broker — the place that actually owns the permission.

So there is no read-only switch, no policy file, no approval gate to configure.
The one thing the tool guarantees is that nothing is silent: **every call, over
MCP and over the CLI alike, lands an audit row** in `~/.queue-aiops/audit.db`,
and reversible writes still capture their before-state and record an inverse.

> Each tool declares a `risk_level`, kept in agreement with its `[READ]`/`[WRITE]`
> documentation tag by a test, and carried into the audit row as a descriptive
> tier — so a reviewer can see at a glance that a row was a high-risk delete. It
> is a label, not a gate.

Running a smaller / local model? See
[agent-guardrails.md](skills/queue-aiops/references/agent-guardrails.md) — it lists
the guardrails this tool now enforces for you (so you don't spend prompt budget
restating them) and gives a ready-made system prompt for what's left.

## Support scope

| Platform | Protocol | Coverage |
|----------|----------|----------|
| **redis** (5.x–7.x wire protocol via `redis` Python client) | RESP, password optional, TLS optional | INFO (server/memory/clients/stats/persistence/keyspace), SLOWLOG, CLIENT LIST/KILL, CONFIG GET/SET, MEMORY STATS/USAGE, SCAN-budgeted big-key sampling, DBSIZE, PING |
| **rabbitmq** (management plugin HTTP API) | HTTP(S), Basic auth | /api/overview, /api/queues (+ per-vhost, detail, purge, declare, delete), /api/connections, /api/channels, /api/consumers, /api/policies (get/set/delete), /api/nodes |

**28 MCP tools** — 20 reads (incl. 4 flagship RCAs) + 8 governed writes.

| Group | Tools | R/W |
|-------|-------|:---:|
| Overview | queue_overview | read |
| redis reads | redis_server_info, redis_memory_stats, redis_clients, redis_slowlog, redis_config_get, redis_keyspace, redis_big_keys | read |
| rabbitmq reads | rabbitmq_overview, list_queues, queue_detail, list_connections, list_channels, list_policies, node_health | read |
| Flagship RCAs | redis_memory_pressure_rca, redis_latency_rca, rabbitmq_queue_backlog_rca, connection_churn_analysis | read |
| Writes (medium) | redis_config_set, redis_kill_client, declare_queue, set_policy, delete_policy | write |
| Writes (**high**) | purge_queue, delete_queue | write |
| Undo | undo_list, undo_apply | read / write |

The four RCAs are transparent heuristics that report their numbers — thresholds
are named constants, every finding carries its evidence, never a black-box
verdict. Big-key sampling walks at most 10,000 keys with SCAN and sizes at most
200 with MEMORY USAGE — **never `KEYS *`** — and reports its coverage.

## Governance

Every MCP tool — and every CLI write, which routes through the same governed
functions — runs through the bundled `@governed_tool` harness
(`queue_aiops.governance` — no external dependency). It **records; it does not
authorize** whether a write may happen (see above):

- **Audit** — every call lands in `~/.queue-aiops/audit.db` (relocatable via
  `QUEUE_AIOPS_HOME`), secret-redacted. The CLI writes the same row the MCP path
  does — there is no unaudited entry point.
- **Budget / runaway guard** — a safety backstop, not an authorization gate:
  call/time ceilings (`QUEUE_MAX_TOOL_CALLS`, `QUEUE_MAX_TOOL_SECONDS`) + a
  runaway-loop breaker stop a stuck agent from burning unbounded calls/time.
- **Risk tier** — a descriptive label on the audit row derived from `risk_level`;
  it gates nothing. `QUEUE_AUDIT_APPROVED_BY` / `QUEUE_AUDIT_RATIONALE` are
  optional annotations recorded on the row (who/why), never required and never
  blocking.
- **Undo** — reversible writes capture the real before-state first:
  `redis_config_set` records the prior value from CONFIG GET;
  `set_policy`/`delete_policy` record the prior policy; `delete_queue` records
  the queue's definition and its undo re-declares it (the **messages are not
  restored** — the descriptor says so). Irreversible writes (`purge_queue`,
  `redis_kill_client`) record priorState only.
- **Dry-run + double-confirm** — every write takes `dry_run=True` (MCP) /
  `--dry-run` (CLI); CLI writes double-confirm and execute through the same
  governed twins, so they land in the audit log too. `purge_queue` and
  `delete_queue` are risk=high with dry-run + double confirmation at the CLI.
- Credentials live **encrypted** in `~/.queue-aiops/secrets.enc` (Fernet +
  scrypt master password; `QUEUE_AIOPS_MASTER_PASSWORD` for non-interactive
  use). Redis passwords are optional — an auth-less lab instance is a
  supported target.

## MCP configuration

```json
{
  "mcpServers": {
    "queue-aiops": {
      "command": "uvx",
      "args": ["--from", "queue-aiops", "queue-aiops-mcp"],
      "env": {
        "QUEUE_AIOPS_MASTER_PASSWORD": "your-master-password"
      }
    }
  }
}
```

> **env-block caveat**: MCP clients launch the server with a *minimal*
> environment — your shell profile is not sourced. Anything the server needs
> (`QUEUE_AIOPS_MASTER_PASSWORD`, a relocated `QUEUE_AIOPS_HOME`, and any
> optional `QUEUE_AUDIT_APPROVED_BY`/`QUEUE_AUDIT_RATIONALE` audit annotations)
> must be set in the `env` block above, not just in your terminal.

Or, with the package installed: `queue-aiops mcp`.

## CLI reference (short)

```text
queue-aiops init | doctor | overview | mcp
queue-aiops secret set|list|migrate ...
queue-aiops redis info|memory|clients|slowlog|config-get|keyspace|bigkeys
queue-aiops redis config-set <param> <value> [--dry-run]
queue-aiops redis kill-client --id <id> | --addr <ip:port> [--dry-run]
queue-aiops rabbitmq overview|queues|queue|connections|channels|policies|nodes
queue-aiops rabbitmq purge|delete-queue|declare-queue <name> [--vhost /] [--dry-run]
queue-aiops rabbitmq set-policy|delete-policy <name> ... [--dry-run]
queue-aiops analyze memory|latency|backlog|churn
```

## Verification status

**Live-verified against Redis 7.4.9 and RabbitMQ 3.13.7 (2026-07-19/20).**
Connectivity, every Redis read (cross-checked against `redis-cli` ground truth), all
four analyses, and the full governance loop (real `redis_config_set` → audit row →
undo restoring the prior value) were exercised against a real server. That run found
and fixed a real defect: integer quantities — key counts, client counts, byte totals —
were rendered as floats (`202.0` keys), which equality assertions cannot catch.

The RabbitMQ group is now verified end-to-end too — reads cross-checked against
`rabbitmqadmin`, and `set_policy` → `undo_apply` closing on the live broker. **Redis
cluster/sentinel topologies and AUTH/TLS connections** remain unverified.

[docs/VERIFICATION.md](docs/VERIFICATION.md) records exactly what was checked and what
is still open. `queue-aiops doctor` is the fastest live check.


## Contributing

缺功能提 issue/PR 欢迎留言 — missing a read you need (streams/consumer groups,
quorum-queue specifics, shovel/federation status), another broker platform, or
a threshold that doesn't fit your fleet? Open an issue or PR at
[github.com/AIops-tools/Queue-AIops](https://github.com/AIops-tools/Queue-AIops)
— platform registry entries are additive and small.

## License

MIT
