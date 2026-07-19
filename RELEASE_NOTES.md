# Release notes — queue-aiops 0.2.1

Previous release: 0.2.0.

## Fixed: integer quantities were rendered as floats

Key counts, client counts and byte totals were routed through the float coercion
helper, so a live server reported `202.0` keys and `1.0` connected clients. The
values were arithmetically right but semantically wrong — a count cannot be
fractional, and a reader should not have to wonder whether `.0` means the number was
rounded.

These now use a new `as_int()` helper and come back as integers. Genuinely fractional
values (`hitRatePct`, `usedPctOfMax`, `opsPerSec`, `expiresPct`) are unchanged. Note
that equality assertions cannot catch this class of bug (`202 == 202.0`), so the
regression test asserts the *type*.

If you parse these fields, the JSON shape changes from `202.0` to `202`.

## Live-verified

This release was exercised end-to-end against a real **Redis 7.4.9** server: every
Redis read cross-checked against `redis-cli` ground truth, all four analyses, and the
full governance loop (real `redis_config_set` → audit row → `undo_apply` restoring the
prior value). See [docs/VERIFICATION.md](docs/VERIFICATION.md) — **RabbitMQ remains
mock-only** and is now the largest gap in this repo.
