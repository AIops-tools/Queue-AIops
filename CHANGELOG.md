# Changelog

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
