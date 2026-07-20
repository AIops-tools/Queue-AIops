"""Refuse killing the redis connection this tool is calling through.

The sibling DBA tools have always filtered themselves out of their activity
reads (Postgres ``WHERE pid <> pg_backend_pid()``, MySQL ``WHERE id <>
CONNECTION_ID()``). Queue-AIops did not: ``list_clients`` returned the raw
CLIENT LIST, so an agent asked to "kill the idle client" could be handed the
tool's own id and then pass it straight to ``kill_client``. CLIENT KILL has no
undo, so that is the purest form of an operation destroying its own
reversibility — the kill ends the session the audit row is written from.

CLIENT ID supplies the missing primitive on both paths: it hides the row from
the read and refuses it on the write. The guard must be EXACT (every other
client stays killable) and must FAIL OPEN when CLIENT ID cannot be determined —
an unknown identity may never read as "it is me", and on the read path it may
never silently hide some other client's row either.
"""

from __future__ import annotations

import pytest

from queue_aiops.ops import redis_reads as reads
from queue_aiops.ops import writes as ops
from queue_aiops.ops.writes import SelfLockout
from tests.fakes import redis_conn

_OWN_ID = 11
_OWN_ADDR = "10.0.0.5:52000"
_OTHER_ADDR = "10.0.0.9:41000"

_CLIENTS = [
    {"id": _OWN_ID, "addr": _OWN_ADDR, "name": "queue-aiops", "age": 3,
     "idle": 0, "cmd": "client|id", "db": "0", "flags": "N"},
    {"id": 22, "addr": _OTHER_ADDR, "name": "worker", "age": 900,
     "idle": 880, "cmd": "get", "db": "0", "flags": "N"},
]


def _conn(client_id: int | None = _OWN_ID):
    return redis_conn(clients=_CLIENTS, client_id=client_id)


# ── the read path stops handing out the tool's own id ───────────────────────


@pytest.mark.unit
def test_list_clients_excludes_this_tools_own_connection():
    out = reads.list_clients(_conn())
    ids = [c["id"] for c in out["clients"]]
    assert str(_OWN_ID) not in ids, "the tool's own connection must not be listed"
    assert "22" in ids, "every other client must still be listed"


@pytest.mark.unit
def test_the_excluded_row_is_not_counted_either():
    """total/returned must agree with the rows actually shown."""
    out = reads.list_clients(_conn())
    assert out["total"] == 1 and out["returned"] == 1
    assert sum(e["clients"] for e in out["bySource"]) == 1


@pytest.mark.unit
def test_list_clients_returns_everything_when_the_id_is_unknown():
    """Fail open: an unknown identity must not hide some other client's row."""
    out = reads.list_clients(_conn(client_id=None))
    assert out["total"] == 2, "unfiltered is the honest answer when CLIENT ID fails"


# ── the write path refuses it, by id and by addr ────────────────────────────


@pytest.mark.unit
def test_kill_client_refuses_this_tools_own_id():
    with pytest.raises(SelfLockout, match="calling through"):
        ops.kill_client(_conn(), client_id=_OWN_ID)


@pytest.mark.unit
def test_kill_client_refuses_this_tools_own_addr():
    """The addr selector must not be a way around the id check."""
    with pytest.raises(SelfLockout, match="calling through"):
        ops.kill_client(_conn(), addr=_OWN_ADDR)


@pytest.mark.unit
def test_the_refusal_says_why_and_what_to_do_instead():
    with pytest.raises(SelfLockout) as ei:
        ops.kill_client(_conn(), client_id=_OWN_ID)
    msg = str(ei.value)
    assert "no undo" in msg, "must name the concrete failure: a kill is irreversible"
    assert "list_clients" in msg, "must offer the route that does work"


@pytest.mark.unit
def test_no_kill_reaches_the_wire_when_refused():
    conn = _conn()
    with pytest.raises(SelfLockout):
        ops.kill_client(conn, client_id=_OWN_ID)
    assert not any(c[0] == "client_kill_filter" for c in conn._client.calls)


# ── exactness: every other client stays killable ────────────────────────────


@pytest.mark.unit
def test_another_client_is_still_killed_by_id():
    conn = _conn()
    out = ops.kill_client(conn, client_id=22)
    assert out["action"] == "kill_client" and out["killed"] == 1
    assert ("client_kill_filter", "22", None) in conn._client.calls


@pytest.mark.unit
def test_another_client_is_still_killed_by_addr():
    conn = _conn()
    out = ops.kill_client(conn, addr=_OTHER_ADDR)
    assert out["killed"] == 1
    assert ("client_kill_filter", None, _OTHER_ADDR) in conn._client.calls


# ── fail open: unknown identity is never read as "it is me" ─────────────────


@pytest.mark.unit
def test_kill_proceeds_when_client_id_is_undeterminable():
    conn = _conn(client_id=None)
    out = ops.kill_client(conn, client_id=_OWN_ID)
    assert out["killed"] == 1, "an undeterminable id must fail OPEN, not closed"


@pytest.mark.unit
def test_kill_proceeds_when_the_client_id_probe_raises():
    conn = _conn()

    def _boom():
        raise RuntimeError("CLIENT ID unsupported")

    conn._client.client_id = _boom
    out = ops.kill_client(conn, client_id=_OWN_ID)
    assert out["killed"] == 1, "a failed probe must fail OPEN"


@pytest.mark.unit
def test_a_missing_addr_match_does_not_invent_a_self_target():
    """An addr nobody holds is not this tool's addr."""
    conn = _conn()
    out = ops.kill_client(conn, addr="10.0.0.99:1")
    assert out["killed"] == 1


@pytest.mark.unit
def test_the_guard_is_reachable_without_performing_the_kill():
    """The MCP wrapper calls this ahead of its dry_run return."""
    conn = _conn()
    ops.guard_kill_client(conn, client_id=22)  # a non-self target is silently allowed
    with pytest.raises(SelfLockout):
        ops.guard_kill_client(conn, client_id=_OWN_ID)
    assert not any(c[0] == "client_kill_filter" for c in conn._client.calls)


@pytest.mark.unit
def test_an_empty_selector_still_fails_before_the_guard():
    """The pre-existing argument check must keep its own error."""
    with pytest.raises(ValueError, match="Pass client_id"):
        ops.kill_client(_conn())
