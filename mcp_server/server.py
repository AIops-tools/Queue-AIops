"""MCP server wrapping queue-aiops operations (stdio transport).

Thin adapter layer: each ``@mcp.tool()`` function (in ``mcp_server/tools/``)
delegates to the ``queue_aiops`` ops package and is wrapped with the
queue-aiops ``@governed_tool`` harness (audit / budget / undo / risk-tier).

Standalone, self-governed broker operations over redis and rabbitmq:
redis server/memory/clients/slowlog/config/keyspace/big-key reads, rabbitmq
overview/queues/connections/channels/policies/node reads, four flagship
analyses, and governed writes (redis config set + client kill; rabbitmq queue
purge/delete/declare and policy set/delete).

Source: https://github.com/AIops-tools/Queue-AIops
License: MIT
"""

import logging
import os

from mcp_server._shared import _safe_error, mcp, tool_errors

# Importing the tool modules registers every @mcp.tool() onto the shared
# `mcp` instance. Order does not matter; each module is self-contained.
from mcp_server.tools import (  # noqa: F401 — side effects
    analysis,
    overview,
    rabbit_reads,
    redis_reads,
    undo,
    writes,
)

__all__ = ["mcp", "main", "_safe_error", "tool_errors"]

logger = logging.getLogger(__name__)



def main() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO)
    # Read-only mode was removed. Warn a deployment that still exports the old
    # switch so it gets one audible signal instead of silently gaining writes.
    if os.environ.get("QUEUE_READ_ONLY"):
        logger.warning(
            "QUEUE_READ_ONLY is set but no longer has any effect — "
            "read-only mode was removed. Writes ARE enabled. Restrict them via "
            "the connecting account's permissions instead (a read-only socket / "
            "scope-limited token)."
        )
    mcp.run(transport="stdio")
