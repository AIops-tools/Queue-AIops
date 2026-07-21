# Security Policy

## Disclaimer

Community-maintained open-source project. **Not affiliated with, endorsed by, or
sponsored by the Redis or RabbitMQ projects or their respective owners.** Product and
trademark names (Redis, RabbitMQ) belong to their owners. Source is auditable
under the MIT license.

## Reporting Vulnerabilities

Report privately via a GitHub Security Advisory on
[github.com/AIops-tools/Queue-AIops](https://github.com/AIops-tools/Queue-AIops/security/advisories)
or email zhouwei008@gmail.com. Please do not open public issues for security
reports.

## Security Design

### Credential Management
- Per-target secrets — the redis **password** (optional; auth-less lab
  instances are a supported setup) or the rabbitmq management user's
  **password** (paired with `username` for HTTP Basic auth) — live
  **encrypted** in `~/.queue-aiops/secrets.enc` (Fernet/AES-128 +
  scrypt-derived key; chmod 600), never in `config.yaml` and never in source.
  The master password is never stored — only a per-store random salt and the
  ciphertext are on disk.
- A legacy plaintext env var `QUEUE_<TARGET_NAME_UPPER>_SECRET` is still
  honoured as a fallback with a deprecation warning (migrate with
  `queue-aiops secret migrate`).
- The secret is held only in memory and never logged or echoed. It is presented
  as `AUTH` at connect time (redis) or HTTP Basic auth (rabbitmq management
  API); the config file holds only platform, host, port, username, db, and TLS
  settings.

### Governed Operations
Every MCP tool runs through the bundled `@governed_tool` harness
(`queue_aiops.governance`):
- **Audit** — every call logged to a local SQLite DB under `~/.queue-aiops/`
  (relocatable via `QUEUE_AIOPS_HOME`), agent-attributed, secret-redacted.
- **Token/runaway budget** — hard ceilings (`QUEUE_MAX_TOOL_CALLS` /
  `QUEUE_MAX_TOOL_SECONDS`) plus an on-by-default guard that trips a tight
  poll/retry loop, preventing unbounded API consumption.
- **Risk tier** — a descriptive label on each audit row derived from
  `risk_level`; it gates nothing. `QUEUE_AUDIT_APPROVED_BY` /
  `QUEUE_AUDIT_RATIONALE` are optional annotations recorded on the row, never
  required and never blocking.
- **Undo-token recording** — reversible writes capture the BEFORE state (via a
  real read) and record an inverse descriptor (e.g. `redis_config_set` restores
  the prior value from CONFIG GET; `delete_queue` re-declares the captured
  queue definition; `set_policy`/`delete_policy` restore the prior policy) so
  the change can be rolled back.

### State-Changing Operations
The destructive writes — `purge_queue` and `delete_queue` — are
`risk_level=high`, accept a `dry_run` preview, and require double confirmation
at the CLI. Purged/deleted **messages cannot be restored** — `purge_queue` records the
prior message count as audit evidence (no undo) and `delete_queue`'s undo
restores the queue *definition* only. `redis_config_set`, `redis_kill_client`
(priorState = the client's CLIENT LIST row; no undo), `declare_queue`,
`set_policy`, and `delete_policy` are `risk_level=medium`; reversible ones
capture before-state and record an undo token.

### Command Surface (redis)
The connection layer exposes a **typed allow-list of commands only** (INFO,
SLOWLOG, CLIENT LIST/KILL, CONFIG GET/SET, MEMORY STATS/USAGE, SCAN, DBSIZE,
PING) — there is no generic "run any command" passthrough, and big-key
sampling is SCAN-based under a hard budget (never `KEYS *`). `CONFIG SET`
parameter names are validated against a strict pattern before reaching the
wire.

### SSL/TLS Verification
`verify_ssl` defaults to true when TLS is enabled; disable only for self-signed
lab certificates.

### Prompt-Injection Protection
All broker-returned text (key names, client addresses, slowlog commands, queue
and policy names, node names) is passed through a `sanitize()` truncate +
control-character strip before reaching the agent. RabbitMQ path segments —
including the default vhost `/` — are percent-encoded centrally so an
agent-supplied name can never rewrite a request path.

### Network Scope
No webhooks, no telemetry, no outbound calls beyond the configured redis
instances and rabbitmq management API endpoints. No post-install scripts or
background services.

## Static Analysis

```bash
uvx bandit -r queue_aiops/ mcp_server/
uv run ruff check .
```

## Supported Versions

The latest released version receives security fixes. This is a preview (0.x);
pin a version in production.
