"""redis reads — INFO, memory, clients, slowlog, config, keyspace, big keys.

The day-to-day "is this cache healthy?" surface for redis targets. Every call
is resilient — a transport/parse failure surfaces as ``{"error": ...}`` instead
of raising — and all broker text is sanitised via ``s``.

Big-key sampling is **SCAN-based with a hard budget** (never ``KEYS *``): at
most ``SCAN_BUDGET_KEYS`` keys are walked in ``SCAN_PAGE``-sized pages and at
most ``MEMORY_SAMPLE_MAX`` of them are sized with ``MEMORY USAGE``, so the read
can never stall a production instance. Coverage is reported so the caller knows
how partial the sample is.
"""

from __future__ import annotations

from typing import Any

from queue_aiops.ops._util import as_int, as_obj, num, opt, pct, s

# Hard bounds for the SCAN-based big-key sample (never KEYS *).
SCAN_BUDGET_KEYS = 10_000  # max keys walked per call
SCAN_PAGE = 500  # SCAN COUNT hint per page
MEMORY_SAMPLE_MAX = 200  # max MEMORY USAGE calls per big-key sample
TOP_KEYS = 20  # rows returned, largest first

MAX_CLIENT_ROWS = 200
MAX_SLOWLOG_ROWS = 128


def server_info(conn: Any) -> dict:
    """[READ] Server identity + health basics from INFO server/clients/stats."""
    try:
        info = as_obj(conn.redis_info())
        return {
            "platform": conn.target.platform,
            "version": opt(info.get("redis_version")),
            "mode": opt(info.get("redis_mode")),
            "uptimeSeconds": as_int(info.get("uptime_in_seconds")),
            "role": opt(info.get("role")),
            "connectedClients": as_int(info.get("connected_clients")),
            "blockedClients": as_int(info.get("blocked_clients")),
            "opsPerSec": num(info.get("instantaneous_ops_per_sec")),
            "totalCommands": as_int(info.get("total_commands_processed")),
            "keyspaceHits": as_int(info.get("keyspace_hits")),
            "keyspaceMisses": as_int(info.get("keyspace_misses")),
            "hitRatePct": pct(
                num(info.get("keyspace_hits")),
                num(info.get("keyspace_hits")) + num(info.get("keyspace_misses")),
            ),
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def memory_stats(conn: Any) -> dict:
    """[READ] Memory posture: used vs maxmemory, policy, fragmentation, peaks."""
    try:
        info = as_obj(conn.redis_info("memory"))
        used = as_int(info.get("used_memory"))
        maxmem = as_int(info.get("maxmemory"))
        stats = as_obj(conn.redis_memory_stats())
        return {
            "usedBytes": used,
            "usedHuman": opt(info.get("used_memory_human")),
            "maxmemoryBytes": maxmem,
            "usedPctOfMax": pct(used, maxmem),
            "maxmemoryPolicy": opt(info.get("maxmemory_policy")),
            "fragmentationRatio": num(info.get("mem_fragmentation_ratio")),
            "rssBytes": as_int(info.get("used_memory_rss")),
            "peakBytes": as_int(info.get("used_memory_peak")),
            "keysCount": as_int(stats.get("keys.count")),
            "overheadBytes": as_int(stats.get("overhead.total")),
            "datasetBytes": as_int(stats.get("dataset.bytes")),
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def list_clients(conn: Any) -> dict:
    """[READ] Connected clients grouped by source address, busiest first."""
    try:
        clients = conn.redis_client_list()
        rows = [
            {
                "id": opt(c.get("id"), 32),
                "addr": opt(c.get("addr"), 64),
                "name": opt(c.get("name"), 64),
                "ageSeconds": as_int(c.get("age")),
                "idleSeconds": as_int(c.get("idle")),
                "lastCommand": opt(c.get("cmd"), 64),
                "db": opt(c.get("db"), 8),
                "flags": opt(c.get("flags"), 16),
            }
            for c in clients
        ]
        by_source: dict[str, int] = {}
        for r in rows:
            source = r["addr"].rsplit(":", 1)[0] or "(unknown)"
            by_source[source] = by_source.get(source, 0) + 1
        sources = sorted(
            ({"source": k, "clients": v} for k, v in by_source.items()),
            key=lambda e: e["clients"],
            reverse=True,
        )
        return {
            "total": len(rows),
            "bySource": sources[:50],
            "clients": rows[:MAX_CLIENT_ROWS],
            "returned": min(len(rows), MAX_CLIENT_ROWS),
            "limit": MAX_CLIENT_ROWS,
            "truncated": len(rows) > MAX_CLIENT_ROWS,
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def _command_text(raw: Any) -> str:
    """Fold a slowlog command cell (str/bytes/list) into one bounded string."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, (list, tuple)):
        raw = " ".join(
            p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p) for p in raw
        )
    return s(raw, 128)


def slowlog(conn: Any, count: int = MAX_SLOWLOG_ROWS) -> dict:
    """[READ] Recent SLOWLOG entries, slowest first.

    Returns a truncation envelope::

        {"entries": [...], "returned": 128, "limit": 128, "truncated": true}

    so a cut-off read announces itself. The slowest command on the server may be
    the one just past the cut-off; a bare list cannot say "there is more", and a
    smaller local model faced with a long result tends to report that nothing
    came back at all. One extra entry is requested from Redis so ``truncated``
    is *measured* rather than guessed from a length coincidence.
    """
    try:
        want = max(1, min(int(count), MAX_SLOWLOG_ROWS))
        entries = conn.redis_slowlog(want + 1)
        rows = [
            {
                "id": as_int(e.get("id")),
                "startTime": as_int(e.get("start_time")),
                "durationUs": as_int(e.get("duration")),
                "command": _command_text(e.get("command")),
                "clientAddr": opt(e.get("client_address"), 64),
                "clientName": opt(e.get("client_name"), 64),
            }
            for e in entries
            if isinstance(e, dict)
        ]
        rows.sort(key=lambda r: r["durationUs"], reverse=True)
        truncated = len(rows) > want
        kept = rows[:want]
        return {
            "entries": kept,
            "returned": len(kept),
            "limit": want,
            "truncated": truncated,
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def config_get(conn: Any, pattern: str = "*") -> dict:
    """[READ] CONFIG GET for a glob pattern (bounded, values sanitised)."""
    try:
        raw = conn.redis_config_get(s(pattern, 64) or "*")
        params = {s(k, 64): s(v, 128) for k, v in sorted(raw.items())}
        return {"total": len(params), "parameters": params}
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def keyspace(conn: Any) -> dict:
    """[READ] Per-db key counts + expiry coverage from INFO keyspace."""
    try:
        info = as_obj(conn.redis_info("keyspace"))
        dbs = []
        for name, cell in info.items():
            cell = as_obj(cell) if isinstance(cell, dict) else _parse_keyspace_cell(cell)
            keys = as_int(cell.get("keys"))
            expires = as_int(cell.get("expires"))
            dbs.append(
                {
                    "db": s(name, 16),
                    "keys": keys,
                    "expires": expires,
                    "expiresPct": pct(expires, keys),
                    "avgTtlMs": as_int(cell.get("avg_ttl")),
                }
            )
        return {"databases": dbs, "totalKeys": sum(d["keys"] for d in dbs)}
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def _parse_keyspace_cell(cell: Any) -> dict:
    """Parse a raw ``keys=1,expires=2,avg_ttl=3`` keyspace string."""
    out: dict[str, str] = {}
    for part in str(cell or "").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def big_key_sample(conn: Any, top: int = TOP_KEYS) -> dict:
    """[READ] SCAN-budgeted big-key sample — largest sampled keys first.

    Walks at most ``SCAN_BUDGET_KEYS`` keys with SCAN (pages of ``SCAN_PAGE``),
    sizes an evenly-spaced subset of at most ``MEMORY_SAMPLE_MAX`` keys with
    MEMORY USAGE, and returns the largest ones. Never ``KEYS *`` — the budget
    makes this safe on a big production keyspace, and ``coveragePct`` says how
    partial the sample is.
    """
    try:
        dbsize = conn.redis_dbsize()
        scanned: list[str] = []
        cursor = 0
        while True:
            cursor, keys = conn.redis_scan(cursor=cursor, count=SCAN_PAGE)
            scanned.extend(keys)
            if cursor == 0 or len(scanned) >= SCAN_BUDGET_KEYS:
                break
        scanned = scanned[:SCAN_BUDGET_KEYS]

        step = max(1, len(scanned) // MEMORY_SAMPLE_MAX)
        sampled = scanned[::step][:MEMORY_SAMPLE_MAX]
        sized = []
        for key in sampled:
            usage = conn.redis_memory_usage(key)
            if usage is not None:
                sized.append({"key": s(key, 128), "bytes": int(usage)})
        sized.sort(key=lambda e: e["bytes"], reverse=True)

        return {
            "dbsize": dbsize,
            "scannedKeys": len(scanned),
            "sampledKeys": len(sized),
            "coveragePct": pct(len(scanned), dbsize),
            "budget": {
                "scanBudgetKeys": SCAN_BUDGET_KEYS,
                "scanPage": SCAN_PAGE,
                "memorySampleMax": MEMORY_SAMPLE_MAX,
            },
            "topKeys": sized[: max(1, int(top))],
            "returned": min(len(sized), max(1, int(top))),
            "limit": max(1, int(top)),
            # Measured against the sized sample. Note this is truncation of the
            # *sample*, not of the keyspace — "coveragePct" is what tells you how
            # much of the keyspace was walked at all.
            "truncated": len(sized) > max(1, int(top)),
            "note": (
                "SCAN-based sample under a hard budget (never KEYS *): sizes are "
                "MEMORY USAGE on an evenly-spaced subset; coveragePct shows how "
                "partial the walk was."
            ),
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}
