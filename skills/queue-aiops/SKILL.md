---
name: queue-aiops
description: >
  Use this skill whenever the user needs to operate a redis cache or a rabbitmq broker — a one-shot overview, redis memory posture (used vs maxmemory, eviction policy, fragmentation), SLOWLOG and a SCAN-budgeted big-key sample (never KEYS *), connected clients, CONFIG get/set, rabbitmq queues with backlog depth, connections/channels, policies and node watermark alarms, four flagship RCAs (redis memory pressure, redis latency/slowlog, rabbitmq queue backlog, connection churn on both platforms), and governed writes (set a config parameter, kill a client, declare/purge/delete a queue, set/delete a policy).
  Always use this skill for "redis", "rabbitmq", "maxmemory", "eviction", "evicted keys", "big key", "slowlog", "why is my cache slow", "queue backlog", "messages piling up", "no consumers", "unacked messages", "memory watermark", "connection churn", "purge a queue", "rabbitmq policy" when the context is a redis or rabbitmq deployment.
  Do NOT use when the target is something other than a redis/rabbitmq broker (a hypervisor, storage appliance, backup product, container-orchestration cluster, database server, monitoring stack, or OT/industrial equipment) — route those to the appropriate other AIops-tools skill. Managed cloud queue services and other broker products are out of scope.
  Preview — governed broker operations with a built-in governance harness (audit, policy, token budget, undo, risk-tiers). Mock-validated only, not run against live production brokers; both platforms are free/self-hostable, so a lab container is the easiest live check.
installer:
  kind: uv
  package: queue-aiops
argument-hint: "[a queue/key/client id, or describe your cache/broker task]"
allowed-tools:
  - Bash
metadata: {"openclaw":{"requires":{"env":["QUEUE_AIOPS_CONFIG"],"bins":["queue-aiops"],"config":["~/.queue-aiops/config.yaml","~/.queue-aiops/secrets.enc"]},"optional":{"env":["QUEUE_AIOPS_MASTER_PASSWORD"]},"primaryEnv":"QUEUE_AIOPS_CONFIG","homepage":"https://github.com/AIops-tools/Queue-AIops","emoji":"📬","os":["macos","linux"]}}
compatibility: >
  Standalone, self-governed broker operations across redis (RESP wire protocol via the redis Python client; password optional — auth-less lab instances are supported — TLS optional) and rabbitmq (management HTTP API /api/..., HTTP Basic auth with a monitoring/management-tagged user) — preview. Each target in the config names its own platform, and a name-keyed platform registry selects the protocol shape, so one config can span a mixed estate. The governance harness (audit, policy, token/runaway budget, undo, risk-tiers) is bundled in the package — no external skill-family dependency.
  All write operations are audited to a local SQLite DB under ~/.queue-aiops/ (relocatable via QUEUE_AIOPS_HOME).
  Credentials: the redis password (optional) or the rabbitmq management password is stored ENCRYPTED in ~/.queue-aiops/secrets.enc (Fernet/AES-128 + scrypt-derived key) — never plaintext on disk. Run 'queue-aiops init' to onboard (it asks for the platform), or 'queue-aiops secret set <target>' to add one. The store is unlocked by a master password from QUEUE_AIOPS_MASTER_PASSWORD (non-interactive/MCP/CI) or an interactive prompt (CLI on a TTY). A legacy plaintext env var QUEUE_<TARGET_NAME_UPPER>_SECRET is still honoured as a fallback with a deprecation warning (migrate with 'queue-aiops secret migrate'). The secret is presented as AUTH at connect time (redis) or HTTP Basic auth (rabbitmq) and held only in memory; secrets are never logged or echoed.
  State-changing operations pass through the @governed_tool decorator (pre-check + budget guard + audit + risk-tier gate). purge_queue and delete_queue are risk=high with dry_run + an approver gate; purge is irreversible (priorState = the message count about to be destroyed), and delete_queue's undo re-declares the captured queue definition — the messages are NOT restored. Reversible writes (redis_config_set, set_policy, delete_policy, declare_queue) capture the real fetched before-state and record an inverse undo descriptor.
  Safety: the redis surface is a typed command allow-list (no generic passthrough) and big-key sampling is SCAN-based under a hard budget — never KEYS *. rabbitmq path segments (queue/policy names and the default vhost '/') are percent-encoded centrally.
  Webhooks: none — no outbound network calls beyond the configured redis instances / rabbitmq management API.
  Transitive dependencies: the redis Python client, httpx (HTTP client), and the MCP SDK. No post-install scripts or background services.
  PREVIEW: mock-validated only — not run against live production brokers. Both platforms are free/self-hostable, so a lab container is the easiest live check.
---

# Queue AIops (preview)

> **Disclaimer**: Community-maintained open-source project, **not affiliated with, endorsed by, or sponsored by the Redis or RabbitMQ projects or their respective owners.** Redis and RabbitMQ are trademarks of their respective owners. Source at [github.com/AIops-tools/Queue-AIops](https://github.com/AIops-tools/Queue-AIops) under the MIT license.

Governed broker operations — **26 MCP tools** across **redis** (RESP client) and
**rabbitmq** (management HTTP API), every one wrapped with the bundled
`@governed_tool` harness: a local unified audit log under `~/.queue-aiops/`,
policy engine, token/runaway budget guard, undo-token recording, and
graduated-autonomy risk tiers. A per-target `platform` field selects the
protocol shape, so one config can span a mixed estate. The redis password /
rabbitmq management password is stored **encrypted**
(`~/.queue-aiops/secrets.enc`, Fernet + scrypt) — never plaintext on disk.

> **Standalone**: the governance harness is bundled in the package
> (`queue_aiops.governance`) — no external skill-family dependency.
> **Preview / mock-only**: not run against live production brokers; both
> platforms are free/self-hostable, so a lab container is the easiest live check.

## What This Skill Does

| Group | Tools | Count | R/W |
|-------|-------|:-----:|:---:|
| **Overview** | queue_overview | 1 | read |
| **redis** | redis_server_info, redis_memory_stats, redis_clients, redis_slowlog, redis_config_get, redis_keyspace, redis_big_keys | 7 | read |
| **rabbitmq** | rabbitmq_overview, list_queues, queue_detail, list_connections, list_channels, list_policies, node_health | 7 | read |
| **Flagship analyses** | redis_memory_pressure_rca, redis_latency_rca, rabbitmq_queue_backlog_rca, connection_churn_analysis | 4 | read |
| **Writes** | redis_config_set, redis_kill_client, declare_queue, set_policy, delete_policy | 5 | write (med) |
| **Writes** | purge_queue, delete_queue | 2 | write (**high**) |

The four flagship analyses are transparent heuristics that report their numbers,
never a black-box verdict: `redis_memory_pressure_rca` reads used-vs-maxmemory +
eviction policy + fragmentation + the big-key sample into a cause + action;
`redis_latency_rca` digests the SLOWLOG by command pattern and adds fork/AOF
stall signals; `rabbitmq_queue_backlog_rca` classifies each deep queue (no
consumers / unacked pileup / rate deficit) and reports watermark alarms that
block all publishers; `connection_churn_analysis` works on both platforms and
pins churn to client sources.

## Quick Install

```bash
uv tool install queue-aiops
queue-aiops init       # wizard: pick platform (redis/rabbitmq) + encrypted secret
queue-aiops doctor
```

## When to Use This Skill

- Get a one-shot snapshot (`overview` / `redis_server_info` / `rabbitmq_overview`)
- Investigate cache memory pressure (`analyze memory`) → cause + action
  (raise maxmemory vs fix eviction policy vs split big keys)
- Chase latency (`analyze latency`, `redis slowlog`) → O(N) command patterns,
  blocked clients, fork/AOF stalls
- Triage a growing queue (`analyze backlog`, `rabbitmq queues`) → per-queue
  cause (no consumers / unacked pileup / slow consumers) + watermark alarms
- Spot connection churn or leaks (`analyze churn`, `redis clients`,
  `rabbitmq connections`) — clients grouped by source
- Safely change state: `redis config-set` (undo = prior value), `rabbitmq
  set-policy`/`delete-policy` (undo = prior policy), `declare-queue`, and the
  high-risk `purge`/`delete-queue` (dry-run + approver; messages are not
  restorable)

**Do NOT use when** the target is not a redis/rabbitmq broker — route
hypervisor, storage, backup, cluster/orchestration, database, network,
monitoring-stack, or OT/industrial work to the appropriate other AIops-tools
skill.

## Related Skills — Skill Routing

| If the user wants… | Use |
|--------------------|-----|
| redis / rabbitmq cache & broker ops | **queue-aiops** (this skill) |
| A non-broker platform (hypervisor, storage, backup, cluster, database, network, monitoring stack, OT edge) | the appropriate **other AIops-tools** skill |
| Managed cloud queue services / other broker products | out of scope for this tool |

## Common Workflows

### Cache is out of memory / evicting

1. `redis memory` → used vs maxmemory, policy, fragmentation
2. `redis bigkeys` → SCAN-budgeted sample of the largest keys (with coverage %)
3. `analyze memory` → ranked findings with numbers: noeviction-near-limit
   (writes will OOM), active eviction, fragmentation vs swapping, oversized keys

### Cache is slow

1. `redis slowlog` → slowest entries
2. `analyze latency` → slowlog digested by command pattern (O(N) commands
   flagged with an incremental-variant suggestion), blocked clients, fork/AOF
   stall signals from INFO persistence

### Queue keeps growing

1. `rabbitmq queues` → deepest backlog first
2. `analyze backlog` → per-queue cause: no consumers attached, consumers not
   acking (unacked pileup), publish rate outpacing delivery — plus global
   memory/disk watermark alarms that block every publisher
3. If agreed: `rabbitmq purge <queue> --dry-run` → preview, then re-run with
   double-confirm and an approver (`QUEUE_AUDIT_APPROVED_BY`)

### Change a config value safely (reversible)

1. `redis config-get maxmemory*` → current values
2. `redis config-set maxmemory-policy allkeys-lru --dry-run` → preview
3. Re-run without `--dry-run` (double-confirm) — it captures the prior value
   from CONFIG GET and records an inverse undo descriptor

## Governance & Safety

- Every tool is audited to `~/.queue-aiops/audit.db` (relocatable via
  `QUEUE_AIOPS_HOME`).
- **Secure by default**: with no `~/.queue-aiops/rules.yaml`, high-risk ops
  (`purge_queue`, `delete_queue`) are denied unless `QUEUE_AUDIT_APPROVED_BY`
  names an approver (set `QUEUE_AUDIT_RATIONALE` too). `queue-aiops init`
  seeds a starter rules.yaml; an operator-authored rules file is honoured
  as-is.
- Writes support `--dry-run` and double confirmation at the CLI; CLI writes
  execute through the governed twins, so they are audited too.
- Reversible writes capture the real fetched before-state and record an
  inverse descriptor; `purge_queue` and `redis_kill_client` are irreversible
  and record priorState only. `delete_queue`'s undo restores the queue
  *definition*, never its messages — the descriptor says so.
- Big-key sampling is SCAN-budgeted (never `KEYS *`); the redis surface is a
  typed command allow-list; rabbitmq paths are centrally percent-encoded
  (default vhost `/` included).

## References

- `references/capabilities.md` — full tool + platform + API/command reference
- `references/cli-reference.md` — CLI command reference
- `references/setup-guide.md` — onboarding, credentials, and connectivity
