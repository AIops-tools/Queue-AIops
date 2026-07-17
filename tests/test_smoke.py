"""Smoke tests for queue-aiops.

Proves: every module imports, the CLI builds and --help works, the MCP server
exposes the expected tool surface and EVERY tool carries the harness marker
``_is_governed_tool``, and config platform validation works. No real
redis/rabbitmq is needed.
"""

import asyncio
import importlib

import pytest
from typer.testing import CliRunner

# Kept in sync with mcp_server/server.py (the full registered tool surface).
EXPECTED_TOOLS = {
    # overview
    "queue_overview",
    # redis reads
    "redis_server_info", "redis_memory_stats", "redis_clients", "redis_slowlog",
    "redis_config_get", "redis_keyspace", "redis_big_keys",
    # rabbitmq reads
    "rabbitmq_overview", "list_queues", "queue_detail", "list_connections",
    "list_channels", "list_policies", "node_health",
    # analysis (flagship)
    "redis_memory_pressure_rca", "redis_latency_rca",
    "rabbitmq_queue_backlog_rca", "connection_churn_analysis",
    # writes
    "redis_config_set", "redis_kill_client", "purge_queue", "delete_queue",
    "declare_queue", "set_policy", "delete_policy",
}


@pytest.mark.unit
def test_all_modules_import():
    for name in (
        "queue_aiops", "queue_aiops.config", "queue_aiops.connection",
        "queue_aiops.platform", "queue_aiops.doctor",
        "queue_aiops.secretstore",
        "queue_aiops.ops.redis_reads", "queue_aiops.ops.rabbit_reads",
        "queue_aiops.ops.analysis", "queue_aiops.ops.writes",
        "queue_aiops.ops.overview",
        "queue_aiops.cli", "queue_aiops.cli._root", "queue_aiops.cli._common",
        "queue_aiops.cli.init", "queue_aiops.cli.secret",
        "queue_aiops.cli.redis_cmds", "queue_aiops.cli.rabbit_cmds",
        "queue_aiops.cli.analyze", "queue_aiops.cli.overview",
        "queue_aiops.cli.doctor",
        "mcp_server.server", "mcp_server._shared",
        "mcp_server.tools.redis_reads", "mcp_server.tools.rabbit_reads",
        "mcp_server.tools.analysis", "mcp_server.tools.writes",
        "mcp_server.tools.overview",
    ):
        importlib.import_module(name)


@pytest.mark.unit
def test_version_matches_pyproject():
    """__version__ is single-sourced from package metadata; it must track
    pyproject.toml so a release bump can never ship a stale self-report."""
    import tomllib
    from pathlib import Path

    import queue_aiops

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"]
    assert queue_aiops.__version__ == expected


@pytest.mark.unit
def test_cli_app_builds_and_help_works():
    from queue_aiops.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("redis", "rabbitmq", "analyze", "secret", "init", "overview",
                "doctor", "mcp"):
        assert sub in result.output


@pytest.mark.unit
def test_cli_leaf_help_triggers_lazy_imports():
    from queue_aiops.cli import app

    runner = CliRunner()
    for cmd in (
        ["redis", "--help"], ["rabbitmq", "--help"], ["analyze", "--help"],
        ["secret", "--help"], ["doctor", "--help"], ["overview", "--help"],
        ["init", "--help"],
        ["redis", "info", "--help"], ["redis", "bigkeys", "--help"],
        ["redis", "config-set", "--help"], ["redis", "kill-client", "--help"],
        ["rabbitmq", "queues", "--help"], ["rabbitmq", "purge", "--help"],
        ["rabbitmq", "delete-queue", "--help"], ["rabbitmq", "set-policy", "--help"],
        ["analyze", "memory", "--help"], ["analyze", "latency", "--help"],
        ["analyze", "backlog", "--help"], ["analyze", "churn", "--help"],
        ["secret", "list", "--help"], ["secret", "set", "--help"],
    ):
        result = runner.invoke(app, cmd)
        assert result.exit_code == 0, f"{cmd} failed: {result.output}"


@pytest.mark.unit
def test_mcp_list_tools_exposes_expected_tools():
    from mcp_server.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"


@pytest.mark.unit
def test_every_mcp_tool_is_governed_by_harness():
    from mcp_server import _shared

    tool_objs = _shared.mcp._tool_manager._tools
    assert EXPECTED_TOOLS <= set(tool_objs), "tool registry incomplete"
    for name, tool in tool_objs.items():
        fn = getattr(tool, "fn", None)
        assert fn is not None, f"{name} has no fn"
        assert getattr(fn, "_is_governed_tool", False), f"{name} missing @governed_tool"


@pytest.mark.unit
def test_tool_count_is_expected():
    from mcp_server import _shared

    assert len(_shared.mcp._tool_manager._tools) == 28


@pytest.mark.unit
def test_config_rejects_unknown_platform():
    from queue_aiops.config import TargetConfig

    with pytest.raises(ValueError, match="platform must be one of"):
        TargetConfig(name="x", platform="kafka", host="h")


@pytest.mark.unit
def test_config_defaults_port_per_platform():
    from queue_aiops.config import TargetConfig

    assert TargetConfig(name="c", platform="redis", host="h").port == 6379
    assert TargetConfig(name="b", platform="rabbitmq", host="h").port == 15672


@pytest.mark.unit
def test_redis_secret_is_optional_rabbitmq_secret_is_not(monkeypatch):
    from queue_aiops.config import TargetConfig

    monkeypatch.delenv("QUEUE_C_SECRET", raising=False)
    monkeypatch.delenv("QUEUE_B_SECRET", raising=False)
    assert TargetConfig(name="c", platform="redis", host="h").secret == ""
    with pytest.raises(OSError, match="No secret for target 'b'"):
        _ = TargetConfig(name="b", platform="rabbitmq", host="h", username="u").secret
