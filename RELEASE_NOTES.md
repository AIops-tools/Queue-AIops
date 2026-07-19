# Release notes — queue-aiops 0.2.2

Previous release: 0.2.1.

## Fixed: the remaining float-typed integer quantities

0.2.1 converted the obvious counts, but a regex-driven sweep missed 19 more —
`consumers`, per-node `fdUsed`/`socketsUsed`, `queues`/`connections`/`channels`
object totals, connection `connectedAt`, slowlog `id`/`startTime`/`durationUs`, and
Redis `totalCommands`. A live RabbitMQ broker reported `"consumers": 0.0`.

These are integers now. Genuinely fractional values — `consumerUtilisation`,
`opsPerSec`, `fragmentationRatio`, and the `*Rate` fields — are unchanged.

## Live-verified: RabbitMQ

The entire RabbitMQ command group had never been run against a live broker. It has
now been exercised against **RabbitMQ 3.13.7**: `doctor`, every read
(`overview`, `queues`, `queue`, `connections`, `channels`, `policies`, `nodes`) with
queue depth cross-checked against `rabbitmqadmin` ground truth, the backlog and
churn analyses, and the full governance loop — `set_policy` really created a policy
on the broker, `priorState` recorded that it had not existed, and `undo_apply`
deleted it, with all three calls audited.

The platform guard was confirmed too: a Redis-only analysis against a RabbitMQ
target fails with a teaching error naming the mismatch.

See [docs/VERIFICATION.md](docs/VERIFICATION.md). Redis cluster/sentinel topologies
and AUTH/TLS connections remain unverified.
