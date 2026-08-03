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
  `maxmemory-policy`, captured `noeviction` as `priorState`, wrote an audit row,
  and `undo_apply` restored the prior value on the live server.

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
- ~~Redis **cluster / sentinel** topologies (only standalone was exercised).~~
  **Both exercised 2026-08-03**, and the cluster one found a defect: a node's
  `totalKeys` was reported as the dataset total (100 on a 3-master cluster
  holding 300). Reads, `overview` and the RCAs otherwise behaved correctly on a
  cluster node, on a Sentinel-fronted primary, and on its replica (`role:
  slave`). Still untested: an actual **failover** through Sentinel, and any
  cluster-wide aggregate — this tool talks to one endpoint by design, which is
  exactly why the scope marker matters.
- AUTH-enabled Redis and TLS connections.
- `kill-client` against a real blocked client.
