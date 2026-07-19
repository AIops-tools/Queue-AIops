"""rabbitmq reads — overview, queues, connections, channels, policies, nodes.

The day-to-day "is this broker healthy?" surface for rabbitmq targets, all via
the management HTTP API. Every call is resilient — a transport/parse failure
surfaces as ``{"error": ...}`` instead of raising — and all broker text is
sanitised via ``s`` / the platform normaliser.
"""

from __future__ import annotations

from typing import Any

from queue_aiops.ops._util import as_int, as_obj, num, opt, pick, rate, s

MAX_ROWS = 200


def broker_overview(conn: Any) -> dict:
    """[READ] Broker identity + totals from /api/overview."""
    try:
        obj = as_obj(conn.platform.normalise(conn.get(conn.platform.path("overview"))))
        totals = as_obj(obj.get("queue_totals"))
        objects = as_obj(obj.get("object_totals"))
        stats = as_obj(obj.get("message_stats"))
        churn = as_obj(obj.get("churn_rates"))
        return {
            "platform": conn.target.platform,
            "version": opt(pick(obj, "rabbitmq_version", "product_version")),
            "clusterName": opt(obj.get("cluster_name")),
            "node": opt(obj.get("node")),
            "messagesReady": as_int(totals.get("messages_ready")),
            "messagesUnacked": as_int(totals.get("messages_unacknowledged")),
            "messagesTotal": as_int(totals.get("messages")),
            "queues": num(objects.get("queues")),
            "connections": num(objects.get("connections")),
            "channels": num(objects.get("channels")),
            "consumers": num(objects.get("consumers")),
            "publishRate": rate(stats.get("publish_details")),
            "deliverRate": rate(stats.get("deliver_get_details")),
            "ackRate": rate(stats.get("ack_details")),
            "connectionChurn": {
                "createdRate": rate(churn.get("connection_created_details")),
                "closedRate": rate(churn.get("connection_closed_details")),
                "channelCreatedRate": rate(churn.get("channel_created_details")),
                "channelClosedRate": rate(churn.get("channel_closed_details")),
            },
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def _queue_row(q: dict) -> dict:
    stats = as_obj(q.get("message_stats"))
    return {
        "name": opt(q.get("name"), 128),
        "vhost": opt(q.get("vhost"), 64),
        "state": opt(q.get("state"), 32),
        "durable": bool(q.get("durable")),
        "messages": as_int(q.get("messages")),
        "messagesReady": as_int(q.get("messages_ready")),
        "messagesUnacked": as_int(q.get("messages_unacknowledged")),
        "consumers": num(q.get("consumers")),
        "memoryBytes": as_int(q.get("memory")),
        "publishRate": rate(stats.get("publish_details")),
        "deliverRate": rate(stats.get("deliver_get_details")),
        "ackRate": rate(stats.get("ack_details")),
    }


def list_queues(conn: Any, vhost: str | None = None) -> dict:
    """[READ] Queues (optionally one vhost), deepest backlog first."""
    try:
        path = (
            conn.platform.path("queues_vhost", vhost=vhost)
            if vhost
            else conn.platform.path("queues")
        )
        rows = [_queue_row(q) for q in conn.platform.rows(conn.get(path))]
        rows.sort(key=lambda r: r["messages"], reverse=True)
        return {
            "queues": rows[:MAX_ROWS],
            "returned": min(len(rows), MAX_ROWS),
            "limit": MAX_ROWS,
            "truncated": len(rows) > MAX_ROWS,
            "total": len(rows),
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def queue_detail(conn: Any, vhost: str, name: str) -> dict:
    """[READ] One queue's full detail (rates, consumers, memory, args)."""
    try:
        obj = as_obj(
            conn.platform.normalise(
                conn.get(conn.platform.path("queue", vhost=vhost, name=name))
            )
        )
        row = _queue_row(obj)
        row.update(
            {
                "autoDelete": bool(obj.get("auto_delete")),
                "exclusive": bool(obj.get("exclusive")),
                "arguments": as_obj(obj.get("arguments")),
                "node": opt(obj.get("node"), 64),
                "idleSince": opt(obj.get("idle_since"), 64),
                "consumerUtilisation": num(
                    pick(obj, "consumer_capacity", "consumer_utilisation", default=0)
                ),
            }
        )
        return row
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def list_connections(conn: Any) -> dict:
    """[READ] Client connections grouped by peer host, busiest first."""
    try:
        rows = [
            {
                "name": opt(c.get("name"), 128),
                "peerHost": opt(pick(c, "peer_host", default="(unknown)"), 64),
                "user": opt(c.get("user"), 64),
                "state": opt(c.get("state"), 32),
                "channels": num(c.get("channels")),
                "connectedAt": num(c.get("connected_at")),
                "clientProduct": s(as_obj(c.get("client_properties")).get("product"), 64),
            }
            for c in conn.platform.rows(conn.get(conn.platform.path("connections")))
        ]
        by_host: dict[str, dict] = {}
        for r in rows:
            bucket = by_host.setdefault(
                r["peerHost"], {"peerHost": r["peerHost"], "connections": 0, "channels": 0.0}
            )
            bucket["connections"] += 1
            bucket["channels"] += r["channels"]
        sources = sorted(by_host.values(), key=lambda e: e["connections"], reverse=True)
        return {
            "total": len(rows),
            "byPeerHost": sources[:50],
            "connections": rows[:MAX_ROWS],
            "returned": min(len(rows), MAX_ROWS),
            "limit": MAX_ROWS,
            "truncated": len(rows) > MAX_ROWS,
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def list_channels(conn: Any) -> dict:
    """[READ] Channels with unacked/prefetch pressure, most unacked first."""
    try:
        rows = [
            {
                "name": opt(c.get("name"), 128),
                "connection": s(as_obj(c.get("connection_details")).get("name"), 128),
                "user": opt(c.get("user"), 64),
                "state": opt(c.get("state"), 32),
                "unacked": num(c.get("messages_unacknowledged")),
                "prefetch": num(c.get("prefetch_count")),
                "consumers": num(c.get("consumer_count")),
            }
            for c in conn.platform.rows(conn.get(conn.platform.path("channels")))
        ]
        rows.sort(key=lambda r: r["unacked"], reverse=True)
        return {
            "channels": rows[:MAX_ROWS],
            "returned": min(len(rows), MAX_ROWS),
            "limit": MAX_ROWS,
            "truncated": len(rows) > MAX_ROWS,
            "total": len(rows),
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def list_policies(conn: Any, vhost: str | None = None) -> dict:
    """[READ] Policies (optionally one vhost)."""
    try:
        path = (
            conn.platform.path("policies_vhost", vhost=vhost)
            if vhost
            else conn.platform.path("policies")
        )
        rows = [
            {
                "name": opt(p.get("name"), 128),
                "vhost": opt(p.get("vhost"), 64),
                "pattern": opt(p.get("pattern"), 128),
                "applyTo": opt(pick(p, "apply-to", "apply_to", default="all"), 32),
                "priority": num(p.get("priority")),
                "definition": as_obj(p.get("definition")),
            }
            for p in conn.platform.rows(conn.get(path))
        ]
        return {
            "policies": rows[:MAX_ROWS],
            "returned": min(len(rows), MAX_ROWS),
            "limit": MAX_ROWS,
            "truncated": len(rows) > MAX_ROWS,
            "total": len(rows),
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}


def node_health(conn: Any) -> dict:
    """[READ] Node memory/disk/fd posture + alarms, most loaded first."""
    try:
        rows = []
        for n in conn.platform.rows(conn.get(conn.platform.path("nodes"))):
            mem_used = num(n.get("mem_used"))
            mem_limit = num(n.get("mem_limit"))
            rows.append(
                {
                    "name": opt(n.get("name"), 64),
                    "running": bool(n.get("running", True)),
                    "memUsedBytes": mem_used,
                    "memLimitBytes": mem_limit,
                    "memAlarm": bool(n.get("mem_alarm")),
                    "diskFreeBytes": as_int(n.get("disk_free")),
                    "diskFreeLimitBytes": as_int(n.get("disk_free_limit")),
                    "diskAlarm": bool(n.get("disk_free_alarm")),
                    "fdUsed": num(n.get("fd_used")),
                    "fdTotal": num(n.get("fd_total")),
                    "socketsUsed": num(n.get("sockets_used")),
                    "socketsTotal": num(n.get("sockets_total")),
                }
            )
        rows.sort(key=lambda r: r["memUsedBytes"], reverse=True)
        return {
            "total": len(rows),
            "alarms": sum(1 for r in rows if r["memAlarm"] or r["diskAlarm"]),
            "nodes": rows,
        }
    except Exception as exc:  # noqa: BLE001 — report as partial
        return {"error": s(exc, 200)}
