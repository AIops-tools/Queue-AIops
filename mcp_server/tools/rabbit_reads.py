"""rabbitmq read MCP tools — overview, queues, connections, channels, policies, nodes."""

from typing import Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from queue_aiops.governance import governed_tool
from queue_aiops.ops import rabbit_reads as ops


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def rabbitmq_overview(target: Optional[str] = None) -> dict:
    """[READ] Broker identity + totals: version, queue/message counts, rates, churn.

    Args:
        target: rabbitmq target name from config; omit for the default.
    """
    return ops.broker_overview(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def list_queues(vhost: Optional[str] = None, target: Optional[str] = None) -> dict:
    """[READ] Queues (optionally one vhost), deepest backlog first.

    Args:
        vhost: Restrict to one vhost (the default vhost is '/'); omit for all.
        target: rabbitmq target name from config; omit for the default.
    """
    return ops.list_queues(_get_connection(target), vhost)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def queue_detail(vhost: str, name: str, target: Optional[str] = None) -> dict:
    """[READ] One queue's full detail: counts, rates, consumers, memory, arguments.

    Args:
        vhost: The queue's vhost (the default vhost is '/').
        name: Queue name (from list_queues).
        target: rabbitmq target name from config; omit for the default.
    """
    return ops.queue_detail(_get_connection(target), vhost, name)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def list_connections(target: Optional[str] = None) -> dict:
    """[READ] Client connections grouped by peer host, busiest first.

    Args:
        target: rabbitmq target name from config; omit for the default.
    """
    return ops.list_connections(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def list_channels(target: Optional[str] = None) -> dict:
    """[READ] Channels with unacked/prefetch pressure, most unacked first.

    Args:
        target: rabbitmq target name from config; omit for the default.
    """
    return ops.list_channels(_get_connection(target))


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def list_policies(vhost: Optional[str] = None, target: Optional[str] = None) -> dict:
    """[READ] Policies (optionally one vhost) with pattern, priority, definition.

    Args:
        vhost: Restrict to one vhost (the default vhost is '/'); omit for all.
        target: rabbitmq target name from config; omit for the default.
    """
    return ops.list_policies(_get_connection(target), vhost)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def node_health(target: Optional[str] = None) -> dict:
    """[READ] Node memory/disk/fd posture + watermark alarms, most loaded first.

    Args:
        target: rabbitmq target name from config; omit for the default.
    """
    return ops.node_health(_get_connection(target))
