"""MCP tool-surface tests: the read wrappers + the four flagship RCA tools.

These drive the actual ``@mcp.tool()`` callables (through governance + the
``@tool_errors`` sanitiser) with a fake connection wired into each tool module.
They assert the load-bearing wiring the pure-function tests can't: that the RCA
tools pull LIVE telemetry via the right redis command / rabbitmq endpoint and
classify it, that injected telemetry skips the live pull, that a broker failure
is turned into the sanitised ``{error, hint}`` envelope, and that read tools are
risk=low. No live broker — the fakes from tests.fakes are injected.
"""

from __future__ import annotations

import pytest

from tests.fakes import rabbit_conn, redis_conn

# A redis fake fat enough for every live RCA pull (INFO sections + slowlog +
# SCAN/MEMORY USAGE + clients), sized to trip memory pressure (92% + noeviction).
_REDIS_INFO = {
    "all": {
        "redis_version": "7.2.5", "redis_mode": "standalone", "role": "master",
        "uptime_in_seconds": 3600, "connected_clients": 10, "blocked_clients": 3,
        "instantaneous_ops_per_sec": 500, "keyspace_hits": 90, "keyspace_misses": 10,
        "total_connections_received": 360_000, "rejected_connections": 4,
    },
    "stats": {"evicted_keys": 1000, "expired_keys": 5, "latest_fork_usec": 250_000,
              "total_connections_received": 360_000, "rejected_connections": 4},
    "memory": {"used_memory": 920, "used_memory_human": "920B", "maxmemory": 1000,
               "maxmemory_policy": "noeviction", "mem_fragmentation_ratio": 1.1,
               "used_memory_rss": 950, "used_memory_peak": 990},
    "clients": {"blocked_clients": 3},
    "persistence": {"aof_delayed_fsync": 7, "aof_rewrite_in_progress": 0,
                    "rdb_bgsave_in_progress": 1, "loading": 0},
    "keyspace": {"db0": "keys=100,expires=40,avg_ttl=5000"},
}

_SLOWLOG = [
    {"id": 1, "start_time": 100, "duration": 45_000, "command": ["HGETALL", b"big:hash"]},
    {"id": 2, "start_time": 101, "duration": 40_000, "command": ["HGETALL", b"other"]},
]

_CLIENTS = [
    {"id": "1", "addr": "10.0.0.5:5000", "name": "", "age": "1", "idle": "0",
     "cmd": "get", "db": "0", "flags": "N"},
]


def _fat_redis():
    return redis_conn(
        info=_REDIS_INFO, slowlog=_SLOWLOG, clients=_CLIENTS,
        memory_stats={"keys.count": 100},
        scan_pages=[(0, ["k1"])], memory_usage={"k1": 64 * 1024 * 1024}, dbsize=1,
    )


@pytest.fixture
def wire_redis(monkeypatch):
    """Wire a fat fake redis connection into every tool module's _get_connection."""
    conn = _fat_redis()
    for mod in ("analysis", "redis_reads", "overview"):
        import importlib

        m = importlib.import_module(f"mcp_server.tools.{mod}")
        monkeypatch.setattr(m, "_get_connection", lambda target=None, _c=conn: _c)
    return conn


# ── read wrappers dispatch to the right command / endpoint ───────────────────


@pytest.mark.unit
def test_redis_read_wrappers_hit_expected_commands(wire_redis):
    from mcp_server.tools import redis_reads as t

    assert t.redis_server_info()["version"] == "7.2.5"
    assert t.redis_memory_stats()["usedPctOfMax"] == 92.0
    assert t.redis_clients()["total"] == 1
    assert t.redis_slowlog()["total"] == 2
    assert t.redis_big_keys()["scannedKeys"] == 1
    cmds = {c[0] for c in wire_redis._client.calls}
    assert {"info", "client_list", "slowlog_get", "scan", "memory_usage"} <= cmds


@pytest.mark.unit
def test_rabbit_read_wrappers_hit_expected_endpoints(monkeypatch):
    from mcp_server.tools import rabbit_reads as t

    conn = rabbit_conn({
        ("GET", "/api/overview"): {"rabbitmq_version": "3.13.2",
                                   "object_totals": {"queues": 2}},
        ("GET", "/api/queues"): [{"name": "q", "vhost": "/", "messages": 5,
                                  "consumers": 1}],
        ("GET", "/api/queues/%2F/q"): {"name": "q", "vhost": "/", "messages": 5},
        ("GET", "/api/connections"): [{"name": "c", "peer_host": "10.0.0.7",
                                       "channels": 1}],
        ("GET", "/api/channels"): [{"name": "ch", "messages_unacknowledged": 1}],
        ("GET", "/api/policies"): [{"name": "p", "vhost": "/", "pattern": ".*"}],
        ("GET", "/api/nodes"): [{"name": "n", "running": True}],
    })
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)

    assert t.rabbitmq_overview()["version"] == "3.13.2"
    assert t.list_queues()["total"] == 1
    assert t.queue_detail(vhost="/", name="q")["name"] == "q"
    assert t.list_connections()["total"] == 1
    assert t.list_channels()["channels"][0]["name"] == "ch"
    assert t.list_policies()["policies"][0]["applyTo"] in ("all", None)
    assert t.node_health()["nodes"][0]["name"] == "n"
    paths = {p for _m, p, _k in conn._client.requests}
    assert "/api/overview" in paths and "/api/queues/%2F/q" in paths


@pytest.mark.unit
def test_queue_overview_wrapper_dispatches_by_platform(wire_redis):
    from mcp_server.tools import overview as t

    out = t.queue_overview()
    assert out["platform"] == "redis"
    assert out["usedPctOfMax"] == 92.0


# ── flagship RCA tools: live pull + classification ───────────────────────────


@pytest.mark.unit
def test_memory_pressure_rca_pulls_live_and_flags_noeviction_oom(wire_redis):
    from mcp_server.tools import analysis as t

    out = t.redis_memory_pressure_rca()
    assert out["pressure"] is True
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "noeviction" in causes and "OOM" in causes
    assert "evicted" in causes  # evicted_keys=1000 from INFO stats
    assert ("info", "stats") in wire_redis._client.calls  # live telemetry pulled


@pytest.mark.unit
def test_latency_rca_pulls_live_slowlog_and_stalls(wire_redis):
    from mcp_server.tools import analysis as t

    out = t.redis_latency_rca()
    top = out["slowlogPatterns"][0]
    assert top["command"] == "HGETALL" and top["count"] == 2
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "blocked client" in causes  # blocked_clients=3
    assert "Fork stall" in causes  # latest_fork_usec=250000


@pytest.mark.unit
def test_memory_rca_injected_telemetry_skips_live_pull():
    from mcp_server.tools import analysis as t

    out = t.redis_memory_pressure_rca(telemetry={
        "memory": {"usedPctOfMax": 20.0, "maxmemoryBytes": 1000,
                   "maxmemoryPolicy": "allkeys-lru", "fragmentationRatio": 1.05},
    })
    assert out["pressure"] is False
    assert out["findings"][0]["action"] == "No action needed."


@pytest.mark.unit
def test_backlog_rca_pulls_live_queues_and_classifies_no_consumers(monkeypatch):
    from mcp_server.tools import analysis as t

    conn = rabbit_conn({
        ("GET", "/api/queues"): [
            {"name": "dead", "vhost": "/", "messages": 8000, "consumers": 0},
        ],
        ("GET", "/api/nodes"): [{"name": "n", "running": True}],
    })
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)
    out = t.rabbitmq_queue_backlog_rca()
    assert out["backloggedCount"] == 1
    assert "no consumers" in out["queues"][0]["cause"]
    assert "/api/queues" in {p for _m, p, _k in conn._client.requests}


@pytest.mark.unit
def test_churn_analysis_pulls_live_redis_snapshot_and_flags_reconnects(wire_redis):
    from mcp_server.tools import analysis as t

    out = t.connection_churn_analysis()
    assert out["platform"] == "redis"
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "churn" in causes  # 360000/3600 = 100 conn/s
    assert "maxclients" in causes  # rejected_connections=4


@pytest.mark.unit
def test_rca_tool_sanitises_broker_failure_into_error_envelope(monkeypatch):
    from mcp_server.tools import analysis as t

    def boom(_target=None):
        raise ValueError("redis down: 10.0.0.1:6379 unreachable")

    monkeypatch.setattr(t, "_get_connection", boom)
    out = t.redis_memory_pressure_rca()
    assert "error" in out and "hint" in out
    assert "doctor" in out["hint"]


@pytest.mark.unit
def test_read_and_rca_tools_are_low_risk():
    from mcp_server.tools import analysis as a
    from mcp_server.tools import redis_reads as r

    for fn in (a.redis_memory_pressure_rca, a.redis_latency_rca,
               a.rabbitmq_queue_backlog_rca, a.connection_churn_analysis,
               r.redis_server_info, r.redis_big_keys):
        assert fn._risk_level == "low"
