"""CLI read-path tests: every ``redis``/``rabbitmq``/``analyze`` read command
and the top-level ``overview``, driven through the real Typer app with a fake
connection wired into each CLI module's ``get_connection``.

They assert the load-bearing wiring: the command exits 0 and the RIGHT redis
command / rabbitmq management endpoint (and option params — slowlog --count,
bigkeys --top, queues --vhost) actually reached the injected client. The
``cli_errors`` teaching-translation path (broker error → one red line + exit 1)
is covered too. No live broker — the fakes from tests.fakes are injected.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from queue_aiops.connection import QueueApiError
from tests.fakes import rabbit_conn, redis_conn

runner = CliRunner()

_INFO = {
    "all": {
        "redis_version": "7.2.5", "redis_mode": "standalone", "role": "master",
        "uptime_in_seconds": 3600, "connected_clients": 10, "blocked_clients": 3,
        "instantaneous_ops_per_sec": 500, "keyspace_hits": 90, "keyspace_misses": 10,
        "total_connections_received": 360_000, "rejected_connections": 4,
    },
    "stats": {"evicted_keys": 1000, "latest_fork_usec": 250_000,
              "total_connections_received": 360_000, "rejected_connections": 4},
    "memory": {"used_memory": 920, "maxmemory": 1000, "maxmemory_policy": "noeviction",
               "mem_fragmentation_ratio": 1.1, "used_memory_rss": 950,
               "used_memory_peak": 990},
    "clients": {"blocked_clients": 3},
    "persistence": {"aof_delayed_fsync": 7, "rdb_bgsave_in_progress": 1, "loading": 0},
    "keyspace": {"db0": "keys=100,expires=40,avg_ttl=5000"},
}


def _redis():
    return redis_conn(
        info=_INFO, slowlog=[{"id": 1, "start_time": 1, "duration": 45_000,
                              "command": ["HGETALL", b"big"]}],
        clients=[{"id": "1", "addr": "10.0.0.5:5000", "name": "", "age": "1",
                  "idle": "0", "cmd": "get", "db": "0", "flags": "N"}],
        config={"maxmemory": "0"}, memory_stats={"keys.count": 100},
        scan_pages=[(0, ["k1"])], memory_usage={"k1": 64 * 1024 * 1024}, dbsize=1,
    )


def _rabbit():
    return rabbit_conn({
        ("GET", "/api/overview"): {"rabbitmq_version": "3.13.2",
                                   "object_totals": {"queues": 1}},
        ("GET", "/api/queues"): [{"name": "dead", "vhost": "/", "messages": 8000,
                                  "consumers": 0}],
        ("GET", "/api/queues/staging"): [{"name": "q2", "vhost": "staging",
                                          "messages": 1, "consumers": 1}],
        ("GET", "/api/queues/%2F/orders"): {"name": "orders", "vhost": "/",
                                            "messages": 5, "durable": True},
        ("GET", "/api/connections"): [{"name": "c", "peer_host": "10.0.0.7",
                                       "channels": 1}],
        ("GET", "/api/channels"): [{"name": "ch", "messages_unacknowledged": 1}],
        ("GET", "/api/policies"): [{"name": "p", "vhost": "/", "pattern": ".*"}],
        ("GET", "/api/nodes"): [{"name": "n", "running": True, "mem_used": 1,
                                 "mem_limit": 10}],
    })


def _wire(monkeypatch, module: str, conn):
    import importlib

    m = importlib.import_module(f"queue_aiops.cli.{module}")
    monkeypatch.setattr(m, "get_connection", lambda target=None, _c=conn: (_c, None))
    return conn


def _calls(conn):
    return conn._client.calls


def _paths(conn):
    return [p for _m, p, _k in conn._client.requests]


# ── redis read commands ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argv", "expect_call"),
    [
        (["redis", "info"], "info"),
        (["redis", "memory"], "memory_stats"),
        (["redis", "clients"], "client_list"),
        (["redis", "config-get", "maxmemory"], "config_get"),
        (["redis", "keyspace"], "info"),
    ],
)
def test_redis_read_commands_hit_expected_client_call(monkeypatch, argv, expect_call):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "redis_cmds", _redis())
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert any(c[0] == expect_call for c in _calls(conn))


@pytest.mark.unit
def test_redis_slowlog_forwards_count_option(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "redis_cmds", _redis())
    result = runner.invoke(app, ["redis", "slowlog", "--count", "7"])
    assert result.exit_code == 0, result.output
    # count + 1 is deliberate: the extra entry is how truncation is measured.
    assert ("slowlog_get", 8) in _calls(conn)


@pytest.mark.unit
def test_redis_bigkeys_scans_within_budget_never_keys_star(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "redis_cmds", _redis())
    result = runner.invoke(app, ["redis", "bigkeys", "--top", "5"])
    assert result.exit_code == 0, result.output
    assert any(c[0] == "scan" for c in _calls(conn))
    assert all(c[0] != "keys" for c in _calls(conn))


@pytest.mark.unit
def test_cli_errors_translates_broker_failure_to_one_red_line(monkeypatch):
    from queue_aiops.cli import app

    def boom(target=None):
        raise QueueApiError("redis unreachable at cache:6379")

    import queue_aiops.cli.redis_cmds as m

    monkeypatch.setattr(m, "get_connection", boom)
    result = runner.invoke(app, ["redis", "info"])
    assert result.exit_code == 1
    assert "Error:" in result.output and "unreachable" in result.output


# ── rabbitmq read commands ───────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argv", "expect_path"),
    [
        (["rabbitmq", "overview"], "/api/overview"),
        (["rabbitmq", "queues"], "/api/queues"),
        (["rabbitmq", "connections"], "/api/connections"),
        (["rabbitmq", "channels"], "/api/channels"),
        (["rabbitmq", "policies"], "/api/policies"),
        (["rabbitmq", "nodes"], "/api/nodes"),
        (["rabbitmq", "queue", "orders"], "/api/queues/%2F/orders"),
    ],
)
def test_rabbit_read_commands_hit_expected_endpoint(monkeypatch, argv, expect_path):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "rabbit_cmds", _rabbit())
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert expect_path in _paths(conn)


@pytest.mark.unit
def test_rabbit_queues_forwards_vhost_option_to_scoped_endpoint(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "rabbit_cmds", _rabbit())
    result = runner.invoke(app, ["rabbitmq", "queues", "--vhost", "staging"])
    assert result.exit_code == 0, result.output
    assert "/api/queues/staging" in _paths(conn)


# ── analyze (RCA) commands ───────────────────────────────────────────────────


@pytest.mark.unit
def test_analyze_memory_pulls_live_telemetry(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "analyze", _redis())
    result = runner.invoke(app, ["analyze", "memory"])
    assert result.exit_code == 0, result.output
    assert ("info", "stats") in _calls(conn)  # evicted-keys pull
    assert any(c[0] == "scan" for c in _calls(conn))  # big-key sample


@pytest.mark.unit
def test_analyze_latency_pulls_slowlog_and_persistence(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "analyze", _redis())
    result = runner.invoke(app, ["analyze", "latency", "--slow-us", "5000"])
    assert result.exit_code == 0, result.output
    assert any(c[0] == "slowlog_get" for c in _calls(conn))
    assert ("info", "persistence") in _calls(conn)


@pytest.mark.unit
def test_analyze_backlog_pulls_queues_and_nodes(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "analyze", _rabbit())
    result = runner.invoke(app, ["analyze", "backlog", "--top", "5"])
    assert result.exit_code == 0, result.output
    assert "/api/queues" in _paths(conn)
    assert "/api/nodes" in _paths(conn)


@pytest.mark.unit
def test_analyze_churn_pulls_redis_snapshot(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "analyze", _redis())
    result = runner.invoke(app, ["analyze", "churn"])
    assert result.exit_code == 0, result.output
    assert any(c[0] == "info" for c in _calls(conn))
    assert ("client_list",) in _calls(conn)


# ── top-level overview ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_overview_redis_reports_platform_and_memory(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "overview", _redis())
    result = runner.invoke(app, ["overview"])
    assert result.exit_code == 0, result.output
    assert "redis" in result.output
    assert any(c[0] == "info" for c in _calls(conn))


@pytest.mark.unit
def test_overview_rabbitmq_reports_platform(monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, "overview", _rabbit())
    result = runner.invoke(app, ["overview"])
    assert result.exit_code == 0, result.output
    assert "/api/overview" in _paths(conn)
