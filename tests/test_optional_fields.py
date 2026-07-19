"""Absent fields come back as null, not as an empty string.

An empty string reads as "this field exists and is empty"; a missing field is a
different fact. Broker payloads are full of genuinely-absent values: a queue with
no consumers has no consumer detail, a Redis primary reports no
``master_link_status``, RabbitMQ omits ``idle_since`` for an active queue, and a
SLOWLOG entry can arrive with no command text. Collapsing those into ``""`` hides
the difference, and a smaller local model will confidently invent one.

These tests pin the contract end-to-end: the helper, the ops normalisers, the
consumer that would otherwise crash on a null, and the truncation envelope.
"""

from __future__ import annotations

import pytest

from queue_aiops.governance import opt_str
from queue_aiops.ops import analysis, rabbit_reads, redis_reads
from queue_aiops.ops._util import opt, s
from tests.fakes import rabbit_conn, redis_conn

# ── the helper ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_opt_str_distinguishes_absent_from_empty():
    assert opt_str(None) is None, "absent must stay absent"
    assert opt_str("") == "", "a genuinely empty value is not the same as absent"
    assert opt_str("orders", 64) == "orders"


@pytest.mark.unit
def test_opt_str_still_sanitizes_and_truncates():
    assert opt_str("a\x00b") == "ab"  # control character stripped
    assert opt_str("abcdef", 3) == "abc"


@pytest.mark.unit
def test_opt_str_accepts_non_string_values():
    assert opt_str(42) == "42"


@pytest.mark.unit
def test_ops_opt_helper_preserves_absence_while_s_still_coerces():
    assert opt(None) is None
    assert s(None) == "", "s() keeps its always-present semantics"


# ── the ops layer ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_queue_row_reports_absent_fields_as_none():
    conn = rabbit_conn({("GET", "/api/queues"): [{"name": "orders"}]})
    row = rabbit_reads.list_queues(conn)["queues"][0]
    assert row["name"] == "orders"
    assert row["vhost"] is None
    assert row["state"] is None


@pytest.mark.unit
def test_queue_row_keeps_empty_string_when_source_is_empty():
    """An explicitly empty upstream value is preserved as '' — not turned into null."""
    conn = rabbit_conn({("GET", "/api/queues"): [{"name": "orders", "vhost": ""}]})
    assert rabbit_reads.list_queues(conn)["queues"][0]["vhost"] == ""


@pytest.mark.unit
def test_queue_row_never_drops_the_key_itself():
    """Keys are always present; only their value may be null.

    Omitting a key entirely is worse than a null — the consumer cannot tell the
    field was even considered.
    """
    conn = rabbit_conn({("GET", "/api/queues"): [{}]})
    row = rabbit_reads.list_queues(conn)["queues"][0]
    for key in ("name", "vhost", "state", "messages", "consumers"):
        assert key in row, f"{key} must be present even when the broker omitted it"


@pytest.mark.unit
def test_redis_client_row_reports_absent_name_as_none():
    """An unnamed Redis client has no name — that is not a client named ""."""
    conn = redis_conn(clients=[{"addr": "10.0.0.1:5000"}])
    row = redis_reads.list_clients(conn)["clients"][0]
    assert row["name"] is None


# ── the consumers ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_slowlog_digest_survives_an_entry_with_no_command():
    """The regression this guards: None.split() on a command-less entry.

    ``_digest_slowlog`` groups by the leading command word. An entry whose
    command Redis did not report is null now, not "" — it must group under
    "(UNKNOWN)" rather than crash or invent an empty command name.
    """
    out = analysis._digest_slowlog([{"command": None, "durationUs": 500}])
    assert out[0]["command"] == "(UNKNOWN)"
    assert out[0]["sample"] is None, "the absent command stays absent in the sample"


@pytest.mark.unit
def test_slowlog_digest_still_groups_real_commands():
    out = analysis._digest_slowlog([
        {"command": "HGETALL big:hash", "durationUs": 900},
        {"command": "HGETALL other:hash", "durationUs": 100},
    ])
    assert len(out) == 1 and out[0]["command"] == "HGETALL" and out[0]["count"] == 2


# ── truncation announces itself ─────────────────────────────────────────────


@pytest.mark.unit
def test_slowlog_reports_truncation_when_more_entries_exist():
    """Truncation is measured (count + 1 requested), not guessed."""
    entries = [{"id": i, "duration": i * 10, "command": b"GET k"} for i in range(6)]
    conn = redis_conn(slowlog=entries)
    out = redis_reads.slowlog(conn, count=5)
    assert out["returned"] == 5
    assert out["limit"] == 5
    assert out["truncated"] is True


@pytest.mark.unit
def test_slowlog_is_not_marked_truncated_when_it_fits():
    entries = [{"id": i, "duration": i * 10, "command": b"GET k"} for i in range(3)]
    conn = redis_conn(slowlog=entries)
    out = redis_reads.slowlog(conn, count=5)
    assert out["returned"] == 3 and out["truncated"] is False


@pytest.mark.unit
def test_slowlog_asks_redis_for_one_extra_entry():
    """Without the +1 the count could never exceed the limit, so nothing would
    ever look truncated — the measurement depends on over-fetching by one."""
    conn = redis_conn(slowlog=[])
    redis_reads.slowlog(conn, count=10)
    calls = [c for c in conn._client.calls if c[0] == "slowlog_get"]
    assert calls[-1][1] == 11


@pytest.mark.unit
def test_queue_list_carries_the_standard_envelope():
    conn = rabbit_conn({("GET", "/api/queues"): [{"name": "orders"}]})
    out = rabbit_reads.list_queues(conn)
    for key in ("queues", "returned", "limit", "truncated"):
        assert key in out, f"{key} is part of the standard envelope"
    assert out["truncated"] is False
    assert out["returned"] == len(out["queues"])


@pytest.mark.unit
def test_undo_list_envelope_measures_truncation(monkeypatch):
    from mcp_server.tools import undo as undo_tools

    rows = [
        {
            "undo_id": f"u{i}",
            "ts": "2026-07-18T00:00:00Z",
            "tool": "some_tool",
            "undo_tool": "some_inverse_tool",
            "note": "",
        }
        for i in range(4)
    ]
    captured = {}

    class _Store:
        def list(self, *, status=None, limit=50):
            captured["limit"] = limit
            return rows[:limit]

    monkeypatch.setattr(undo_tools, "get_undo_store", lambda: _Store())
    result = undo_tools.undo_list(limit=3)
    assert captured["limit"] == 4, "one extra row is fetched to measure truncation"
    assert result["returned"] == 3
    assert result["limit"] == 3
    assert result["truncated"] is True
    assert len(result["undos"]) == 3
