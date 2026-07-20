"""Refuse config writes that destroy their own reversibility.

``CONFIG SET requirepass`` is reachable through ``config_set`` — the parameter
name passes the plain-word regex like any other. Setting it invalidates the
credential this connection authenticates with (connection.py passes
``target.secret`` as the redis password), so the undo the harness just recorded
would have to replay over a connection that can no longer authenticate. A
governed, reversible tool must not offer an action that removes the ability to
reverse it. ``bind`` / ``port`` / ``protected-mode`` / ``maxclients`` /
``unixsocket`` / ``aclfile`` / ``tls-*`` break the same loop via the socket
instead of the credential.

Ordering is load-bearing too: the prior value of ``requirepass`` IS a plaintext
credential, so the refusal must come BEFORE any prior-state capture, or the
secret lands in audit.db and the undo store.

The denylist is static, so there is no fail-open case — but it must be EXACT:
ordinary tuning parameters have to keep working.
"""

from __future__ import annotations

import json

import pytest

from queue_aiops.ops import writes as ops
from queue_aiops.ops.writes import SelfLockout
from tests.fakes import redis_conn


@pytest.mark.unit
def test_setting_requirepass_is_refused():
    conn = redis_conn(config={"requirepass": "current-password"})
    with pytest.raises(SelfLockout, match="invalidates the credential"):
        ops.config_set(conn, "requirepass", "new-password")


@pytest.mark.unit
def test_the_refusal_says_why_and_what_to_do_instead():
    conn = redis_conn(config={"requirepass": "current-password"})
    with pytest.raises(SelfLockout) as ei:
        ops.config_set(conn, "requirepass", "new-password")
    msg = str(ei.value)
    assert "reversibility" in msg, "must name the concrete failure: the undo cannot replay"
    assert "redis.conf" in msg, "must offer the route that does work"


@pytest.mark.unit
def test_refusal_happens_before_the_prior_value_is_read():
    """The prior value of requirepass is a plaintext credential.

    Capturing it would write the live password into audit.db and the undo store,
    so nothing may reach the wire before the denylist is consulted.
    """
    conn = redis_conn(config={"requirepass": "current-password"})
    with pytest.raises(SelfLockout):
        ops.config_set(conn, "requirepass", "new-password")
    assert conn._client.calls == [], "no CONFIG GET may run for a denylisted parameter"


@pytest.mark.unit
@pytest.mark.parametrize(
    "parameter",
    ["masterauth", "bind", "protected-mode", "maxclients", "port", "unixsocket",
     "aclfile", "tls-cert-file", "tls-port"],
)
def test_every_self_affecting_parameter_is_refused(parameter):
    conn = redis_conn(config={parameter: "current"})
    with pytest.raises(SelfLockout):
        ops.config_set(conn, parameter, "new")
    assert conn._client.calls == []


@pytest.mark.unit
def test_ordinary_parameters_still_work():
    """The guard must be exact — over-blocking would break normal redis tuning."""
    conn = redis_conn(config={"maxmemory-policy": "noeviction"})
    out = ops.config_set(conn, "maxmemory-policy", "allkeys-lru")
    assert out["priorState"] == {"value": "noeviction"}
    assert ("config_set", "maxmemory-policy", "allkeys-lru") in conn._client.calls


@pytest.mark.unit
def test_parameters_that_merely_look_similar_still_work():
    """Only the listed names are refused, not anything containing them."""
    conn = redis_conn(config={"maxmemory": "0", "appendfsync": "everysec"})
    ops.config_set(conn, "maxmemory", "2gb")  # not 'maxclients'
    ops.config_set(conn, "appendfsync", "always")  # not a 'tls-' parameter
    assert ("config_set", "maxmemory", "2gb") in conn._client.calls
    assert ("config_set", "appendfsync", "always") in conn._client.calls


@pytest.mark.unit
def test_denylist_survives_case_and_whitespace():
    """config_set normalises the name first, so the guard cannot be side-stepped."""
    conn = redis_conn(config={"requirepass": "current-password"})
    with pytest.raises(SelfLockout):
        ops.config_set(conn, "  REQUIREPASS  ", "new-password")
    assert conn._client.calls == []


@pytest.mark.unit
def test_self_lockout_is_a_valueerror():
    """Existing 'except ValueError' handling (CLI, tool_errors) keeps working."""
    assert issubclass(SelfLockout, ValueError)


# ── dry_run must report the refusal, not preview a call that will be refused ─
#
# A green preview followed by a refusal is the weak-model trap this line designs
# against: the model reads the refusal as transient and retries. dry_run's whole
# job is to say what would happen, so "it would be refused" IS the right answer.


@pytest.mark.unit
def test_dry_run_of_a_denylisted_parameter_is_refused(monkeypatch):
    from mcp_server.tools import writes as t

    conn = redis_conn(config={"requirepass": "current-password"})
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)

    result = t.redis_config_set(parameter="requirepass", value="new", dry_run=True)

    assert "error" in result, "the preview must report the refusal"
    assert "wouldSet" not in result, "must not also hand back a green preview"
    assert conn._client.calls == [], "the guard is static — a preview needs no I/O"


@pytest.mark.unit
def test_dry_run_of_an_ordinary_parameter_still_previews(monkeypatch):
    """The dry-run guard must be exact, not a blanket refusal of every preview."""
    from mcp_server.tools import writes as t

    conn = redis_conn(config={"maxmemory-policy": "noeviction"})
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)

    result = t.redis_config_set(parameter="maxmemory-policy", value="allkeys-lru",
                                dry_run=True)

    assert result["dryRun"] is True
    assert result["wouldSet"] == {"parameter": "maxmemory-policy", "value": "allkeys-lru"}
    assert conn._client.calls == [], "a preview must not touch the broker"


# ── the CLI preview path must refuse too, and exit non-zero ─────────────────


def _flat(text: str) -> str:
    """Collapse whitespace: rich wraps console output at the terminal width, so a
    phrase can arrive split across two lines. Assert on meaning, not on layout."""
    return " ".join(text.split())


def _cli_dry_run(monkeypatch, tmp_path, argv, conn):
    """Drive a CLI --dry-run with the governed write module pointed at ``conn``."""
    from typer.testing import CliRunner

    import queue_aiops.governance.audit as audit_mod
    import queue_aiops.governance.policy as policy_mod
    import queue_aiops.governance.undo as undo_mod
    from mcp_server.tools import writes as gov
    from queue_aiops.cli import app

    monkeypatch.setenv("QUEUE_AIOPS_HOME", str(tmp_path))
    audit_mod.reset_engine()
    policy_mod.reset_policy_engine()
    undo_mod.reset_undo_store()
    monkeypatch.setattr(gov, "_get_connection", lambda target=None: conn)
    try:
        return CliRunner().invoke(app, argv)
    finally:
        audit_mod.reset_engine()
        policy_mod.reset_policy_engine()
        undo_mod.reset_undo_store()


@pytest.mark.unit
def test_cli_dry_run_of_a_denylisted_parameter_is_refused(monkeypatch, tmp_path):
    """A refused preview must look like a refusal: teaching message, exit 1."""
    conn = redis_conn(config={"requirepass": "current-password"})
    result = _cli_dry_run(monkeypatch, tmp_path,
                          ["redis", "config-set", "requirepass", "new", "--dry-run"],
                          conn)

    assert result.exit_code == 1, result.output
    assert "DRY-RUN" not in _flat(result.output), "must not print a green banner"
    assert "redis.conf" in _flat(result.output), "must carry the route back"
    assert "current-password" not in _flat(result.output), "never echo the credential"


@pytest.mark.unit
def test_cli_dry_run_of_an_ordinary_parameter_still_previews(monkeypatch, tmp_path):
    """Exactness: the CLI guard must not turn into a blanket refusal."""
    conn = redis_conn(config={"maxmemory-policy": "noeviction"})
    result = _cli_dry_run(
        monkeypatch, tmp_path,
        ["redis", "config-set", "maxmemory-policy", "allkeys-lru", "--dry-run"], conn)

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in _flat(result.output)
    assert conn._client.calls == [], "a preview must not touch the broker"


@pytest.mark.unit
def test_the_refusal_reaches_the_agent_intact_through_the_mcp_layer(monkeypatch):
    """The teaching tail must survive _safe_error's length cap.

    ValueError is on the passthrough list, so the message is forwarded rather
    than replaced — but it is truncated. The route back sits at the END of the
    message, so an over-long refusal loses exactly the part the caller acts on.
    """
    from mcp_server.tools import writes as t

    conn = redis_conn(config={"requirepass": "current-password"})
    monkeypatch.setattr(t, "_get_connection", lambda target=None: conn)

    result = t.redis_config_set(parameter="requirepass", value="new-password")

    assert "error" in result, "the refusal must surface as an error, not a success"
    assert "redis.conf" in result["error"], "the route back must not be truncated away"
    assert "current-password" not in json.dumps(result), "never echo the live credential"
