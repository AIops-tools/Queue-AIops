"""Connection-layer tests through the REAL QueueConnection with fake clients.

Covers the typed redis command surface not exercised by the ops-level reads
(CLIENT KILL by id/addr, MEMORY USAGE, DBSIZE, SCAN paging, CONFIG GET/SET,
non-dict/non-list coercion), the rabbitmq HTTP request pipeline (empty body,
non-JSON body, transport error → teaching QueueApiError, every teaching-message
status branch, POST/PUT/DELETE verbs), the platform guard in both directions,
and the ConnectionManager session cache / disconnect / listing helpers. No live
broker: the fake clients from tests.fakes are injected under the real layer.
"""

from __future__ import annotations

import httpx
import pytest

from queue_aiops.config import AppConfig, TargetConfig
from queue_aiops.connection import (
    ConnectionManager,
    QueueApiError,
    QueueConnection,
)
from queue_aiops.platform import REDIS
from tests.fakes import (
    FakeHttp,
    FakeRedis,
    FakeResponse,
    rabbit_conn,
    rabbit_target,
    redis_conn,
    redis_target,
)

# ── redis typed command surface ──────────────────────────────────────────────


@pytest.mark.unit
def test_redis_client_kill_by_id_sends_string_id_filter():
    conn = redis_conn(kill_result=1)
    assert conn.redis_client_kill_id(77) == 1
    assert ("client_kill_filter", "77", None) in conn._client.calls


@pytest.mark.unit
def test_redis_client_kill_by_addr_sends_addr_filter():
    conn = redis_conn(kill_result=2)
    assert conn.redis_client_kill_addr("10.0.0.5:5000") == 2
    assert ("client_kill_filter", None, "10.0.0.5:5000") in conn._client.calls


@pytest.mark.unit
def test_redis_memory_usage_and_dbsize_pass_through_typed():
    conn = redis_conn(memory_usage={"k1": 4096}, dbsize=1234)
    assert conn.redis_memory_usage("k1") == 4096
    assert conn.redis_memory_usage("absent") is None
    assert conn.redis_dbsize() == 1234
    assert ("memory_usage", "k1") in conn._client.calls
    assert ("dbsize",) in conn._client.calls


@pytest.mark.unit
def test_redis_scan_returns_next_cursor_and_string_keys():
    conn = redis_conn(scan_pages=[(9, ["a", "b"])])
    nxt, keys = conn.redis_scan(cursor=0, count=500)
    assert nxt == 9
    assert keys == ["a", "b"]
    assert ("scan", 0, 500) in conn._client.calls


@pytest.mark.unit
def test_redis_config_get_and_set_round_trip_through_typed_helpers():
    conn = redis_conn(config={"maxmemory": "0"})
    assert conn.redis_config_get("maxmemory") == {"maxmemory": "0"}
    assert conn.redis_config_set("maxmemory", "1gb") is True
    assert ("config_set", "maxmemory", "1gb") in conn._client.calls


@pytest.mark.unit
def test_redis_info_and_slowlog_coerce_unexpected_client_returns():
    conn = redis_conn()
    conn._client.info = lambda section=None: "not-a-dict"
    conn._client.slowlog_get = lambda count: "not-a-list"
    assert conn.redis_info() == {}
    assert conn.redis_slowlog() == []


@pytest.mark.unit
def test_redis_call_translates_transport_error_to_teaching_message():
    conn = redis_conn()

    def boom(*_a, **_k):
        raise ConnectionError("connection refused")

    conn._client.dbsize = boom
    with pytest.raises(QueueApiError, match="Check .*host/port, password"):
        conn.redis_dbsize()


@pytest.mark.unit
def test_redis_op_on_rabbitmq_target_raises_platform_guard():
    conn = rabbit_conn()
    with pytest.raises(QueueApiError, match="needs a redis target"):
        conn.redis_ping()


# ── rabbitmq HTTP request pipeline ───────────────────────────────────────────


@pytest.mark.unit
def test_rabbit_empty_body_returns_empty_dict():
    conn = rabbit_conn({("DELETE", "/api/queues/%2F/q/contents"): FakeResponse(204, text="")})
    assert conn.delete("/api/queues/%2F/q/contents") == {}


@pytest.mark.unit
def test_rabbit_non_json_body_returns_empty_dict():
    conn = rabbit_conn({("GET", "/api/x"): FakeResponse(200, payload=None, text="<html>")})
    assert conn.get("/api/x") == {}


@pytest.mark.unit
def test_rabbit_post_put_delete_verbs_dispatch_the_right_method():
    conn = rabbit_conn({
        ("POST", "/api/p"): {"ok": 1},
        ("PUT", "/api/p"): {"ok": 2},
        ("DELETE", "/api/p"): {},
    })
    assert conn.post("/api/p") == {"ok": 1}
    assert conn.put("/api/p") == {"ok": 2}
    assert conn.delete("/api/p") == {}
    methods = [(m, p) for m, p, _ in conn._client.requests if m != "CLOSE"]
    assert methods == [("POST", "/api/p"), ("PUT", "/api/p"), ("DELETE", "/api/p")]


@pytest.mark.unit
def test_rabbit_transport_error_becomes_teaching_queue_api_error():
    conn = rabbit_conn()

    def boom(*_a, **_k):
        raise httpx.ConnectError("no route to host")

    conn._client.request = boom
    with pytest.raises(QueueApiError, match="management plugin is enabled"):
        conn.get("/api/overview")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, "management user/password"),
        (403, "management user/password"),
        (404, "Resource not found"),
        (400, "Bad request"),
        (503, "server error"),
        (418, "API error"),
    ],
)
def test_rabbit_status_codes_map_to_teaching_messages(status, needle):
    conn = rabbit_conn({("GET", "/api/overview"): FakeResponse(status, text="boom")})
    with pytest.raises(QueueApiError) as exc:
        conn.get("/api/overview")
    assert needle in str(exc.value)
    assert exc.value.status_code == status


@pytest.mark.unit
def test_rabbit_op_on_redis_target_raises_platform_guard():
    conn = redis_conn()
    with pytest.raises(QueueApiError, match="needs a rabbitmq target"):
        conn.get("/api/overview")


# ── ConnectionManager session cache + lifecycle ──────────────────────────────


@pytest.fixture
def fake_build(monkeypatch):
    """Make QueueConnection build a fake client so connect() never hits network."""
    built: list[TargetConfig] = []

    def _fake(target: TargetConfig):
        built.append(target)
        return FakeRedis() if target.platform == REDIS else FakeHttp()

    monkeypatch.setattr(QueueConnection, "_build_client", staticmethod(_fake))
    return built


def _cfg() -> AppConfig:
    return AppConfig(targets=(redis_target("cache1"), rabbit_target("broker1")))


@pytest.mark.unit
def test_manager_caches_one_connection_per_target(fake_build):
    mgr = ConnectionManager(_cfg())
    first = mgr.connect("cache1")
    again = mgr.connect("cache1")
    assert first is again  # cached — the client was built exactly once
    assert [t.name for t in fake_build] == ["cache1"]
    assert mgr.list_connected() == ["cache1"]


@pytest.mark.unit
def test_manager_default_target_is_first_and_lists_all_targets(fake_build):
    mgr = ConnectionManager(_cfg())
    default = mgr.connect()
    assert default.target.name == "cache1"
    assert mgr.list_targets() == ["cache1", "broker1"]


@pytest.mark.unit
def test_manager_disconnect_closes_and_evicts(fake_build):
    mgr = ConnectionManager(_cfg())
    conn = mgr.connect("cache1")
    mgr.disconnect("cache1")
    assert mgr.list_connected() == []
    assert ("close",) in conn._client.calls
    mgr.disconnect("cache1")  # idempotent: no-op on an absent target


@pytest.mark.unit
def test_manager_disconnect_all_closes_every_session(fake_build):
    mgr = ConnectionManager(_cfg())
    mgr.connect("cache1")
    mgr.connect("broker1")
    mgr.disconnect_all()
    assert mgr.list_connected() == []


@pytest.mark.unit
def test_manager_from_config_uses_injected_config(fake_build):
    mgr = ConnectionManager.from_config(_cfg())
    assert mgr.list_targets() == ["cache1", "broker1"]


@pytest.mark.unit
def test_manager_unknown_target_teaches_available(fake_build):
    mgr = ConnectionManager(_cfg())
    with pytest.raises(KeyError, match="Available: cache1, broker1"):
        mgr.connect("ghost")
