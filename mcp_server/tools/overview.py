"""Overview MCP tool — one-shot broker health (read-only)."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from queue_aiops.governance import governed_tool
from queue_aiops.ops import overview as ops


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def queue_overview(target: Optional[str] = None) -> dict:
    """[READ] One-shot summary: platform + version + backlog/memory/client health.

    Platform-dispatched: a redis target reports memory posture, clients,
    ops/sec and hit rate; a rabbitmq target reports queue/message totals,
    connection/channel counts, rates, and node alarms.

    Args:
        target: Broker target name from config; omit for the default.
    """
    return ops.queue_overview(_get_connection(target))
