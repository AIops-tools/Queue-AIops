"""Flagship signature analyses over broker telemetry (pure analysis).

These are the differentiators — transparent heuristics, every flag reported
with its numbers so an operator can see *why* something was ranked, never a
black-box verdict:

  1. ``redis_memory_pressure_rca`` — used vs maxmemory, eviction policy +
     evicted-keys pressure, fragmentation ratio, and the SCAN-budgeted big-key
     sample → cause + action.
  2. ``redis_latency_rca`` — SLOWLOG digested by command pattern, blocked
     clients, and fork/AOF stalls from INFO persistence → cause + action.
  3. ``rabbitmq_queue_backlog_rca`` — growing queues with zero/slow consumers,
     unacked pileups, and memory/disk watermark blocks → cause per queue +
     action.
  4. ``connection_churn_analysis`` — both platforms: connection/channel counts
     vs an optional prior snapshot, plus clients grouped by source.

All four are pure functions (no I/O): pass them the telemetry (from the reads
in the other ops modules, or injected) and they return the analysis. The live
pulls that feed them live in the ``pull_*`` helpers.
"""

from __future__ import annotations

import time
from typing import Any

from queue_aiops.ops import rabbit_reads, redis_reads
from queue_aiops.ops._util import as_obj, num, opt, pct
from queue_aiops.platform import REDIS

MAX_ROWS = 100

# ── 1. redis memory-pressure RCA ─────────────────────────────────────────────
DEFAULT_USED_PCT = 85.0  # used/maxmemory % at/above which pressure is flagged
FRAG_HIGH_RATIO = 1.5  # mem_fragmentation_ratio at/above which frag is flagged
FRAG_SWAP_RATIO = 0.8  # ratio below which RSS < used ⇒ likely OS swapping
BIG_KEY_MIN_BYTES = 10 * 1024 * 1024  # 10 MiB — a sampled key this big is flagged
NOEVICTION = "noeviction"


def pull_memory_telemetry(conn: Any) -> dict:
    """[READ] Live memory + eviction + big-key telemetry for the RCA."""
    stats = as_obj(conn.redis_info("stats"))
    return {
        "memory": redis_reads.memory_stats(conn),
        "evictedKeys": num(stats.get("evicted_keys")),
        "expiredKeys": num(stats.get("expired_keys")),
        "bigKeys": redis_reads.big_key_sample(conn),
    }


def redis_memory_pressure_rca(
    memory: dict,
    evicted_keys: float = 0,
    big_keys: dict | None = None,
    used_pct: float = DEFAULT_USED_PCT,
) -> dict:
    """[READ] Diagnose redis memory pressure → cause + action, with numbers.

    Pure analysis over ``memory`` (the ``memory_stats`` read: usedBytes,
    maxmemoryBytes, usedPctOfMax, maxmemoryPolicy, fragmentationRatio),
    ``evicted_keys`` (INFO stats), and the SCAN-budgeted ``big_keys`` sample.
    Findings: near-limit + noeviction (writes will OOM), active eviction
    (working set larger than maxmemory), fragmentation >= FRAG_HIGH_RATIO,
    ratio < FRAG_SWAP_RATIO (likely swapping), and sampled keys over
    BIG_KEY_MIN_BYTES. Every finding carries its numbers.
    """
    memory = as_obj(memory)
    used_of_max = num(memory.get("usedPctOfMax"))
    policy = opt(memory.get("maxmemoryPolicy"), 64)
    frag = num(memory.get("fragmentationRatio"))
    maxmem = num(memory.get("maxmemoryBytes"))
    findings: list[dict] = []

    if maxmem and used_of_max >= used_pct and policy == NOEVICTION:
        findings.append({
            "cause": f"Memory {used_of_max}% of maxmemory with policy=noeviction — "
            f"writes will start failing with OOM errors",
            "action": "Raise maxmemory, switch to an eviction policy (e.g. "
            "allkeys-lru) if this is a cache, or shrink the dataset (TTLs, "
            "delete the sampled big keys).",
            "evidence": {"usedPctOfMax": used_of_max, "maxmemoryPolicy": policy},
        })
    elif maxmem and used_of_max >= used_pct:
        findings.append({
            "cause": f"Memory {used_of_max}% of maxmemory — eviction pressure imminent",
            "action": "Raise maxmemory or shrink the working set before latency "
            "suffers from constant eviction.",
            "evidence": {"usedPctOfMax": used_of_max, "maxmemoryPolicy": policy},
        })
    if evicted_keys > 0:
        findings.append({
            "cause": f"Keys are being evicted ({int(evicted_keys)} evicted_keys, "
            f"policy={policy}) — the working set no longer fits in maxmemory",
            "action": "Confirm evictions are acceptable for this workload; if "
            "not, raise maxmemory or reduce key sizes/TTLs.",
            "evidence": {"evictedKeys": evicted_keys, "maxmemoryPolicy": policy},
        })
    if frag >= FRAG_HIGH_RATIO:
        findings.append({
            "cause": f"High fragmentation (ratio {frag}) — RSS well above live data",
            "action": "Enable activedefrag, or restart the instance in a "
            "maintenance window to return memory to the OS.",
            "evidence": {"fragmentationRatio": frag},
        })
    if 0 < frag < FRAG_SWAP_RATIO:
        findings.append({
            "cause": f"Fragmentation ratio {frag} < {FRAG_SWAP_RATIO} — RSS below "
            f"used memory, the OS is likely swapping redis pages",
            "action": "Check host swap usage; add RAM or lower maxmemory — a "
            "swapping instance has catastrophic latency.",
            "evidence": {"fragmentationRatio": frag},
        })

    big = [
        k for k in as_obj(big_keys).get("topKeys", [])
        if num(k.get("bytes")) >= BIG_KEY_MIN_BYTES
    ]
    if big:
        findings.append({
            "cause": f"{len(big)} sampled key(s) over "
            f"{BIG_KEY_MIN_BYTES // (1024 * 1024)} MiB dominate memory",
            "action": "Split or expire the big keys (hash/sharded lists), and "
            "check the producer that grows them.",
            "evidence": {"bigKeys": big[:10]},
        })

    if not findings:
        findings.append({
            "cause": "Healthy — within thresholds",
            "action": "No action needed.",
            "evidence": {"usedPctOfMax": used_of_max, "fragmentationRatio": frag},
        })
    return {
        "pressure": any(f["cause"] != "Healthy — within thresholds" for f in findings),
        "usedPctOfMax": used_of_max,
        "maxmemoryPolicy": policy,
        "fragmentationRatio": frag,
        "evictedKeys": evicted_keys,
        "thresholds": {
            "usedPct": used_pct,
            "fragHighRatio": FRAG_HIGH_RATIO,
            "fragSwapRatio": FRAG_SWAP_RATIO,
            "bigKeyMinBytes": BIG_KEY_MIN_BYTES,
        },
        "findings": findings[:MAX_ROWS],
        "note": (
            "Advisory read-only heuristic: pressure = near maxmemory / active "
            "eviction / fragmentation out of band / oversized sampled keys. The "
            "big-key sample is SCAN-budgeted and partial — see its coveragePct."
        ),
    }


# ── 2. redis latency / slowlog RCA ───────────────────────────────────────────
SLOW_US = 10_000  # a slowlog entry >= 10ms is genuinely slow
FORK_STALL_US = 100_000  # latest_fork_usec >= 100ms stalls the event loop
# Commands that are O(N)/blocking on big structures — the usual latency killers.
_HEAVY_COMMANDS = {
    "KEYS", "SMEMBERS", "LRANGE", "HGETALL", "SORT", "SUNION", "SINTER",
    "SDIFF", "ZRANGE", "ZRANGEBYSCORE", "MGET", "MSET", "FLUSHALL", "FLUSHDB",
    "LPOS", "GETALL", "SCARD",
}


def pull_latency_telemetry(conn: Any) -> dict:
    """[READ] Live slowlog + INFO clients/stats/persistence for the RCA."""
    info_clients = as_obj(conn.redis_info("clients"))
    info_stats = as_obj(conn.redis_info("stats"))
    info_persist = as_obj(conn.redis_info("persistence"))
    return {
        "slowlog": redis_reads.slowlog(conn).get("entries", []),
        "blockedClients": num(info_clients.get("blocked_clients")),
        "latestForkUsec": num(info_stats.get("latest_fork_usec")),
        "aofDelayedFsync": num(info_persist.get("aof_delayed_fsync")),
        "aofRewriteInProgress": num(info_persist.get("aof_rewrite_in_progress")),
        "rdbBgsaveInProgress": num(info_persist.get("rdb_bgsave_in_progress")),
        "loading": num(info_persist.get("loading")),
    }


def _digest_slowlog(entries: list[dict]) -> list[dict]:
    """Group slowlog entries by leading command word; slowest pattern first."""
    by_cmd: dict[str, dict] = {}
    for e in entries or []:
        cmd = opt(e.get("command"), 128)
        # A slowlog entry whose command Redis did not report is null now, not
        # "" — it cannot be split, and it groups under "(unknown)" rather than
        # inventing an empty command name.
        word = ((cmd or "").split(" ", 1)[0] or "(unknown)").upper()
        bucket = by_cmd.setdefault(
            word, {"command": word, "count": 0, "totalUs": 0.0, "maxUs": 0.0, "sample": cmd}
        )
        dur = num(e.get("durationUs"))
        bucket["count"] += 1
        bucket["totalUs"] += dur
        bucket["maxUs"] = max(bucket["maxUs"], dur)
    digest = []
    for b in by_cmd.values():
        digest.append({
            "command": b["command"],
            "count": b["count"],
            "avgUs": round(b["totalUs"] / b["count"], 1) if b["count"] else 0.0,
            "maxUs": b["maxUs"],
            "sample": b["sample"],
            "heavy": b["command"] in _HEAVY_COMMANDS,
        })
    digest.sort(key=lambda d: d["maxUs"], reverse=True)
    return digest


def redis_latency_rca(telemetry: dict, slow_us: float = SLOW_US) -> dict:
    """[READ] Diagnose redis latency → slowlog digest + stall causes + actions.

    Pure analysis over ``telemetry`` (from ``pull_latency_telemetry`` or
    injected): {slowlog:[{command, durationUs, ...}], blockedClients,
    latestForkUsec, aofDelayedFsync, aofRewriteInProgress,
    rdbBgsaveInProgress, loading}. Findings: heavy O(N) command patterns in the
    slowlog, blocked clients, fork stalls >= FORK_STALL_US from BGSAVE/AOF
    rewrite, delayed AOF fsyncs (slow disk), and RDB loading. Every finding
    carries its numbers.
    """
    telemetry = as_obj(telemetry)
    digest = _digest_slowlog(list(telemetry.get("slowlog") or []))
    findings: list[dict] = []

    slow_patterns = [d for d in digest if d["maxUs"] >= slow_us]
    for d in slow_patterns[:10]:
        if d["heavy"]:
            findings.append({
                "cause": f"O(N)/blocking command pattern {d['command']} in the "
                f"slowlog ({d['count']}x, max {int(d['maxUs'])}us)",
                "action": f"Replace {d['command']} with an incremental variant "
                f"(SCAN/HSCAN/SSCAN, paginated ranges) or move it off the hot path.",
                "evidence": d,
            })
        else:
            findings.append({
                "cause": f"Slow command pattern {d['command']} "
                f"({d['count']}x, max {int(d['maxUs'])}us)",
                "action": "Check the size of the structures this command touches "
                "and the concurrent load when it fires.",
                "evidence": d,
            })

    blocked = num(telemetry.get("blockedClients"))
    if blocked > 0:
        findings.append({
            "cause": f"{int(blocked)} blocked client(s) (BLPOP/BRPOP/WAIT/XREAD "
            f"blocking calls holding connections)",
            "action": "Confirm blocking consumers are intentional; use timeouts "
            "so a stuck consumer cannot hold a connection forever.",
            "evidence": {"blockedClients": blocked},
        })
    fork_us = num(telemetry.get("latestForkUsec"))
    if fork_us >= FORK_STALL_US:
        findings.append({
            "cause": f"Fork stall — latest fork took {int(fork_us)}us "
            f"(BGSAVE / AOF rewrite forking a large dataset pauses the event loop)",
            "action": "Schedule persistence off-peak, reduce dataset size per "
            "instance, or disable transparent hugepages on the host.",
            "evidence": {"latestForkUsec": fork_us},
        })
    delayed = num(telemetry.get("aofDelayedFsync"))
    if delayed > 0:
        findings.append({
            "cause": f"{int(delayed)} delayed AOF fsync(s) — the disk cannot keep "
            f"up with appendfsync",
            "action": "Move the AOF to faster storage or relax appendfsync to "
            "everysec; check for disk contention from co-located services.",
            "evidence": {"aofDelayedFsync": delayed},
        })
    if num(telemetry.get("rdbBgsaveInProgress")) or num(telemetry.get("aofRewriteInProgress")):
        findings.append({
            "cause": "A background persistence job (BGSAVE / AOF rewrite) is "
            "running right now — copy-on-write doubles memory pressure and "
            "competes for I/O",
            "action": "Re-measure latency after it finishes; if this recurs at "
            "peak, reschedule persistence.",
            "evidence": {
                "rdbBgsaveInProgress": num(telemetry.get("rdbBgsaveInProgress")),
                "aofRewriteInProgress": num(telemetry.get("aofRewriteInProgress")),
            },
        })
    if num(telemetry.get("loading")):
        findings.append({
            "cause": "Instance is loading a dataset from disk (restart/replica "
            "sync) — commands are delayed until loading completes",
            "action": "Wait for loading to finish; check replicationlink stability "
            "if this replica keeps re-syncing.",
            "evidence": {"loading": 1},
        })

    if not findings:
        findings.append({
            "cause": "Healthy — no slowlog pattern over threshold and no stall signals",
            "action": "No action needed.",
            "evidence": {"slowlogPatterns": len(digest)},
        })
    return {
        "slowlogPatterns": digest[:MAX_ROWS],
        "patternsOverThreshold": len(slow_patterns),
        "thresholds": {"slowUs": slow_us, "forkStallUs": FORK_STALL_US},
        "findings": findings[:MAX_ROWS],
        "note": (
            "Advisory read-only heuristic: slowlog grouped by leading command "
            "word; 'heavy' marks O(N)/blocking commands. Fork/AOF signals come "
            "from INFO stats/persistence."
        ),
    }


# ── 3. rabbitmq queue-backlog RCA ────────────────────────────────────────────
BACKLOG_MIN_MESSAGES = 1_000  # a queue this deep is worth a finding
UNACKED_PCT_HIGH = 50.0  # unacked >= this % of messages ⇒ ack problem
RATE_DEFICIT_FACTOR = 1.2  # publish > deliver * factor ⇒ consumers falling behind


def pull_backlog_telemetry(conn: Any, vhost: str | None = None) -> dict:
    """[READ] Live queues + node alarms for the backlog RCA."""
    return {
        "queues": rabbit_reads.list_queues(conn, vhost).get("queues", []),
        "nodes": rabbit_reads.node_health(conn).get("nodes", []),
    }


def _classify_queue(q: dict) -> dict | None:
    messages = num(q.get("messages"))
    consumers = num(q.get("consumers"))
    unacked = num(q.get("messagesUnacked"))
    publish = num(q.get("publishRate"))
    deliver = num(q.get("deliverRate"))
    if messages < BACKLOG_MIN_MESSAGES:
        return None
    if consumers == 0:
        return {
            "cause": f"{int(messages)} messages and no consumers attached",
            "action": "Start (or fix the crash loop of) the consumer service; "
            "verify the queue's binding is still what producers publish to.",
        }
    if messages and pct(unacked, messages) >= UNACKED_PCT_HIGH:
        return {
            "cause": f"{int(unacked)} of {int(messages)} messages unacknowledged "
            f"({pct(unacked, messages)}%) — consumers hold messages without acking",
            "action": "Check consumer errors/timeouts and prefetch size; a "
            "requeue loop or a stuck handler pins messages as unacked.",
        }
    if publish > deliver * RATE_DEFICIT_FACTOR:
        return {
            "cause": f"Publish rate {publish}/s outpaces deliver rate {deliver}/s "
            f"with {int(consumers)} consumer(s)",
            "action": "Scale out consumers or raise prefetch; if the backlog is "
            "expected batch traffic, confirm it drains off-peak.",
        }
    return {
        "cause": f"Deep queue ({int(messages)} messages) with consumers keeping "
        f"pace — likely a residual backlog",
        "action": "Watch the trend; if it is not draining, treat as a slow-consumer case.",
    }


def rabbitmq_queue_backlog_rca(
    queues: list[dict],
    nodes: list[dict] | None = None,
    top: int = 20,
) -> dict:
    """[READ] Rank backlogged queues and map each to a cause + action.

    Pure analysis over ``queues`` rows (from ``list_queues`` or injected):
    {name, vhost, messages, messagesReady, messagesUnacked, consumers,
    publishRate, deliverRate, state} plus optional ``nodes`` (node_health rows)
    for memory/disk watermark alarms. Per-queue causes: no consumers, unacked
    pileup, consumers slower than publishers, or residual backlog; a node
    memory/disk alarm is reported globally (it blocks *all* publishers via flow
    control). Every finding carries its numbers.
    """
    global_findings: list[dict] = []
    for n in nodes or []:
        if n.get("memAlarm"):
            global_findings.append({
                "cause": f"Node {opt(n.get('name'))} memory alarm — the memory "
                f"high watermark is reached and ALL publishers are blocked",
                "action": "Drain/purge the deepest queues or raise the "
                "watermark/RAM; publishing resumes when usage drops.",
                "evidence": {
                    "memUsedBytes": num(n.get("memUsedBytes")),
                    "memLimitBytes": num(n.get("memLimitBytes")),
                },
            })
        if n.get("diskAlarm"):
            global_findings.append({
                "cause": f"Node {opt(n.get('name'))} disk alarm — free disk under "
                f"the limit, publishers are blocked",
                "action": "Free disk space (old logs, unused vhosts) or lower "
                "disk_free_limit deliberately.",
                "evidence": {
                    "diskFreeBytes": num(n.get("diskFreeBytes")),
                    "diskFreeLimitBytes": num(n.get("diskFreeLimitBytes")),
                },
            })

    flagged = []
    for q in queues or []:
        verdict = _classify_queue(q)
        if verdict is None:
            continue
        flagged.append({
            "name": opt(q.get("name"), 128),
            "vhost": opt(q.get("vhost"), 64),
            "messages": num(q.get("messages")),
            "messagesReady": num(q.get("messagesReady")),
            "messagesUnacked": num(q.get("messagesUnacked")),
            "consumers": num(q.get("consumers")),
            "publishRate": num(q.get("publishRate")),
            "deliverRate": num(q.get("deliverRate")),
            "state": opt(q.get("state"), 32),
            **verdict,
        })
    flagged.sort(key=lambda e: e["messages"], reverse=True)

    return {
        "queuesEvaluated": len(queues or []),
        "backloggedCount": len(flagged),
        "globalFindings": global_findings,
        "queues": flagged[: max(1, int(top))],
        "thresholds": {
            "backlogMinMessages": BACKLOG_MIN_MESSAGES,
            "unackedPctHigh": UNACKED_PCT_HIGH,
            "rateDeficitFactor": RATE_DEFICIT_FACTOR,
        },
        "note": (
            "Advisory read-only heuristic: queues over backlogMinMessages are "
            "classified (no consumers / unacked pileup / rate deficit / "
            "residual); node memory/disk alarms are global — they block every "
            "publisher via flow control."
        ),
    }


# ── 4. connection-churn analysis (both platforms) ────────────────────────────
CHURN_RATE_HIGH = 1.0  # rabbitmq connection created+closed rate (per s)
CHANNELS_PER_CONN_HIGH = 20.0  # a leak smell: many channels per connection
REDIS_CONN_RATE_HIGH = 5.0  # new connections per second sustained


def pull_churn_snapshot(conn: Any) -> dict:
    """[READ] Live connection/channel snapshot for the churn analysis."""
    if conn.target.platform == REDIS:
        info = as_obj(conn.redis_info())
        clients = redis_reads.list_clients(conn)
        return {
            "platform": REDIS,
            "capturedAt": time.time(),
            "connectedClients": num(info.get("connected_clients")),
            "totalConnectionsReceived": num(info.get("total_connections_received")),
            "rejectedConnections": num(info.get("rejected_connections")),
            "uptimeSeconds": num(info.get("uptime_in_seconds")),
            "bySource": clients.get("bySource", []),
        }
    ov = rabbit_reads.broker_overview(conn)
    conns = rabbit_reads.list_connections(conn)
    return {
        "platform": conn.target.platform,
        "capturedAt": time.time(),
        "connections": num(ov.get("connections")),
        "channels": num(ov.get("channels")),
        "churn": as_obj(ov.get("connectionChurn")),
        "bySource": conns.get("byPeerHost", []),
    }


def _redis_churn(snapshot: dict, history: dict | None) -> tuple[list[dict], dict]:
    total = num(snapshot.get("totalConnectionsReceived"))
    uptime = num(snapshot.get("uptimeSeconds"))
    connected = num(snapshot.get("connectedClients"))
    rejected = num(snapshot.get("rejectedConnections"))
    findings: list[dict] = []

    rate_per_s = round(total / uptime, 2) if uptime else 0.0
    if history:
        dt = num(snapshot.get("capturedAt")) - num(history.get("capturedAt"))
        d_total = total - num(history.get("totalConnectionsReceived"))
        if dt > 0 and d_total >= 0:
            rate_per_s = round(d_total / dt, 2)
    if rate_per_s >= REDIS_CONN_RATE_HIGH:
        findings.append({
            "cause": f"High connection churn — {rate_per_s} new connections/s "
            f"against {int(connected)} steady clients (clients reconnect per "
            f"operation instead of pooling)",
            "action": "Enable client-side connection pooling / persistent "
            "connections in the busiest source apps below.",
            "evidence": {"newConnectionsPerSec": rate_per_s, "connectedClients": connected},
        })
    if rejected > 0:
        findings.append({
            "cause": f"{int(rejected)} rejected connection(s) — maxclients was hit",
            "action": "Raise maxclients (and the OS fd limit) or fix the leak/"
            "churn that exhausts client slots.",
            "evidence": {"rejectedConnections": rejected},
        })
    metrics = {
        "connectedClients": connected,
        "newConnectionsPerSec": rate_per_s,
        "rejectedConnections": rejected,
    }
    return findings, metrics


def _rabbitmq_churn(snapshot: dict, history: dict | None) -> tuple[list[dict], dict]:
    churn = as_obj(snapshot.get("churn"))
    created = num(churn.get("createdRate"))
    closed = num(churn.get("closedRate"))
    connections = num(snapshot.get("connections"))
    channels = num(snapshot.get("channels"))
    findings: list[dict] = []

    if created >= CHURN_RATE_HIGH and closed >= CHURN_RATE_HIGH:
        findings.append({
            "cause": f"Connection churn — {created}/s created and {closed}/s "
            f"closed (apps open a connection per message instead of reusing one)",
            "action": "Switch the busiest source apps below to one long-lived "
            "connection with channels per thread.",
            "evidence": {"createdRate": created, "closedRate": closed},
        })
    if connections and channels / connections >= CHANNELS_PER_CONN_HIGH:
        findings.append({
            "cause": f"{int(channels)} channels over {int(connections)} "
            f"connections ({round(channels / connections, 1)} per connection) — "
            f"a channel leak smell",
            "action": "Audit consumers for channels opened per operation and "
            "never closed.",
            "evidence": {"channels": channels, "connections": connections},
        })
    if history:
        d_conn = connections - num(history.get("connections"))
        if d_conn > 0:
            findings.append({
                "cause": f"Connection count grew by {int(d_conn)} since the prior "
                f"snapshot ({int(num(history.get('connections')))} → {int(connections)})",
                "action": "If growth is monotonic, an app is leaking connections "
                "— match the growth to a source below.",
                "evidence": {"delta": d_conn},
            })
    metrics = {
        "connections": connections,
        "channels": channels,
        "createdRate": created,
        "closedRate": closed,
    }
    return findings, metrics


def connection_churn_analysis(snapshot: dict, history: dict | None = None) -> dict:
    """[READ] Connection/channel churn → cause + action, both platforms.

    Pure analysis over a ``snapshot`` (from ``pull_churn_snapshot`` or
    injected; carries ``platform``) and an optional prior snapshot for deltas.
    redis: new-connections rate (INFO total_connections_received over uptime,
    or the delta between snapshots), rejected connections (maxclients).
    rabbitmq: overview churn_rates, channels-per-connection ratio, and
    connection growth vs history. Both report clients grouped by source so a
    finding can be pinned to an app. Every finding carries its numbers.
    """
    snapshot = as_obj(snapshot)
    platform = opt(snapshot.get("platform"), 32)
    if platform == REDIS:
        findings, metrics = _redis_churn(snapshot, history)
    else:
        findings, metrics = _rabbitmq_churn(snapshot, history)

    if not findings:
        findings.append({
            "cause": "Healthy — connection churn within thresholds",
            "action": "No action needed.",
            "evidence": metrics,
        })
    return {
        "platform": platform,
        "metrics": metrics,
        "bySource": list(snapshot.get("bySource") or [])[:50],
        "comparedToHistory": bool(history),
        "thresholds": {
            "churnRateHigh": CHURN_RATE_HIGH,
            "channelsPerConnHigh": CHANNELS_PER_CONN_HIGH,
            "redisConnRateHigh": REDIS_CONN_RATE_HIGH,
        },
        "findings": findings[:MAX_ROWS],
        "note": (
            "Advisory read-only heuristic: churn compared against fixed "
            "thresholds, or against the prior snapshot when one is passed; "
            "bySource pins churn to client hosts."
        ),
    }
