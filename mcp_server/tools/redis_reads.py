"""redis read MCP tools — server, memory, clients, slowlog, config, keys."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from queue_aiops.governance import governed_tool
from queue_aiops.ops import redis_reads as ops


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_server_info(target: Optional[str] = None) -> dict:
    """[READ] redis server identity + health basics: version, role, clients, ops/sec, hit rate.

    Args:
        target: redis target name from config; omit for the default.
    """
    return ops.server_info(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_memory_stats(target: Optional[str] = None) -> dict:
    """[READ] redis memory posture: used vs maxmemory, eviction policy, fragmentation, peaks.

    Args:
        target: redis target name from config; omit for the default.
    """
    return ops.memory_stats(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_clients(target: Optional[str] = None) -> dict:
    """[READ] Connected redis clients grouped by source address, busiest first.

    Args:
        target: redis target name from config; omit for the default.

    Returns an envelope: {"clients": [...], "returned": N, "limit": L,
    "truncated": bool}. When "truncated" is true there is more than was
    returned — do not treat this as the complete set.
    """
    return ops.list_clients(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_slowlog(count: int = 128, target: Optional[str] = None) -> dict:
    """[READ] Recent SLOWLOG entries, slowest first.

    Args:
        count: Max entries to pull (capped at 128).
        target: redis target name from config; omit for the default.

    Returns an envelope: {"entries": [...], "returned": N, "limit": L,
    "truncated": bool}. When "truncated" is true there is more than was
    returned — do not treat this as the complete set.
    """
    return ops.slowlog(_get_connection(target), count)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_config_get(pattern: str = "*", target: Optional[str] = None) -> dict:
    """[READ] CONFIG GET for a glob pattern (e.g. 'maxmemory*').

    Args:
        pattern: Config-name glob (default '*').
        target: redis target name from config; omit for the default.
    """
    return ops.config_get(_get_connection(target), pattern)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_keyspace(target: Optional[str] = None) -> dict:
    """[READ] Per-db key counts and expiry coverage from INFO keyspace.

    Args:
        target: redis target name from config; omit for the default.
    """
    return ops.keyspace(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_big_keys(top: int = 20, target: Optional[str] = None) -> dict:
    """[READ] SCAN-budgeted big-key sample — largest sampled keys first.

    Walks at most 10,000 keys with SCAN and sizes at most 200 of them with
    MEMORY USAGE (never KEYS *), so it is safe on a production instance;
    coveragePct reports how partial the sample is.

    Args:
        top: How many key rows to return, largest first (default 20).
        target: redis target name from config; omit for the default.

    Returns an envelope: {"topKeys": [...], "returned": N, "limit": L,
    "truncated": bool}. When "truncated" is true there is more than was
    returned — do not treat this as the complete set.
    """
    return ops.big_key_sample(_get_connection(target), top)
