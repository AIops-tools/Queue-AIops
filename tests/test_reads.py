"""Read ops through the REAL connection layer with fake clients.

Covers: redis reads (INFO-derived, slowlog, clients, config, keyspace, and the
SCAN-budgeted big-key sample — including the hard budget), rabbitmq reads
(overview/queues/connections/channels/policies/nodes), the platform guard
(redis op on a rabbitmq target fails with a teaching error), connection error
translation, and the one-shot overview for both platforms.
"""

from __future__ import annotations

import pytest

from queue_aiops.connection import QueueApiError
from queue_aiops.ops import overview as overview_ops
from queue_aiops.ops import rabbit_reads, redis_reads
from tests.fakes import FakeResponse, rabbit_conn, redis_conn

# ── redis reads ──────────────────────────────────────────────────────────────

_INFO_ALL = {
    "redis_version": "7.2.5", "redis_mode": "standalone", "uptime_in_seconds": 86400,
    "role": "master", "connected_clients": 42, "blocked_clients": 2,
    "instantaneous_ops_per_sec": 1200, "total_commands_processed": 9_000_000,
    "keyspace_hits": 900, "keyspace_misses": 100,
}


@pytest.mark.unit
def test_server_info_reads_info_fields():
    conn = redis_conn(info={"all": _INFO_ALL})
    out = redis_reads.server_info(conn)
    assert out["version"] == "7.2.5"
    assert out["connectedClients"] == 42
    assert out["hitRatePct"] == 90.0


@pytest.mark.unit
def test_memory_stats_computes_used_pct():
    conn = redis_conn(
        info={"memory": {
            "used_memory": 800, "used_memory_human": "800B", "maxmemory": 1000,
            "maxmemory_policy": "allkeys-lru", "mem_fragmentation_ratio": 1.2,
            "used_memory_rss": 960, "used_memory_peak": 990,
        }},
        memory_stats={"keys.count": 10, "overhead.total": 100, "dataset.bytes": 700},
    )
    out = redis_reads.memory_stats(conn)
    assert out["usedPctOfMax"] == 80.0
    assert out["maxmemoryPolicy"] == "allkeys-lru"
    assert out["keysCount"] == 10


@pytest.mark.unit
def test_list_clients_groups_by_source_address():
    conn = redis_conn(clients=[
        {"id": "1", "addr": "10.0.0.5:5000", "name": "", "age": "10", "idle": "0",
         "cmd": "get", "db": "0", "flags": "N"},
        {"id": "2", "addr": "10.0.0.5:5001", "name": "worker", "age": "20", "idle": "5",
         "cmd": "setex", "db": "0", "flags": "N"},
        {"id": "3", "addr": "10.0.0.9:6000", "name": "", "age": "5", "idle": "1",
         "cmd": "blpop", "db": "0", "flags": "b"},
    ])
    out = redis_reads.list_clients(conn)
    assert out["total"] == 3
    assert out["bySource"][0] == {"source": "10.0.0.5", "clients": 2}


@pytest.mark.unit
def test_slowlog_sorts_slowest_first_and_folds_command_bytes():
    conn = redis_conn(slowlog=[
        {"id": 1, "start_time": 100, "duration": 5_000, "command": b"GET k1"},
        {"id": 2, "start_time": 101, "duration": 50_000,
         "command": ["HGETALL", b"big:hash"]},
    ])
    out = redis_reads.slowlog(conn)
    assert out["returned"] == 2
    assert out["truncated"] is False
    assert out["entries"][0]["durationUs"] == 50_000
    assert "HGETALL" in out["entries"][0]["command"]
    assert out["entries"][1]["command"] == "GET k1"


@pytest.mark.unit
def test_config_get_bounds_and_sorts():
    conn = redis_conn(config={"maxmemory": "0", "appendonly": "no"})
    out = redis_reads.config_get(conn, "*")
    assert out["total"] == 2
    assert list(out["parameters"]) == ["appendonly", "maxmemory"]


@pytest.mark.unit
def test_keyspace_parses_raw_cells():
    conn = redis_conn(info={"keyspace": {"db0": "keys=100,expires=40,avg_ttl=5000"}})
    out = redis_reads.keyspace(conn)
    assert out["totalKeys"] == 100
    assert out["databases"][0]["expiresPct"] == 40.0


@pytest.mark.unit
def test_big_key_sample_scans_and_sizes_topmost():
    conn = redis_conn(
        scan_pages=[(1, ["k1", "k2"]), (0, ["k3"])],
        memory_usage={"k1": 100, "k2": 50_000_000, "k3": 300},
        dbsize=3,
    )
    out = redis_reads.big_key_sample(conn)
    assert out["scannedKeys"] == 3
    assert out["coveragePct"] == 100.0
    assert out["topKeys"][0] == {"key": "k2", "bytes": 50_000_000.0}


@pytest.mark.unit
def test_big_key_sample_enforces_hard_scan_budget():
    """A cursor that never returns 0 must stop at the budget — never KEYS *,
    never an unbounded walk."""
    pages = [(i + 1, [f"k{i}-{j}" for j in range(500)]) for i in range(100)]
    conn = redis_conn(scan_pages=pages, memory_usage={}, dbsize=1_000_000)
    out = redis_reads.big_key_sample(conn)
    assert out["scannedKeys"] == redis_reads.SCAN_BUDGET_KEYS
    scans = [c for c in conn._client.calls if c[0] == "scan"]
    assert len(scans) == redis_reads.SCAN_BUDGET_KEYS // redis_reads.SCAN_PAGE
    sizes = [c for c in conn._client.calls if c[0] == "memory_usage"]
    assert len(sizes) <= redis_reads.MEMORY_SAMPLE_MAX


@pytest.mark.unit
def test_redis_op_on_rabbitmq_target_teaches_platform_mismatch():
    conn = rabbit_conn()
    out = redis_reads.server_info(conn)
    assert "redis target" in out["error"]


@pytest.mark.unit
def test_redis_transport_error_translated_to_teaching_message():
    conn = redis_conn()

    def boom():
        raise ConnectionError("connection refused")

    conn._client.ping = boom
    with pytest.raises(QueueApiError, match="Check host/port, password"):
        conn.redis_ping()


# ── rabbitmq reads ───────────────────────────────────────────────────────────

_OVERVIEW = {
    "rabbitmq_version": "3.13.2", "cluster_name": "rabbit@mq1", "node": "rabbit@mq1",
    "queue_totals": {"messages_ready": 900, "messages_unacknowledged": 100,
                     "messages": 1000},
    "object_totals": {"queues": 12, "connections": 4, "channels": 9, "consumers": 6},
    "message_stats": {"publish_details": {"rate": 55.0},
                      "deliver_get_details": {"rate": 12.5},
                      "ack_details": {"rate": 12.0}},
    "churn_rates": {"connection_created_details": {"rate": 0.2},
                    "connection_closed_details": {"rate": 0.1},
                    "channel_created_details": {"rate": 0.4},
                    "channel_closed_details": {"rate": 0.3}},
}


@pytest.mark.unit
def test_broker_overview_reads_totals_and_rates():
    conn = rabbit_conn({("GET", "/api/overview"): _OVERVIEW})
    out = rabbit_reads.broker_overview(conn)
    assert out["version"] == "3.13.2"
    assert out["messagesReady"] == 900
    assert out["publishRate"] == 55.0
    assert out["connectionChurn"]["createdRate"] == 0.2


@pytest.mark.unit
def test_list_queues_sorts_deepest_backlog_first():
    conn = rabbit_conn({("GET", "/api/queues"): [
        {"name": "small", "vhost": "/", "messages": 3, "consumers": 1},
        {"name": "deep", "vhost": "/", "messages": 50_000, "consumers": 0,
         "message_stats": {"publish_details": {"rate": 10.0}}},
    ]})
    out = rabbit_reads.list_queues(conn)
    assert out["total"] == 2
    assert out["queues"][0]["name"] == "deep"
    assert out["queues"][0]["publishRate"] == 10.0


@pytest.mark.unit
def test_queue_detail_uses_encoded_vhost_path():
    conn = rabbit_conn({("GET", "/api/queues/%2F/orders"): {
        "name": "orders", "vhost": "/", "messages": 5, "durable": True,
        "auto_delete": False, "arguments": {"x-queue-type": "quorum"},
        "consumers": 2, "node": "rabbit@mq1",
    }})
    out = rabbit_reads.queue_detail(conn, "/", "orders")
    assert out["durable"] is True
    assert out["arguments"] == {"x-queue-type": "quorum"}


@pytest.mark.unit
def test_list_connections_groups_by_peer_host():
    conn = rabbit_conn({("GET", "/api/connections"): [
        {"name": "c1", "peer_host": "10.0.0.7", "user": "app", "state": "running",
         "channels": 3, "client_properties": {"product": "pika"}},
        {"name": "c2", "peer_host": "10.0.0.7", "user": "app", "state": "running",
         "channels": 1},
        {"name": "c3", "peer_host": "10.0.0.8", "user": "app", "state": "running",
         "channels": 2},
    ]})
    out = rabbit_reads.list_connections(conn)
    assert out["total"] == 3
    assert out["byPeerHost"][0]["peerHost"] == "10.0.0.7"
    assert out["byPeerHost"][0]["channels"] == 4


@pytest.mark.unit
def test_list_channels_sorts_by_unacked():
    conn = rabbit_conn({("GET", "/api/channels"): [
        {"name": "ch1", "messages_unacknowledged": 1, "prefetch_count": 10},
        {"name": "ch2", "messages_unacknowledged": 400, "prefetch_count": 500,
         "connection_details": {"name": "c2"}},
    ]})
    out = rabbit_reads.list_channels(conn)
    assert out["channels"][0]["name"] == "ch2"
    assert out["channels"][0]["connection"] == "c2"


@pytest.mark.unit
def test_list_policies_reads_definitions():
    conn = rabbit_conn({("GET", "/api/policies"): [
        {"name": "ha", "vhost": "/", "pattern": "^ha\\.", "apply-to": "queues",
         "priority": 1, "definition": {"max-length": 10000}},
    ]})
    out = rabbit_reads.list_policies(conn)
    assert out["policies"][0]["applyTo"] == "queues"
    assert out["policies"][0]["definition"] == {"max-length": 10000}


@pytest.mark.unit
def test_node_health_flags_alarms():
    conn = rabbit_conn({("GET", "/api/nodes"): [
        {"name": "rabbit@mq1", "running": True, "mem_used": 900, "mem_limit": 1000,
         "mem_alarm": True, "disk_free": 10_000, "disk_free_limit": 50_000,
         "disk_free_alarm": True, "fd_used": 100, "fd_total": 1024},
    ]})
    out = rabbit_reads.node_health(conn)
    assert out["alarms"] == 1
    assert out["nodes"][0]["memAlarm"] is True


@pytest.mark.unit
def test_rabbitmq_401_translates_to_teaching_message():
    conn = rabbit_conn({("GET", "/api/overview"): FakeResponse(401, text="denied")})
    out = rabbit_reads.broker_overview(conn)
    assert "management user/password" in out["error"]


# ── one-shot overview (both platforms) ───────────────────────────────────────


@pytest.mark.unit
def test_queue_overview_redis_shape():
    conn = redis_conn(info={
        "all": _INFO_ALL,
        "memory": {"used_memory": 500, "maxmemory": 1000,
                   "maxmemory_policy": "noeviction", "mem_fragmentation_ratio": 1.1},
        "keyspace": {"db0": "keys=7,expires=1,avg_ttl=0"},
    })
    out = overview_ops.queue_overview(conn)
    assert out["platform"] == "redis"
    assert out["usedPctOfMax"] == 50.0
    assert out["totalKeys"] == 7
    assert out["errors"] == []


@pytest.mark.unit
def test_queue_overview_rabbitmq_shape_degrades_partially():
    conn = rabbit_conn({("GET", "/api/overview"): _OVERVIEW})  # /api/nodes → 404
    out = overview_ops.queue_overview(conn)
    assert out["platform"] == "rabbitmq"
    assert out["queues"] == 12
    assert any("nodes:" in e for e in out["errors"])
