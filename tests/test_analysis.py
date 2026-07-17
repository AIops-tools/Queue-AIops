"""Pure-function tests for the four flagship RCAs (no I/O, injected telemetry)."""

from __future__ import annotations

import pytest

from queue_aiops.ops import analysis as ops

# ── 1. redis memory-pressure RCA ─────────────────────────────────────────────


@pytest.mark.unit
def test_memory_rca_flags_noeviction_near_limit():
    out = ops.redis_memory_pressure_rca(
        {"usedPctOfMax": 92.0, "maxmemoryBytes": 1000, "maxmemoryPolicy": "noeviction",
         "fragmentationRatio": 1.1},
    )
    assert out["pressure"] is True
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "noeviction" in causes and "OOM" in causes


@pytest.mark.unit
def test_memory_rca_flags_active_eviction_and_fragmentation():
    out = ops.redis_memory_pressure_rca(
        {"usedPctOfMax": 50.0, "maxmemoryBytes": 1000, "maxmemoryPolicy": "allkeys-lru",
         "fragmentationRatio": 2.1},
        evicted_keys=1234,
    )
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "evicted" in causes
    assert "fragmentation" in causes.lower()
    assert out["thresholds"]["fragHighRatio"] == ops.FRAG_HIGH_RATIO


@pytest.mark.unit
def test_memory_rca_flags_swap_and_big_keys():
    out = ops.redis_memory_pressure_rca(
        {"usedPctOfMax": 10.0, "maxmemoryBytes": 1000, "maxmemoryPolicy": "allkeys-lru",
         "fragmentationRatio": 0.5},
        big_keys={"topKeys": [{"key": "huge:blob", "bytes": 64 * 1024 * 1024}]},
    )
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "swapping" in causes
    assert "MiB" in causes  # big-key finding names the size class


@pytest.mark.unit
def test_memory_rca_healthy_reports_no_action():
    out = ops.redis_memory_pressure_rca(
        {"usedPctOfMax": 20.0, "maxmemoryBytes": 1000, "maxmemoryPolicy": "allkeys-lru",
         "fragmentationRatio": 1.05},
    )
    assert out["pressure"] is False
    assert out["findings"][0]["action"] == "No action needed."


# ── 2. redis latency RCA ─────────────────────────────────────────────────────

_SLOWLOG = [
    {"command": "HGETALL big:hash", "durationUs": 45_000},
    {"command": "HGETALL other:hash", "durationUs": 30_000},
    {"command": "GET k", "durationUs": 500},
]


@pytest.mark.unit
def test_latency_rca_digests_by_command_and_flags_heavy():
    out = ops.redis_latency_rca({"slowlog": _SLOWLOG})
    top = out["slowlogPatterns"][0]
    assert top["command"] == "HGETALL"
    assert top["count"] == 2
    assert top["heavy"] is True
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "HGETALL" in causes
    actions = " | ".join(f["action"] for f in out["findings"])
    assert "HSCAN" in actions or "SCAN" in actions


@pytest.mark.unit
def test_latency_rca_flags_fork_and_aof_stalls_and_blocked_clients():
    out = ops.redis_latency_rca({
        "slowlog": [],
        "blockedClients": 3,
        "latestForkUsec": 250_000,
        "aofDelayedFsync": 7,
        "rdbBgsaveInProgress": 1,
        "loading": 1,
    })
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "blocked client" in causes
    assert "Fork stall" in causes
    assert "AOF fsync" in causes
    assert "BGSAVE" in causes
    assert "loading" in causes


@pytest.mark.unit
def test_latency_rca_healthy():
    out = ops.redis_latency_rca({"slowlog": [{"command": "GET k", "durationUs": 100}]})
    assert out["patternsOverThreshold"] == 0
    assert out["findings"][0]["action"] == "No action needed."


# ── 3. rabbitmq queue-backlog RCA ────────────────────────────────────────────


@pytest.mark.unit
def test_backlog_rca_classifies_per_queue_causes():
    queues = [
        {"name": "dead", "vhost": "/", "messages": 8000, "messagesReady": 8000,
         "messagesUnacked": 0, "consumers": 0, "publishRate": 5, "deliverRate": 0},
        {"name": "stuck", "vhost": "/", "messages": 4000, "messagesReady": 1000,
         "messagesUnacked": 3000, "consumers": 2, "publishRate": 1, "deliverRate": 1},
        {"name": "slow", "vhost": "/", "messages": 2000, "messagesReady": 1900,
         "messagesUnacked": 100, "consumers": 1, "publishRate": 50, "deliverRate": 10},
        {"name": "tiny", "vhost": "/", "messages": 3, "consumers": 0},
    ]
    out = ops.rabbitmq_queue_backlog_rca(queues)
    assert out["queuesEvaluated"] == 4
    assert out["backloggedCount"] == 3  # 'tiny' is under the threshold
    by_name = {q["name"]: q for q in out["queues"]}
    assert "no consumers" in by_name["dead"]["cause"]
    assert "unacknowledged" in by_name["stuck"]["cause"]
    assert "outpaces" in by_name["slow"]["cause"]
    assert by_name["dead"]["messages"] == 8000  # deepest first
    assert out["queues"][0]["name"] == "dead"


@pytest.mark.unit
def test_backlog_rca_reports_watermark_alarms_globally():
    out = ops.rabbitmq_queue_backlog_rca(
        [],
        nodes=[{"name": "rabbit@mq1", "memAlarm": True, "memUsedBytes": 900,
                "memLimitBytes": 1000, "diskAlarm": True, "diskFreeBytes": 1,
                "diskFreeLimitBytes": 50}],
    )
    causes = " | ".join(f["cause"] for f in out["globalFindings"])
    assert "memory alarm" in causes
    assert "disk alarm" in causes
    assert "blocked" in causes


# ── 4. connection-churn analysis ─────────────────────────────────────────────


@pytest.mark.unit
def test_churn_redis_flags_reconnect_per_op_and_rejections():
    out = ops.connection_churn_analysis({
        "platform": "redis",
        "capturedAt": 1000.0,
        "connectedClients": 10,
        "totalConnectionsReceived": 360_000,
        "rejectedConnections": 12,
        "uptimeSeconds": 3600,
        "bySource": [{"source": "10.0.0.5", "clients": 9}],
    })
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "churn" in causes
    assert "maxclients" in causes
    assert out["metrics"]["newConnectionsPerSec"] == 100.0
    assert out["bySource"][0]["source"] == "10.0.0.5"


@pytest.mark.unit
def test_churn_redis_uses_history_delta_when_present():
    history = {"platform": "redis", "capturedAt": 0.0,
               "totalConnectionsReceived": 0, "uptimeSeconds": 1}
    snapshot = {"platform": "redis", "capturedAt": 100.0,
                "totalConnectionsReceived": 50, "connectedClients": 5,
                "uptimeSeconds": 101, "bySource": []}
    out = ops.connection_churn_analysis(snapshot, history=history)
    assert out["comparedToHistory"] is True
    assert out["metrics"]["newConnectionsPerSec"] == 0.5
    assert out["findings"][0]["action"] == "No action needed."


@pytest.mark.unit
def test_churn_rabbitmq_flags_churn_and_channel_leak_and_growth():
    history = {"platform": "rabbitmq", "capturedAt": 0.0, "connections": 4}
    out = ops.connection_churn_analysis({
        "platform": "rabbitmq",
        "capturedAt": 60.0,
        "connections": 10,
        "channels": 300,
        "churn": {"createdRate": 5.0, "closedRate": 4.8},
        "bySource": [{"peerHost": "10.0.0.7", "connections": 8, "channels": 290}],
    }, history=history)
    causes = " | ".join(f["cause"] for f in out["findings"])
    assert "Connection churn" in causes
    assert "leak" in causes
    assert "grew by 6" in causes


@pytest.mark.unit
def test_churn_healthy_reports_metrics():
    out = ops.connection_churn_analysis({
        "platform": "rabbitmq", "connections": 4, "channels": 8,
        "churn": {"createdRate": 0.0, "closedRate": 0.0}, "bySource": [],
    })
    assert out["findings"][0]["action"] == "No action needed."
    assert out["metrics"]["channels"] == 8
