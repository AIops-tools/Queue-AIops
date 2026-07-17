<!-- mcp-name: io.github.AIops-tools/queue-aiops -->

# Queue AIops (preview)

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
> **Preview / mock-only**: not yet validated against production brokers. Both
> redis and rabbitmq are free/self-hostable (one lab container or package install each), so a lab
> check is easy — `queue-aiops doctor` is the fastest live probe.

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

## Support scope

| Platform | Protocol | Coverage |
|----------|----------|----------|
| **redis** (5.x–7.x wire protocol via `redis` Python client) | RESP, password optional, TLS optional | INFO (server/memory/clients/stats/persistence/keyspace), SLOWLOG, CLIENT LIST/KILL, CONFIG GET/SET, MEMORY STATS/USAGE, SCAN-budgeted big-key sampling, DBSIZE, PING |
| **rabbitmq** (management plugin HTTP API) | HTTP(S), Basic auth | /api/overview, /api/queues (+ per-vhost, detail, purge, declare, delete), /api/connections, /api/channels, /api/consumers, /api/policies (get/set/delete), /api/nodes |

**26 MCP tools** — 19 reads (incl. 4 flagship RCAs) + 7 governed writes.

| Group | Tools | R/W |
|-------|-------|:---:|
| Overview | queue_overview | read |
| redis reads | redis_server_info, redis_memory_stats, redis_clients, redis_slowlog, redis_config_get, redis_keyspace, redis_big_keys | read |
| rabbitmq reads | rabbitmq_overview, list_queues, queue_detail, list_connections, list_channels, list_policies, node_health | read |
| Flagship RCAs | redis_memory_pressure_rca, redis_latency_rca, rabbitmq_queue_backlog_rca, connection_churn_analysis | read |
| Writes (medium) | redis_config_set, redis_kill_client, declare_queue, set_policy, delete_policy | write |
| Writes (**high**) | purge_queue, delete_queue | write |

The four RCAs are transparent heuristics that report their numbers — thresholds
are named constants, every finding carries its evidence, never a black-box
verdict. Big-key sampling walks at most 10,000 keys with SCAN and sizes at most
200 with MEMORY USAGE — **never `KEYS *`** — and reports its coverage.

## Governance

Every MCP tool runs through the bundled `@governed_tool` harness
(`queue_aiops.governance` — no external dependency):

- **Audit** — every call lands in `~/.queue-aiops/audit.db` (relocatable via
  `QUEUE_AIOPS_HOME`), secret-redacted.
- **Budget** — call/time ceilings (`QUEUE_MAX_TOOL_CALLS`,
  `QUEUE_MAX_TOOL_SECONDS`) + a runaway-loop breaker.
- **Risk tiers & approval** — **secure by default**: with no
  `~/.queue-aiops/rules.yaml`, high-risk writes (`purge_queue`,
  `delete_queue`) are denied unless `QUEUE_AUDIT_APPROVED_BY` names an approver
  (set `QUEUE_AUDIT_RATIONALE` too). `queue-aiops init` seeds a starter
  rules.yaml with that dual-control tier; an operator-authored rules file is
  honoured as-is.
- **Undo** — reversible writes capture the real before-state first:
  `redis_config_set` records the prior value from CONFIG GET;
  `set_policy`/`delete_policy` record the prior policy; `delete_queue` records
  the queue's definition and its undo re-declares it (the **messages are not
  restored** — the descriptor says so). Irreversible writes (`purge_queue`,
  `redis_kill_client`) record priorState only.
- **Dry-run + double-confirm** — every write takes `dry_run=True` (MCP) /
  `--dry-run` (CLI); CLI writes double-confirm and execute through the same
  governed twins, so they land in the audit log too.
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
> (`QUEUE_AIOPS_MASTER_PASSWORD`, a relocated `QUEUE_AIOPS_HOME`,
> `QUEUE_AUDIT_APPROVED_BY` for high-risk writes) must be set in the `env`
> block above, not just in your terminal.

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

Preview / mock-only: the full test suite runs against mocked clients (no live
broker needed), and the REST paths / INFO fields are modelled from the public
docs of both platforms. Not yet validated against live production brokers —
both are trivially self-hostable, so `queue-aiops doctor` against a
local lab redis instance / rabbitmq broker (management plugin enabled) is
the quickest live check.

## Contributing

缺功能提 issue/PR 欢迎留言 — missing a read you need (streams/consumer groups,
quorum-queue specifics, shovel/federation status), another broker platform, or
a threshold that doesn't fit your fleet? Open an issue or PR at
[github.com/AIops-tools/Queue-AIops](https://github.com/AIops-tools/Queue-AIops)
— platform registry entries are additive and small.

## License

MIT
