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
(`202 == 202.0`). Fixed with `as_int()` plus a regression test that asserts the
*type*, keeping genuine ratios (`hitRatePct`, `usedPctOfMax`, `opsPerSec`) as
floats. ⚠️ That round's claim of a fix "across the Redis and RabbitMQ reads"
was **overstated**: the 2026-08-10 RabbitMQ run found `memUsedBytes` /
`memLimitBytes` and the per-peer `channels` count still floats, and an earlier
2026-08-03 round found the whole *write* path untouched. The sweep was never as
wide as the sentence claimed — treat "fixed line-wide" claims as needing their
own enumeration.

## Not yet live-verified ⚠️

- ~~**RabbitMQ** — the entire `rabbitmq` command group and its management-API
  shapes are unit-tested only. This is now the largest gap in this repo.~~
  **Closed 2026-08-10 against a real RabbitMQ 3.13.7** (management plugin), with
  a seeded estate: 3 queues, a real backlog, a live pika consumer holding a
  connection/channel, and a policy.
  - All 7 reads cross-checked against `rabbitmqctl` / `rabbitmqadmin`:
    `overview` (33 ready messages, cluster name, node count), `queues`
    (per-queue counts and durability exact), `queue` detail, `connections`,
    `channels` (unacked and consumer counts), `policies`, `nodes`.
  - All 5 writes exercised end-to-end with their undo: `declare-queue` →
    `undo_apply` deleted it; `set-policy` → `undo_apply` removed it leaving the
    pre-existing policy untouched; `delete-queue` → `undo_apply` re-declared it
    with the captured `durable=false`; `purge` destroyed a real 9-message
    backlog and correctly recorded **no** undo.
  - 🔴 **`purge_queue` had never worked against a real broker.** The HTTP client
    sent `Accept: application/json`, and RabbitMQ's purge endpoint
    (`DELETE /api/queues/{vhost}/{name}/contents`) answers *204 No Content* with
    no JSON representation, so content negotiation failed and the broker
    returned **406 with an empty body** — every purge, on every server. The
    other endpoints were enumerated against the live broker and behave
    identically under either Accept value, so the fix is `application/json, */*`.
    The 406 message was also useless (no body to quote, and the label already
    ended in "API" so it read "…management API API error"); both fixed.
  - 🔴 Two integer quantities rendered as floats (bug class #2): a node's
    `memUsedBytes` / `memLimitBytes` (`161222656.0` sitting next to an integer
    `diskFreeBytes` — one payload disagreeing with itself), and the per-peer
    `channels` count (`1.0` for one channel, from a `0.0` accumulator seed).
  - Still untested: clustered RabbitMQ, quorum/stream queues, and TLS (AMQPS /
    HTTPS management).
- ~~Redis **cluster / sentinel** topologies (only standalone was exercised).~~
  **Both exercised 2026-08-03**, and the cluster one found a defect: a node's
  `totalKeys` was reported as the dataset total (100 on a 3-master cluster
  holding 300). Reads, `overview` and the RCAs otherwise behaved correctly on a
  cluster node, on a Sentinel-fronted primary, and on its replica (`role:
  slave`). Still untested: an actual **failover** through Sentinel, and any
  cluster-wide aggregate — this tool talks to one endpoint by design, which is
  exactly why the scope marker matters.
- ~~AUTH-enabled Redis and TLS connections.~~ **Both verified 2026-08-03**
  against a real Redis 7.4 (Docker). AUTH: without the password every read
  fails with a clear `Authentication required` envelope (nulls + `errors`, not
  a silent empty); with the password via the encrypted-store/env fallback,
  `overview`/`keyspace` matched `DBSIZE` exactly. TLS: `use_tls` with
  `verify_ssl: false` connects over a `rediss://` socket to a self-signed
  instance and returns the right key count; `verify_ssl: true` against the same
  self-signed cert fails with `CERTIFICATE_VERIFY_FAILED` — TLS verification is
  really enforced, not silently downgraded.
- ~~`kill-client` against a real blocked client.~~ **Verified 2026-08-03**: a
  client genuinely blocked on `BLPOP` was listed by `redis clients` (with the
  tool's own connection correctly excluded), the `--dry-run` preview recorded a
  `wouldKill` audit row *without* killing, the real kill removed it server-side
  (confirmed via `CLIENT LIST`), and its CLIENT LIST row was captured as
  `priorState`. Both the preview and the real call wrote audit rows through the
  same governed twin.

  **A real defect was found and fixed by this run** (bug class #2/#4): the write
  path rendered integer quantities as floats — `ageSeconds: 49.0` in
  `kill_client`'s `priorState`, and likewise `messages`/`priority` in the
  RabbitMQ purge/policy captures — because `ops/writes.py` still routed them
  through `num()`. It was missed in the earlier read-path sweep (it did not even
  import `as_int`), and was inconsistent with `list_clients`, which already
  rendered the identical CLIENT LIST `age` field as an `int`. Fixed with
  `as_int()` and a type-asserting regression test (equality cannot catch it:
  `49 == 49.0`); `ageSeconds` re-confirmed as `4` (int) on a live re-kill.
