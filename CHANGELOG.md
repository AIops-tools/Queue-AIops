# Changelog

## v0.1.0 — 2026-07-17

Initial preview release.

- **Platforms**: redis (RESP via the `redis` Python client; password optional,
  TLS optional) and rabbitmq (management HTTP API, Basic auth) behind a
  name-keyed platform registry; one config spans a mixed estate.
- **26 MCP tools** (19 reads, 7 governed writes), all wrapped by the bundled
  governance harness (audit / budget / risk tiers / undo).
- **Flagship RCAs**: `redis_memory_pressure_rca` (maxmemory, eviction policy,
  fragmentation, SCAN-budgeted big keys), `redis_latency_rca` (slowlog digest
  by command pattern, blocked clients, fork/AOF stalls),
  `rabbitmq_queue_backlog_rca` (zero/slow consumers, unacked pileups,
  memory/disk watermark blocks), `connection_churn_analysis` (both platforms,
  optional prior-snapshot deltas, clients by source).
- **Governed writes**: `redis_config_set` (undo = prior value from CONFIG GET),
  `redis_kill_client` (priorState = client row, no undo), `purge_queue` (high;
  priorState = message count, no undo), `delete_queue` (high; undo re-declares
  the captured definition), `declare_queue`, `set_policy` / `delete_policy`
  (undo = prior policy or delete-if-new). All take `dry_run`; CLI twins add
  double-confirm.
- **Safety**: typed redis command allow-list (no generic passthrough; never
  `KEYS *`), central percent-encoding of rabbitmq path segments (incl. the
  default vhost `/`), encrypted secret store (Fernet + scrypt), secure-by-default
  approver gate for high-risk writes.
- Preview / mock-only: exercised against mocked clients; not yet validated on
  live production brokers.
