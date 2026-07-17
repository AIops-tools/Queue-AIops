"""One-shot broker overview (read-only).

A single call an operator can lead with, platform-dispatched: for a redis
target — version, memory posture, clients, ops/sec, hit rate; for a rabbitmq
target — version, queue/message totals, connection/channel counts, rates.
Resilient — a failing sub-call degrades to a partial summary with an
``errors`` list.
"""

from __future__ import annotations

from typing import Any

from queue_aiops.ops import rabbit_reads, redis_reads
from queue_aiops.platform import REDIS


def queue_overview(conn: Any) -> dict:
    """[READ] Summary: platform + version + backlog/memory/client health."""
    if conn.target.platform == REDIS:
        return _redis_overview(conn)
    return _rabbitmq_overview(conn)


def _redis_overview(conn: Any) -> dict:
    errors: list[str] = []

    info = redis_reads.server_info(conn)
    if "error" in info:
        errors.append(f"server: {info['error']}")
        info = {}
    mem = redis_reads.memory_stats(conn)
    if "error" in mem:
        errors.append(f"memory: {mem['error']}")
        mem = {}
    keys = redis_reads.keyspace(conn)
    if "error" in keys:
        errors.append(f"keyspace: {keys['error']}")
        keys = {}

    return {
        "platform": conn.target.platform,
        "target": conn.target.name,
        "version": info.get("version"),
        "role": info.get("role"),
        "uptimeSeconds": info.get("uptimeSeconds"),
        "connectedClients": info.get("connectedClients"),
        "blockedClients": info.get("blockedClients"),
        "opsPerSec": info.get("opsPerSec"),
        "hitRatePct": info.get("hitRatePct"),
        "usedMemoryBytes": mem.get("usedBytes"),
        "maxmemoryBytes": mem.get("maxmemoryBytes"),
        "usedPctOfMax": mem.get("usedPctOfMax"),
        "maxmemoryPolicy": mem.get("maxmemoryPolicy"),
        "fragmentationRatio": mem.get("fragmentationRatio"),
        "totalKeys": keys.get("totalKeys"),
        "errors": errors,
    }


def _rabbitmq_overview(conn: Any) -> dict:
    errors: list[str] = []

    ov = rabbit_reads.broker_overview(conn)
    if "error" in ov:
        errors.append(f"overview: {ov['error']}")
        ov = {}
    nodes = rabbit_reads.node_health(conn)
    node_list = nodes.get("nodes", []) if "error" not in nodes else []
    if "error" in nodes:
        errors.append(f"nodes: {nodes['error']}")

    return {
        "platform": conn.target.platform,
        "target": conn.target.name,
        "version": ov.get("version"),
        "clusterName": ov.get("clusterName"),
        "queues": ov.get("queues"),
        "messagesReady": ov.get("messagesReady"),
        "messagesUnacked": ov.get("messagesUnacked"),
        "connections": ov.get("connections"),
        "channels": ov.get("channels"),
        "consumers": ov.get("consumers"),
        "publishRate": ov.get("publishRate"),
        "deliverRate": ov.get("deliverRate"),
        "nodesTotal": len(node_list),
        "nodesRunning": sum(1 for n in node_list if n.get("running")),
        "nodeAlarms": nodes.get("alarms") if "error" not in nodes else None,
        "errors": errors,
    }
