"""CLI confirmed-write path — past dry-run, through governance, onto disk.

The CLI write commands delegate real execution to the ``@governed_tool``
functions in ``mcp_server.tools``. These tests drive ``redis config-set`` and
``rabbitmq purge`` PAST the dry-run branch and the double-confirm prompts and
assert the call really went through the governed path (audit row on disk) —
the regression test for the "CLI writes were unaudited" line-wide fix.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

import queue_aiops.governance.audit as audit_mod
import queue_aiops.governance.policy as policy_mod
import queue_aiops.governance.undo as undo_mod
from tests.fakes import rabbit_conn, redis_conn


@pytest.fixture
def gov_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_AIOPS_HOME", str(tmp_path))
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()
    yield tmp_path
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()


@pytest.fixture
def cache_conn(monkeypatch):
    """A fake redis connection wired into the governed write module."""
    from mcp_server.tools import writes as gov

    conn = redis_conn(config={"maxmemory-policy": "noeviction"})
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    return conn


@pytest.fixture
def broker_conn(monkeypatch):
    """A fake rabbitmq connection wired into the governed write module."""
    from mcp_server.tools import writes as gov

    conn = rabbit_conn({
        ("GET", "/api/queues/%2F/orders"): {
            "name": "orders", "vhost": "/", "messages": 42, "durable": True,
            "auto_delete": False, "arguments": {},
        },
        ("DELETE", "/api/queues/%2F/orders/contents"): {},
    })
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    return conn


def _audit_tools(db_path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT tool FROM audit_log ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.unit
def test_cli_config_set_dry_run_mutates_nothing_but_is_audited(gov_home, cache_conn):
    """The invariant is: a dry_run MAY read, it must never WRITE.

    config_set is self-lockout guarded, so its preview routes through the
    governed twin to find out whether the real call would be refused. That also
    lands an audit row — not new behaviour but the removal of an inconsistency:
    MCP dry-runs have always audited, the CLI was the outlier. This particular
    guard is a static denylist, so the preview still touches the broker not at
    all.
    """
    from queue_aiops.cli import app

    result = CliRunner().invoke(
        app, ["redis", "config-set", "maxmemory-policy", "allkeys-lru", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    assert cache_conn._client.calls == []
    assert _audit_tools(gov_home / "audit.db") == ["redis_config_set"]


@pytest.mark.unit
def test_cli_config_set_confirmed_goes_through_governance(gov_home, cache_conn):
    """Confirmed CLI write must execute via the governed twin: the client call
    fires AND an audit row lands in audit.db (this is what the reroute fix
    bought)."""
    from queue_aiops.cli import app

    result = CliRunner().invoke(
        app, ["redis", "config-set", "maxmemory-policy", "allkeys-lru"], input="y\ny\n"
    )
    assert result.exit_code == 0, result.output
    assert ("config_set", "maxmemory-policy", "allkeys-lru") in cache_conn._client.calls
    assert _audit_tools(gov_home / "audit.db") == ["redis_config_set"]


@pytest.mark.unit
def test_cli_config_set_aborts_without_double_confirm(gov_home, cache_conn):
    from queue_aiops.cli import app

    result = CliRunner().invoke(
        app, ["redis", "config-set", "maxmemory-policy", "allkeys-lru"], input="y\nn\n"
    )
    assert result.exit_code != 0
    assert all(c[0] != "config_set" for c in cache_conn._client.calls)
    assert not (gov_home / "audit.db").exists()


@pytest.mark.unit
def test_cli_purge_confirmed_goes_through_governance_with_approver(
    gov_home, broker_conn, monkeypatch
):
    """purge_queue is risk=high: with an approver set, the confirmed CLI purge
    executes and both the DELETE and the audit row happen."""
    monkeypatch.setenv("QUEUE_AUDIT_APPROVED_BY", "queueops-alice")
    from queue_aiops.cli import app

    result = CliRunner().invoke(app, ["rabbitmq", "purge", "orders"], input="y\ny\n")
    assert result.exit_code == 0, result.output
    methods = [(m, p) for m, p, _ in broker_conn._client.requests]
    assert ("DELETE", "/api/queues/%2F/orders/contents") in methods
    assert _audit_tools(gov_home / "audit.db") == ["purge_queue"]


@pytest.mark.unit
def test_cli_purge_dry_run_makes_no_call(gov_home, broker_conn):
    from queue_aiops.cli import app

    result = CliRunner().invoke(app, ["rabbitmq", "purge", "orders", "--dry-run"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    assert broker_conn._client.requests == []
