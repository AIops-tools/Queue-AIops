"""CLI confirmed-write path for the remaining rabbitmq write commands.

Complements test_cli_writes.py (which covers redis config-set + purge): drives
``delete-queue`` / ``declare-queue`` / ``set-policy`` / ``delete-policy`` PAST
dry-run and the double-confirm prompts and asserts the write really went
through the governed twin — the DELETE/PUT lands on the management endpoint AND
an audit row is written (the CLI-writes-are-audited guarantee). Also covers the
delete-queue → declare_queue undo capture, dry-run making no call, and the
set-policy JSON-validation error path. The fakes are injected — no live broker.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

import queue_aiops.governance.audit as audit_mod
import queue_aiops.governance.policy as policy_mod
import queue_aiops.governance.undo as undo_mod
from tests.fakes import FakeResponse, rabbit_conn

runner = CliRunner()

_Q = {"name": "orders", "vhost": "/", "messages": 42, "durable": True,
      "auto_delete": False, "arguments": {"x-queue-type": "quorum"}}
_POLICY = {"name": "lim", "vhost": "/", "pattern": "^work\\.", "priority": 3,
           "apply-to": "queues", "definition": {"max-length": 5000}}


@pytest.fixture
def gov_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_AIOPS_HOME", str(tmp_path))
    monkeypatch.setenv("QUEUE_AUDIT_APPROVED_BY", "queueops-alice")
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()
    yield tmp_path
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()


def _wire(monkeypatch, routes):
    from mcp_server.tools import writes as gov

    conn = rabbit_conn(routes)
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    return conn


def _audit_tools(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT tool FROM audit_log ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.unit
def test_cli_delete_queue_confirmed_audits_and_records_replay_undo(gov_home, monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, {
        ("GET", "/api/queues/%2F/orders"): _Q,
        ("DELETE", "/api/queues/%2F/orders"): {},
    })
    result = runner.invoke(app, ["rabbitmq", "delete-queue", "orders"], input="y\ny\n")
    assert result.exit_code == 0, result.output
    methods = [(m, p) for m, p, _ in conn._client.requests]
    assert ("DELETE", "/api/queues/%2F/orders") in methods
    assert _audit_tools(gov_home / "audit.db") == ["delete_queue"]
    # undo re-declares the captured definition through declare_queue
    undo = [u for u in undo_mod.get_undo_store().list()
            if u.get("undo_tool") == "declare_queue"][0]
    assert json.loads(undo["undo_params"])["arguments"] == {"x-queue-type": "quorum"}


@pytest.mark.unit
def test_cli_declare_queue_confirmed_puts_and_audits(gov_home, monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, {
        ("GET", "/api/queues/%2F/fresh"): FakeResponse(404, {"error": "nf"}),
        ("PUT", "/api/queues/%2F/fresh"): {},
    })
    result = runner.invoke(
        app, ["rabbitmq", "declare-queue", "fresh", "--transient"], input="y\ny\n"
    )
    assert result.exit_code == 0, result.output
    put = [(m, p, kw) for m, p, kw in conn._client.requests if m == "PUT"][0]
    assert put[2]["json"]["durable"] is False
    assert _audit_tools(gov_home / "audit.db") == ["declare_queue"]


@pytest.mark.unit
def test_cli_set_policy_confirmed_puts_definition_and_audits(gov_home, monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, {
        ("GET", "/api/policies/%2F/lim"): FakeResponse(404, {"error": "nf"}),
        ("PUT", "/api/policies/%2F/lim"): {},
    })
    result = runner.invoke(
        app,
        ["rabbitmq", "set-policy", "lim", "^work\\.", '{"max-length": 9000}',
         "--apply-to", "queues"],
        input="y\ny\n",
    )
    assert result.exit_code == 0, result.output
    put = [(m, p, kw) for m, p, kw in conn._client.requests if m == "PUT"][0]
    assert put[2]["json"]["definition"] == {"max-length": 9000}
    assert put[2]["json"]["apply-to"] == "queues"
    assert _audit_tools(gov_home / "audit.db") == ["set_policy"]


@pytest.mark.unit
def test_cli_set_policy_rejects_invalid_json_definition(gov_home, monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, {})
    result = runner.invoke(
        app, ["rabbitmq", "set-policy", "lim", ".*", "not-json"], input="y\ny\n"
    )
    assert result.exit_code == 1
    assert "must be valid JSON" in result.output
    assert conn._client.requests == []  # never reached the broker


@pytest.mark.unit
def test_cli_delete_policy_confirmed_deletes_and_audits(gov_home, monkeypatch):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, {
        ("GET", "/api/policies/%2F/lim"): _POLICY,
        ("DELETE", "/api/policies/%2F/lim"): {},
    })
    result = runner.invoke(app, ["rabbitmq", "delete-policy", "lim"], input="y\ny\n")
    assert result.exit_code == 0, result.output
    methods = [(m, p) for m, p, _ in conn._client.requests]
    assert ("DELETE", "/api/policies/%2F/lim") in methods
    assert _audit_tools(gov_home / "audit.db") == ["delete_policy"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argv", "op"),
    [
        (["rabbitmq", "delete-queue", "orders", "--dry-run"], "delete_queue"),
        (["rabbitmq", "declare-queue", "orders", "--dry-run"], "declare_queue"),
        (["rabbitmq", "delete-policy", "lim", "--dry-run"], "delete_policy"),
    ],
)
def test_cli_rabbit_write_dry_run_makes_no_call_and_no_audit(gov_home, monkeypatch, argv, op):
    from queue_aiops.cli import app

    conn = _wire(monkeypatch, {})
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert conn._client.requests == []
    assert not (gov_home / "audit.db").exists()
