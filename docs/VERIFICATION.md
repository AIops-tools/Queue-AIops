# Live verification — redis / rabbitmq

`queue-aiops` is exercised by a **mock-only** test suite (`uv run pytest`, no real
broker). It has **not** yet been validated end-to-end against a live redis instance or
rabbitmq broker. This document says exactly what the mock suite already guarantees, and
what a live run has to prove before anyone may describe this tool as verified against a
real broker.

It is deliberately checklist-shaped so the result is reproducible and auditable — not a
subjective "seems fine".

## What the mock suite already guarantees

- Every module imports; the CLI builds; **all 28 MCP tools** carry the `@governed_tool`
  harness marker (`tests/test_smoke.py`, which also asserts the tool count and that
  `__version__` matches `pyproject.toml`).
- The four flagship analyses (`redis_memory_pressure_rca`, `redis_latency_rca`,
  `rabbitmq_queue_backlog_rca`, `connection_churn_analysis`) are unit-tested against
  synthetic telemetry: each cause classification fires on the right signal
  (noeviction-near-limit vs active eviction vs fragmentation vs swapping; O(N) command
  patterns and fork/AOF stalls; no-consumers vs unacked pile-up vs rate deficit),
  findings cite the measured number, and partial/missing fields do not crash.
- The **platform registry** resolves each tool to the correct redis (RESP, via the
  `redis` Python client) or rabbitmq (management HTTP API, HTTP Basic) shape.
- **Safety invariants** are tested: the redis surface is a typed command **allow-list**
  with no generic passthrough; big-key sampling is `SCAN`-based under a hard budget and
  **never** issues `KEYS *`; and rabbitmq path segments (queue/policy names, and the
  default vhost `/` → `%2F`) are percent-encoded centrally.
- Reversible writes record a faithful **inverse** undo descriptor built from a fetched
  before-state (`redis_config_set` restores the prior value from `CONFIG GET`;
  `set_policy` / `delete_policy` restore the captured definition; `declare_queue`'s undo
  deletes it only when newly created). `purge_queue` declares **no** undo;
  `delete_queue`'s undo re-declares the captured definition and the tests assert that
  **messages are not claimed to be restored**.
- Governance persistence is tested against a real on-disk SQLite audit DB: calls land as
  rows, failures record `status=error` and no undo, and the secure-by-default approver
  gate refuses high-risk ops when no `rules.yaml` exists.

What it does **not** guarantee: that the concrete `INFO` field names, `SLOWLOG` entry
shape, and rabbitmq management-API JSON match a real broker build. Those are modelled
from each project's public documentation and are the **largest verification debt in this
repo** — `INFO` field names in particular have shifted across redis 5/6/7.

## Prerequisites for a live run

Both platforms are free and trivially containerised:

```bash
docker run -d --name redis-verify -p 6379:6379 redis:7 \
  redis-server --maxmemory 128mb --maxmemory-policy noeviction
docker run -d --name rabbit-verify -p 5672:5672 -p 15672:15672 rabbitmq:3-management
```

The rabbitmq **management plugin** must be enabled (the `-management` image does this),
and the API user needs the `monitoring` or `management` tag.

Use a **throwaway broker with throwaway data**. The checklist purges and deletes queues,
kills clients, and changes the running config — `purge_queue` destroys real messages and
records **no undo**. Never point it at a broker carrying traffic you care about.

Set the lab up so the reads have something to find:

- Fill redis past 80% of a small `maxmemory` and leave the policy at `noeviction`, so the
  most dangerous finding is reachable.
- Create a few deliberately oversized keys (a large hash and a large list).
- Run some `KEYS`/`SMEMBERS` calls so the SLOWLOG has genuine O(N) entries.
- On rabbitmq, create three queues: one with **no consumer** and messages published, one
  with a consumer that **never acks**, and one healthy — the three backlog causes.

```bash
uv tool install queue-aiops
queue-aiops init      # wizard: pick platform, optional encrypted secret
```

Record the exact versions tested (e.g. "redis 7.4, rabbitmq 3.13") — a tick is only
meaningful with the build it was ticked against.

## Verification checklist

Tick every box, **per platform**. A box that cannot be ticked is a verification gap —
record it, do not silently pass.

### 1. Connectivity (the fastest live gate)
- [ ] `queue-aiops doctor` → green on each target: config parsed, secret store unlocks,
      and a real `PING` / `/api/overview` returns.
- [ ] `queue-aiops doctor --skip-auth` → passes offline (config/secret checks only).
- [ ] An **auth-less** lab redis connects with no stored secret (this is a supported
      configuration and a common mock-vs-reality divergence).
- [ ] One config spanning both platforms works with `--target` switching.

### 2. Reads return real, well-shaped data
- [ ] `queue-aiops overview` → real version/role, memory posture, client counts, ops/sec,
      hit rate and key count (redis); version, queue/message totals, connection/channel/
      consumer counts and node alarms (rabbitmq). Confirm a **partial** failure degrades
      into the `errors` list rather than failing the whole call.
- [ ] `queue-aiops redis info` / `redis memory` → fields match `redis-cli INFO`
      **on each of redis 6 and 7**, since field names have moved between versions.
- [ ] `queue-aiops redis clients` → matches `CLIENT LIST`; `redis keyspace` matches
      `DBSIZE` per database.
- [ ] `queue-aiops redis slowlog --limit 50` → matches `SLOWLOG GET`, with timings
      parsed correctly.
- [ ] `queue-aiops redis config-get maxmemory*` → glob patterns work and match
      `CONFIG GET`.
- [ ] `queue-aiops redis bigkeys --count 500` → finds the oversized keys you planted, and
      reports a **coverage %** consistent with the keyspace size. Confirm with
      `MONITOR` or the slowlog that it issued `SCAN`, **never** `KEYS *` — this is the
      single most important safety check in the file.
- [ ] `queue-aiops rabbitmq queues` / `queue <name>` / `connections` / `channels` /
      `policies` / `nodes` → match the management UI, including ready-vs-unacked splits.
- [ ] A queue and a policy whose **names need percent-encoding** (a `/`, a space, a `%`)
      are read correctly, and the default vhost `/` resolves as `%2F`.

### 3. The analyses are right, not just non-crashing
- [ ] With redis near `maxmemory` under `noeviction`,
      `queue-aiops analyze memory --used-pct 85` raises the **noeviction-near-limit**
      finding and cites the real used/max numbers. Switch to `allkeys-lru`, drive
      evictions, and confirm the finding changes to active eviction.
- [ ] Fragmentation vs swapping are distinguished correctly (do not accept "flagged
      something" — check it names the right one).
- [ ] `queue-aiops analyze latency --slow-us 10000` digests the planted O(N) entries by
      **command pattern** and suggests the incremental variant (`KEYS` → `SCAN`).
- [ ] Trigger a background save on a large dataset; the fork/AOF stall signal appears.
- [ ] `queue-aiops analyze backlog --top 20` classifies each of your three planted
      queues correctly: **no consumers**, **unacked pile-up**, and healthy.
- [ ] Drive the node past its memory watermark; the alarm is reported as blocking **all**
      publishers, and outranks the per-queue findings.
- [ ] `queue-aiops analyze churn` on both platforms pins reconnect churn to the right
      client source.

### 4. A reversible write + its undo (governance closes the loop)
- [ ] `queue-aiops redis config-set maxmemory-policy allkeys-lru --dry-run` → prints the
      call, `CONFIG GET` shows the value unchanged.
- [ ] `queue-aiops redis config-set maxmemory-policy allkeys-lru` → the running config
      changes, the result carries an `_undo_id`, and a row lands in
      `~/.queue-aiops/audit.db`.
- [ ] `queue-aiops undo list` shows it; `undo apply <id>` restores the **prior** policy —
      verify starting from a non-default value, where a naive undo would wrongly set
      `noeviction`.
- [ ] `queue-aiops rabbitmq set-policy <name> <pattern> '{"max-length":1000}'` over an
      **existing** policy, then `undo apply` → the prior definition returns intact (not
      a deletion).
- [ ] `queue-aiops rabbitmq declare-queue <new> --dry-run` then for real, then
      `undo apply` → the newly created queue is deleted. Repeat against a queue that
      **already existed**: the undo must **not** delete it.

### 5. Irreversible / asymmetric writes behave as declared
- [ ] `queue-aiops rabbitmq purge <queue> --dry-run` → reports the message count about to
      be destroyed; the real run destroys exactly that many and records **no** undo.
- [ ] `queue-aiops rabbitmq delete-queue <queue>` on a queue **with messages**, then
      `undo apply` → the queue is re-declared with the right durable/auto-delete flags,
      and the **messages are gone**. Confirm the tool never claimed otherwise.
- [ ] `queue-aiops redis kill-client --addr <ip:port>` drops that connection, records no
      undo, and is audited.

### 6. Governance actually gates
- [ ] With no `~/.queue-aiops/rules.yaml`, `purge_queue` and `delete_queue` are
      **refused** unless `QUEUE_AUDIT_APPROVED_BY` is set (secure-by-default); with it
      plus `QUEUE_AUDIT_RATIONALE`, both appear in the audit row.
- [ ] A tight poll loop trips the runaway budget guard rather than hammering the broker.
- [ ] A failed call (nonexistent queue) is audited `status=error` and records no undo.
- [ ] Attempting a redis command outside the typed allow-list is rejected **before** the
      client sends anything — confirm against the live server that nothing executes.

### 7. Cleanup
- [ ] Restore every config value you changed, delete the throwaway queues and policies,
      and flush the lab redis.
- [ ] `queue-aiops overview` matches the baseline you captured before starting.
- [ ] Skim `~/.queue-aiops/audit.db` — every write is there with the right risk tier.

## Criteria to consider it live-verified

All of the following must hold:

1. Every box above is ticked against **both** a real redis and a real rabbitmq, with the
   exact builds recorded (e.g. "redis 7.4 + rabbitmq 3.13"), and the redis reads
   re-checked on at least **two major redis versions** given the `INFO` field drift.
2. Every field-name or API-shape mismatch found is **fixed and covered by a regression
   test**, so the mock suite would now catch it.
3. Section 4 passed, including the asymmetric `declare_queue` case (undo must delete only
   a queue it created). Recording an undo descriptor is not the same as the undo
   working, and this product line has shipped broken undo pairs before.
4. The big-key sampler was confirmed **live** to issue `SCAN` under budget and never
   `KEYS *` — a mock cannot prove this, and getting it wrong stalls a production redis.
5. The run is written up in the release notes / product-line memory with the date and
   package version, matching how the line records its other live-verified tools.

Until then, this repo says only what is true: mock-validated, live-unverified. Claiming
otherwise would break that promise.

## Notes for maintainers

- `queue-aiops doctor` is the single fastest live entry point; start there.
- Weight the run toward **redis `INFO` field names** and **rabbitmq percent-encoded path
  segments** — those are the two places the mocks are least able to model.
- Managed cloud queue services (ElastiCache, Amazon MQ, CloudAMQP, …) are a **separate**
  verification target: wire-compatible reads may work, but `CONFIG SET` and
  `CLIENT KILL` are commonly restricted. Do not infer managed support from a self-hosted
  run.
- Add this tool's result to the product-line verification ledger once green, so the
  central "verification debt" list stays accurate.
