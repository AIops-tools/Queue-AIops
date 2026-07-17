# queue-aiops v0.1.0 — 2026-07-17

First preview release: governed AI-ops for **redis + rabbitmq**.

## Highlights

- **Two platforms, one toolset**: a per-target `platform` field selects the
  protocol shape — redis over the RESP client (password optional, TLS
  optional), rabbitmq over the management HTTP API (Basic auth). The platform
  registry makes further brokers additive.
- **Four flagship RCAs** — transparent heuristics that show their numbers:
  - `redis_memory_pressure_rca` — used vs maxmemory + eviction policy,
    evicted-keys pressure, fragmentation/swap signals, big-key sample.
  - `redis_latency_rca` — SLOWLOG digested by command pattern (O(N) commands
    flagged), blocked clients, fork/AOF-fsync stalls from INFO persistence.
  - `rabbitmq_queue_backlog_rca` — per-queue cause (no consumers / unacked
    pileup / publish outpacing delivery) + global watermark-alarm findings.
  - `connection_churn_analysis` — both platforms; counts vs an optional prior
    snapshot, clients grouped by source.
- **Governed writes with honest undo**: config set (prior value captured),
  policies (prior policy captured; delete-if-new), delete_queue (undo
  re-declares the captured definition — messages are not restored, and the
  descriptor says so), purge/kill record priorState only.
- **Safety rails**: SCAN-budgeted big-key sampling (never `KEYS *`), typed
  redis command allow-list, centrally percent-encoded rabbitmq paths (default
  vhost `/` included), encrypted secret store, secure-by-default approver gate
  on high-risk writes, dry-run everywhere.

## Known limits (preview)

- Mock-validated only; not yet run against live production brokers.
- redis cluster-wide topology reads and rabbitmq shovel/federation status are
  out of scope for v0.1 — issues/PRs welcome.
