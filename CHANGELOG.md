# Changelog

## Unreleased

### Fixed
- CLI commands that read the engine without calling an MCP tool now write an
  audit row, as every MCP call does: they run through the same `@governed_tool`
  harness, budget and runaway guard included. Before, an operator's CLI reads
  left no trace in `audit.db`, contrary to the documented guarantee that MCP
  and CLI are audited alike. A test fails if any engine command escapes it.

## v0.9.2 — 2026-09-12

### Changed
- **The ClawHub bundle plugin moved from `@aiops-tools/queue-aiops` to
  `@zw008/queue-aiops`**, matching the publisher the skill has always been under.
  ClawHub cannot move a package between scopes — the scope is the publisher
  identity — so this is a republish under the new name; the old name is
  withdrawn. Install with:
  `openclaw plugins install clawhub:@zw008/queue-aiops`. Nothing about the Python
  package, the CLI, the MCP server or the Claude Code plugin changes.

## v0.9.1 — 2026-09-12

### Added
- **The OpenClaw install path is documented.** The ClawHub bundle channel went
  live but neither the README nor this skill said how to install from it:
  `openclaw plugins install clawhub:@aiops-tools/queue-aiops`. States the `uvx`
  prerequisite (without it the skill installs but reports `Visible to model:
  no`) and that the MCP server is pinned to this exact release.
- **Where an exported master password lives** is now stated next to the
  instruction to export it: readable by every process the shell starts, and
  kept in shell history.

## v0.9.0 — 2026-09-12

### Added
- **Installable as a Claude Code plugin.** `.claude-plugin/plugin.json` plus a
  root `.mcp.json` make this repo a plugin, so `/plugin install queue-aiops@aiops-tools`
  delivers the skill and registers the MCP server in one step. The server is
  pinned to the exact package version the manifest declares, so an audit row
  stays traceable to the code that produced it. Nothing about the tool itself
  changed — the CLI and the standalone MCP server work exactly as before.
- **Installable from ClawHub as an OpenClaw bundle plugin** (`@aiops-tools/queue-aiops`): one install delivers the skill *and* its MCP
  server, pinned to this exact release. `clawhub.ai/plugins`.

### Fixed
- **The skill was invisible to the model in OpenClaw.** Its metadata
  declared `requires.config` (OpenClaw reads that as config *keys*, not file
  paths, so it can never be satisfied), `requires.env` and `requires.bins`
  naming our own CLI — which a plugin user never has on PATH — plus a
  `primaryEnv` that turned a config path into an API-key prompt. Measured on
  OpenClaw 2026.6.35: `Visible to model: no`. It now requires
  `anyBins: [queue-aiops, uvx]` — either one suffices — with every variable kept
  in `optional.env` (still declared, no longer a load gate), which the same
  command reports as `Visible to model: yes`.
## v0.8.0 — 2026-08-10

### Fixed
- **An undetermined outcome no longer exits as a plain failure.** A write whose response was lost carries *both* `error` and `outcomeUnknown`, and the harness deliberately judges unknown first when writing the audit row — the change may have taken effect, so a blind retry could apply it twice. The CLI guard judged `error` first, so the audit said "may have taken effect" while the exit status told a script it had not happened. The two layers now agree (exit 2, not 1), and a test pins the ordering so it cannot silently flip back.
- **The CLI reported a refused or failed governed write as a success.** 8 write call sites printed the governed twin's payload and exited **0** whatever it said — and `@tool_errors` flattens every refusal, guard rejection and upstream failure into `{"error": ...}` rather than raising, so nothing downstream of a `&&` chain or a CI step could tell a blocked write from a landed one. The dry-run path already exited non-zero, which made the asymmetry worse: the preview was stricter than the write it previews. Results now route through a `checked()` helper — exit 1 on an error payload, exit 2 on an undetermined outcome, unchanged on success. This defect class had been fixed repo-by-repo several times and kept coming back; an audit across the whole line found it live in **18 of the 24 tools at once (87 call sites)**, so each tool now carries an invariant test that fails if any future CLI command prints a governed result without checking it.

## v0.7.0 — 2026-08-10

### Fixed
- **`purge_queue` could never have worked against a real RabbitMQ.** The management-API client sent `Accept: application/json`, but the purge endpoint (`DELETE /api/queues/{vhost}/{name}/contents`) answers *204 No Content* and offers no JSON representation, so content negotiation failed and the broker returned **406 with an empty body** — on every purge, on every server. Accept is now `application/json, */*`; every other endpoint (GETs, PUT declare, DELETE queue, DELETE policy) was enumerated against a live broker and behaves identically either way. Verified on RabbitMQ 3.13.7 by purging a real 9-message backlog.
- **A 406 now explains itself.** The broker sends no body with a 406, so the generic branch produced a message with nothing in it — and, because the platform label already ends in "API", it read "RabbitMQ management API **API error**". There is now a 406 branch naming content negotiation as the cause, and the generic branch no longer doubles the word.
- **Two more integer quantities stayed integers** (bug class #2): a node's `memUsedBytes` / `memLimitBytes` came back as floats (`161222656.0`) while the `diskFreeBytes` beside them was an int — one payload disagreeing with itself — and the per-peer `channels` count rendered as `1.0` for one channel, from a `0.0` accumulator seed. Both found on a live broker; regression tests assert the type.
- **Write-path integer quantities stay integers** (bug class #2/#4). `ops/writes.py` still rendered whole-second and count fields through the float helper `num()` — `kill_client`'s `priorState.ageSeconds` came back as `49.0`, and the RabbitMQ purge/policy captures rendered `messages` and `priority` as floats too. It was missed in the earlier read-path sweep (the module did not even import `as_int`) and was inconsistent with `list_clients`, which already rendered the identical CLIENT LIST `age` field as an `int`. Now uses `as_int`; a regression test asserts the *type* (equality cannot catch `49 == 49.0`). Found live while verifying `kill-client` against a real blocked `BLPOP` client on Redis 7.4.

## v0.6.0 — 2026-08-03

### Fixed
- **A cluster node's key count no longer poses as the whole dataset.** `INFO keyspace` on a Redis Cluster node answers for that node's slots only, so `keyspace` / `overview` reported `totalKeys: 100` for a 3-master cluster actually holding 300 — a dataset-wide claim wrong by the shard count, with nothing in the payload to say otherwise. Both now carry `scope` (`node` vs `server`), `clusterMode`, and on a cluster a note explaining that the figure covers one node. Measured on a real 3-master Redis 7.4 cluster (100 / 92 / 108).
- **`undo apply` replays against the target the original write ran on.** It dispatched the inverse against whatever target the *caller* named — in practice the config's first entry — while the write's own target sat unused in the undo record. On a multi-target config the inverse therefore ran against the wrong host; it only looks harmless because the resource usually is not there, but two hosts holding the same name and the inverse **succeeds on the wrong one, silently**. An explicitly named target still wins. Line-wide: all 24 copies had the identical defect. Caught live in container-host-aiops, where a stop recorded against a Podman target replayed against a Portainer one.

## v0.5.0 — 2026-08-02

### Changed (BREAKING)
- **Requires MCP SDK 2.0** (`mcp[cli]>=2.0,<3.0`). `mcp.server.fastmcp` no longer exists in 2.0; the server is now built with `MCPServer` and reports its package version in the stdio handshake.

### Fixed
- **`undo apply` works from the CLI.** Every write tool is imported lazily inside its own CLI command, so a CLI-driven undo ran in a process where the inverse tool was never registered and failed with "inverse tool is not registered" — for every write tool. Only the MCP entry point, which imports the whole server, worked. Found while live-verifying against a real cluster.
- **An undetermined outcome is audited `unknown`, not `ok`.** The harness only classified a result as undetermined when the payload *also* carried an `error` key, so a write that looked successful but had not been confirmed was recorded as a success.
- **`as_int` no longer round-trips integers through float64**, which cannot represent values above 2**53 exactly. A line-wide sweep found only one of six vendored copies had actually been fixed after the original precision bug. The bool guard precedes the int short-circuit because `bool` subclasses `int` — otherwise `True` would be returned unchanged and serialised as `true` rather than a number.


## v0.4.0 — 2026-07-21

### Changed (BREAKING)
- **Removed the authorization layer** — read-only mode, the approver gate, and rules.yaml deny are gone. The skill no longer decides read vs write; that is the agent's judgement or the connecting account's permissions. `<PREFIX>_READ_ONLY` now has no effect (a startup warning is logged); `<PREFIX>_AUDIT_APPROVED_BY`/`_RATIONALE` are optional audit annotations.
- The retained guarantee is **unbypassable audit over MCP and CLI alike** — no unaudited entry point. Harness = audit + runaway safety guard + undo + sanitize; `risk_level` is a descriptive audit label, not a gate.

See RELEASE_NOTES.md for tool-specific changes.


## v0.3.0 — 2026-07-20

### Fixed
- **`redis_config_set` refuses the parameters that lock this tool out of the server**: `requirepass`, `masterauth`, `bind`, `protected-mode`, `maxclients`, `port`, `unixsocket`, `aclfile`, and anything `tls-*`.
- `list_clients` no longer returns this tool's own connection, and `redis_kill_client` refuses it — by id or by address.
- Harness: a write whose response is lost is audited `status=unknown`, not `error` — it may have taken effect. Undo tokens gain `effectVerified` (undo.db migrated in place).
- Harness: a dry-run no longer records an undo token, and no longer requires a named approver. Guards now run on the preview path.
- Truncated strings end in an ellipsis instead of being cut silently; error messages are capped at 800 chars, not 300.

See RELEASE_NOTES.md for the full detail.

## v0.1.1 — 2026-07-17

### Fixed
- Added the MCP Registry ownership marker (mcp-name) to the README so the server publishes to the MCP Registry.

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
