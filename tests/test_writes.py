"""Unit tests for the governed broker writes (ops + MCP tools).

Proves: every reversible write captures REAL prior state BEFORE mutating
(config_set via CONFIG GET, delete_queue via the queue's definition, policies
via the prior policy), the undo descriptors invert correctly AND replay
against the target tool signature (delete_queue → declare_queue re-issues the
captured PUT), risk tiers are correct (purge/delete queue = high, the rest =
medium), dry_run previews never mutate, hostile names are percent-encoded on
the write path, and input validation rejects bad parameters. No real broker —
the fakes from tests.fakes are injected under the real connection layer.
"""

from __future__ import annotations

import json

import pytest

from queue_aiops.ops import writes as ops
from tests.fakes import FakeResponse, rabbit_conn, redis_conn

# ── redis config_set prior-value capture ─────────────────────────────────────


@pytest.mark.unit
def test_config_set_captures_prior_value_before_mutating():
    conn = redis_conn(config={"maxmemory-policy": "noeviction"})
    out = ops.config_set(conn, "maxmemory-policy", "allkeys-lru")
    assert out["priorState"] == {"value": "noeviction"}
    calls = conn._client.calls
    assert calls.index(("config_get", "maxmemory-policy")) < calls.index(
        ("config_set", "maxmemory-policy", "allkeys-lru")
    )


@pytest.mark.unit
def test_config_set_rejects_invalid_parameter_names():
    conn = redis_conn(config={})
    with pytest.raises(ValueError, match="Invalid config parameter"):
        ops.config_set(conn, "maxmemory; FLUSHALL", "x")
    assert conn._client.calls == []  # nothing reached the wire


@pytest.mark.unit
def test_config_set_unknown_parameter_teaches():
    conn = redis_conn(config={})
    with pytest.raises(KeyError, match="does not exist"):
        ops.config_set(conn, "not-a-param", "x")


# ── redis kill_client priorState ─────────────────────────────────────────────


@pytest.mark.unit
def test_kill_client_captures_client_row_as_prior_state():
    conn = redis_conn(clients=[
        {"id": "77", "addr": "10.0.0.5:5000", "name": "worker", "cmd": "blpop",
         "age": "120"},
    ])
    out = ops.kill_client(conn, client_id=77)
    assert out["killed"] == 1
    assert out["priorState"]["client"]["addr"] == "10.0.0.5:5000"
    assert out["priorState"]["client"]["lastCommand"] == "blpop"
    assert ("client_kill_filter", "77", None) in conn._client.calls


@pytest.mark.unit
def test_kill_client_requires_id_or_addr():
    conn = redis_conn()
    with pytest.raises(ValueError, match="client_id .*or addr"):
        ops.kill_client(conn)


# ── rabbitmq purge/delete prior-state capture ────────────────────────────────

_Q = {"name": "orders", "vhost": "/", "messages": 1234, "durable": True,
      "auto_delete": False, "arguments": {"x-queue-type": "quorum"}}


@pytest.mark.unit
def test_purge_queue_captures_message_count_then_deletes_contents():
    conn = rabbit_conn({
        ("GET", "/api/queues/%2F/orders"): _Q,
        ("DELETE", "/api/queues/%2F/orders/contents"): {},
    })
    out = ops.purge_queue(conn, "/", "orders")
    assert out["priorState"] == {"messages": 1234}
    methods = [(m, p) for m, p, _ in conn._client.requests]
    assert methods == [("GET", "/api/queues/%2F/orders"),
                       ("DELETE", "/api/queues/%2F/orders/contents")]


@pytest.mark.unit
def test_delete_queue_captures_definition_before_deleting():
    conn = rabbit_conn({
        ("GET", "/api/queues/%2F/orders"): _Q,
        ("DELETE", "/api/queues/%2F/orders"): {},
    })
    out = ops.delete_queue(conn, "/", "orders")
    assert out["priorState"]["durable"] is True
    assert out["priorState"]["autoDelete"] is False
    assert out["priorState"]["arguments"] == {"x-queue-type": "quorum"}
    assert out["priorState"]["messages"] == 1234
    assert "NOT restored" in out["note"]


@pytest.mark.unit
def test_write_path_percent_encodes_hostile_queue_names():
    hostile = "../admin/users"
    conn = rabbit_conn({
        ("GET", "/api/queues/%2F/..%2Fadmin%2Fusers"): _Q,
        ("DELETE", "/api/queues/%2F/..%2Fadmin%2Fusers/contents"): {},
    })
    ops.purge_queue(conn, "/", hostile)
    for _method, path, _kw in conn._client.requests:
        assert "../" not in path


@pytest.mark.unit
def test_declare_queue_records_whether_it_existed():
    conn = rabbit_conn({
        ("GET", "/api/queues/%2F/fresh"): FakeResponse(404, {"error": "Object Not Found"}),
        ("PUT", "/api/queues/%2F/fresh"): {},
    })
    out = ops.declare_queue(conn, "/", "fresh", durable=True)
    assert out["priorState"] == {"existed": False}
    put = [(m, p, kw) for m, p, kw in conn._client.requests if m == "PUT"][0]
    assert put[2]["json"] == {"durable": True, "auto_delete": False, "arguments": {}}


# ── rabbitmq policy writes prior-state capture ───────────────────────────────

_POLICY = {"name": "lim", "vhost": "/", "pattern": "^work\\.", "priority": 3,
           "apply-to": "queues", "definition": {"max-length": 5000}}


@pytest.mark.unit
def test_set_policy_captures_prior_policy():
    conn = rabbit_conn({
        ("GET", "/api/policies/%2F/lim"): _POLICY,
        ("PUT", "/api/policies/%2F/lim"): {},
    })
    out = ops.set_policy(conn, "/", "lim", "^work\\.", {"max-length": 9000}, priority=4,
                         apply_to="queues")
    assert out["priorState"]["existed"] is True
    assert out["priorState"]["policy"]["definition"] == {"max-length": 5000}
    assert out["priorState"]["policy"]["priority"] == 3


@pytest.mark.unit
def test_set_policy_new_records_not_existed_and_validates_definition():
    conn = rabbit_conn({
        ("GET", "/api/policies/%2F/new"): FakeResponse(404, {"error": "Object Not Found"}),
        ("PUT", "/api/policies/%2F/new"): {},
    })
    out = ops.set_policy(conn, "/", "new", ".*", {"message-ttl": 60000})
    assert out["priorState"] == {"existed": False, "policy": None}
    with pytest.raises(ValueError, match="non-empty object"):
        ops.set_policy(conn, "/", "bad", ".*", {})


@pytest.mark.unit
def test_delete_policy_captures_prior_and_teaches_when_missing():
    conn = rabbit_conn({
        ("GET", "/api/policies/%2F/lim"): _POLICY,
        ("DELETE", "/api/policies/%2F/lim"): {},
    })
    out = ops.delete_policy(conn, "/", "lim")
    assert out["priorState"]["policy"]["pattern"] == "^work\\."
    missing = rabbit_conn({
        ("GET", "/api/policies/%2F/ghost"): FakeResponse(404, {"error": "nf"}),
    })
    with pytest.raises(KeyError, match="does not exist"):
        ops.delete_policy(missing, "/", "ghost")


# ── governed tools: undo tokens recorded + risk tiers + dry-run ─────────────


@pytest.mark.unit
def test_governed_config_set_records_undo_token(monkeypatch):
    from mcp_server.tools import writes as t
    from queue_aiops.governance.undo import get_undo_store

    conn = redis_conn(config={"maxmemory-policy": "noeviction"})
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)

    result = t.redis_config_set(parameter="maxmemory-policy", value="allkeys-lru")

    assert "_undo_id" in result
    recorded = [
        u for u in get_undo_store().list() if u.get("undo_tool") == "redis_config_set"
    ]
    assert recorded, "undo store must hold the inverse config_set"
    assert json.loads(recorded[0]["undo_params"]) == {
        "parameter": "maxmemory-policy", "value": "noeviction",
    }


@pytest.mark.unit
def test_governed_delete_queue_undo_replays_through_declare_queue(monkeypatch):
    """THE replay test: delete_queue's recorded undo, fed back into the
    declare_queue tool with exactly its recorded params, must re-issue the PUT
    with the captured definition."""
    from mcp_server.tools import writes as t
    from queue_aiops.governance.undo import get_undo_store

    conn = rabbit_conn({
        ("GET", "/api/queues/%2F/orders"): _Q,
        ("DELETE", "/api/queues/%2F/orders"): {},
        ("PUT", "/api/queues/%2F/orders"): {},
    })
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)

    result = t.delete_queue(vhost="/", name="orders")
    assert result.get("_undo_id")

    undo = [u for u in get_undo_store().list() if u.get("undo_tool") == "declare_queue"][0]
    # Replay: call the target tool with EXACTLY the recorded params.
    # After the delete, the queue is gone — the GET must 404 for the replay.
    conn._client.routes[("GET", "/api/queues/%2F/orders")] = FakeResponse(404, {})
    replayed = getattr(t, undo["undo_tool"])(**json.loads(undo["undo_params"]))

    assert "error" not in replayed
    assert replayed["action"] == "declare_queue"
    put = [(m, p, kw) for m, p, kw in conn._client.requests if m == "PUT"][-1]
    assert put[1] == "/api/queues/%2F/orders"
    assert put[2]["json"] == {"durable": True, "auto_delete": False,
                              "arguments": {"x-queue-type": "quorum"}}


@pytest.mark.unit
def test_governed_set_policy_undo_replays_prior_policy(monkeypatch):
    from mcp_server.tools import writes as t
    from queue_aiops.governance.undo import get_undo_store

    conn = rabbit_conn({
        ("GET", "/api/policies/%2F/lim"): _POLICY,
        ("PUT", "/api/policies/%2F/lim"): {},
    })
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)

    t.set_policy(vhost="/", name="lim", pattern=".*", definition={"max-length": 1})
    undo = [u for u in get_undo_store().list() if u.get("undo_tool") == "set_policy"][0]
    replayed = getattr(t, undo["undo_tool"])(**json.loads(undo["undo_params"]))
    assert "error" not in replayed
    put = [(m, p, kw) for m, p, kw in conn._client.requests if m == "PUT"][-1]
    assert put[2]["json"]["definition"] == {"max-length": 5000}
    assert put[2]["json"]["priority"] == 3


@pytest.mark.unit
def test_undo_descriptor_shapes_invert_correctly():
    from mcp_server.tools import writes as t

    cfg = t._config_set_undo({"parameter": "maxmemory"}, {"priorState": {"value": "100mb"}})
    assert cfg["tool"] == "redis_config_set"
    assert cfg["params"] == {"parameter": "maxmemory", "value": "100mb"}

    dq = t._delete_queue_undo(
        {"vhost": "/", "name": "q"},
        {"priorState": {"durable": False, "autoDelete": True, "arguments": {"a": 1}}},
    )
    assert dq["tool"] == "declare_queue"
    assert dq["params"] == {"vhost": "/", "name": "q", "durable": False,
                            "auto_delete": True, "arguments": {"a": 1}}

    # declare on a pre-existing queue records NO undo (deleting it wouldn't undo)
    assert t._declare_queue_undo({"vhost": "/", "name": "q"},
                                 {"priorState": {"existed": True}}) is None
    fresh = t._declare_queue_undo({"vhost": "/", "name": "q"},
                                  {"priorState": {"existed": False}})
    assert fresh["tool"] == "delete_queue"

    # set_policy over an existing policy restores it; over a new one deletes it
    sp = t._set_policy_undo(
        {"vhost": "/", "name": "p"},
        {"priorState": {"existed": True, "policy": {
            "pattern": "^x", "definition": {"k": 1}, "priority": 2, "applyTo": "queues"}}},
    )
    assert sp["tool"] == "set_policy"
    assert sp["params"]["definition"] == {"k": 1}
    assert sp["params"]["apply_to"] == "queues"
    sp_new = t._set_policy_undo({"vhost": "/", "name": "p"},
                                {"priorState": {"existed": False, "policy": None}})
    assert sp_new["tool"] == "delete_policy"

    dp = t._delete_policy_undo(
        {"vhost": "/", "name": "p"},
        {"priorState": {"policy": {"pattern": "^x", "definition": {"k": 1},
                                   "priority": 0, "applyTo": "all"}}},
    )
    assert dp["tool"] == "set_policy"
    assert dp["params"]["pattern"] == "^x"


@pytest.mark.unit
def test_write_risk_tiers():
    from mcp_server.tools import writes as t

    assert t.purge_queue._risk_level == "high"
    assert t.delete_queue._risk_level == "high"
    for fn in (t.redis_config_set, t.redis_kill_client, t.declare_queue,
               t.set_policy, t.delete_policy):
        assert fn._risk_level == "medium"


@pytest.mark.unit
def test_dry_run_previews_do_not_mutate(monkeypatch):
    from mcp_server.tools import writes as t

    rconn = redis_conn(config={"maxmemory": "0"})
    qconn = rabbit_conn({("GET", "/api/queues/%2F/orders"): _Q})

    monkeypatch.setattr(t, "_get_connection", lambda target=None: rconn)
    assert t.redis_config_set(parameter="maxmemory", value="1gb", dry_run=True)["dryRun"]
    assert t.redis_kill_client(client_id=7, dry_run=True)["dryRun"]
    assert rconn._client.calls == []

    monkeypatch.setattr(t, "_get_connection", lambda target=None: qconn)
    assert t.purge_queue(vhost="/", name="orders", dry_run=True)["dryRun"]
    assert t.delete_queue(vhost="/", name="orders", dry_run=True)["dryRun"]
    assert t.declare_queue(vhost="/", name="orders", dry_run=True)["dryRun"]
    assert t.set_policy(vhost="/", name="p", pattern=".*",
                        definition={"k": 1}, dry_run=True)["dryRun"]
    assert t.delete_policy(vhost="/", name="p", dry_run=True)["dryRun"]
    assert qconn._client.requests == []


@pytest.mark.unit
def test_irreversible_writes_record_no_undo(monkeypatch):
    from mcp_server.tools import writes as t
    from queue_aiops.governance.undo import get_undo_store

    conn = rabbit_conn({
        ("GET", "/api/queues/%2F/orders"): _Q,
        ("DELETE", "/api/queues/%2F/orders/contents"): {},
    })
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)
    result = t.purge_queue(vhost="/", name="orders")
    assert result["priorState"] == {"messages": 1234}
    assert "_undo_id" not in result
    assert all(u.get("undo_tool") != "purge_queue" for u in get_undo_store().list())
