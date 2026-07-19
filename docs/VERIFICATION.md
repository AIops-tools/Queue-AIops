# Live verification status

This document records what has and has not been validated against real Redis /
RabbitMQ servers, so the maturity claim is auditable rather than a vibe.

## Already live-verified ✅ — Redis 7.4.9, 2026-07-19

Exercised end-to-end against a real **Redis 7.4.9** server (Docker) seeded with
202 keys and a list-backed job queue:

- `doctor` against a live server: PING OK, no-AUTH lab path reported clearly.
- Every Redis read cross-checked against `redis-cli` ground truth:
  `overview`, `redis info/memory/clients/slowlog/keyspace/bigkeys`
  (key count matched `DBSIZE` exactly).
- All four analyses ran clean: `analyze memory/latency/backlog/churn`.
- Governance loop end-to-end: `redis_config_set` really changed
  `maxmemory-policy`, captured `noeviction` as `priorState`, wrote an audit row
  with approver, and `undo_apply` restored the prior value on the live server.

**A real defect was found and fixed by this run**: integer quantities were
rendered as floats — `202.0` keys, `1.0` connected clients, float byte counts —
because they were routed through `num()`. Values were arithmetically right but
semantically wrong, and equality assertions could not catch it
(`202 == 202.0`). Fixed with `as_int()` across the Redis and RabbitMQ reads,
plus a regression test that asserts the *type*, keeping genuine ratios
(`hitRatePct`, `usedPctOfMax`, `opsPerSec`) as floats.

## Not yet live-verified ⚠️

- **RabbitMQ** — the entire `rabbitmq` command group and its management-API
  shapes are unit-tested only. This is now the largest gap in this repo.
- Redis **cluster / sentinel** topologies (only standalone was exercised).
- AUTH-enabled Redis and TLS connections.
- `kill-client` against a real blocked client.
