"""Shared MCP server primitives: the FastMCP instance, connection helper,
error sanitisation, and the ``@tool_errors`` decorator.

Tool modules under ``mcp_server/tools/`` import ``mcp`` from here and register
their ``@mcp.tool()`` functions onto it. ``mcp_server/server.py`` then imports
those modules and runs the server.

Keep ``Optional[X]`` (never PEP 604 ``X | None``) in any FastMCP-reflected
tool signature — on older mcp/pydantic the union eval'd to ``types.UnionType``
crashes FastMCP's ``issubclass`` check.
"""

import functools
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import httpx
import redis
from mcp.server.fastmcp import FastMCP

from queue_aiops.config import load_config
from queue_aiops.connection import ConnectionManager, QueueApiError
from queue_aiops.governance import mark_unknown, sanitize

logger = logging.getLogger(__name__)

_DOCTOR_HINT = "Run 'queue-aiops doctor' to verify connectivity and credentials."


# Failures that leave the request's fate genuinely undetermined: the bytes
# went out and either the response or the rest of the connection was lost. A
# write that hits one of these MAY have taken effect on the server.
#
# Deliberately narrow. Connect errors and pool timeouts mean the request never
# left this process, and an API error carrying a status means the server
# answered — all are ordinary failures where nothing, or a known something,
# happened. Marking them 'unknown' would cry wolf on every unreachable host.
# redis TimeoutError means the command went out and the reply did not come
# back. redis ConnectionError is excluded: it also covers a connection that
# was never established.
_UNDETERMINED_ERRORS = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    redis.exceptions.TimeoutError,
)


# Long enough to carry the remediation sentence. These messages teach the
# caller what to do instead, and that clause comes last — a 300-char cap cut
# it off silently on every refusal long enough to need one.
_ERROR_MAX = 800


def _safe_error(exc: Exception, tool: str) -> str:
    """Return an agent-safe error string; log full detail server-side only."""
    logger.error("Tool %s failed", tool, exc_info=True)
    _passthrough = (
        ValueError,
        FileNotFoundError,
        KeyError,
        PermissionError,
        TimeoutError,
        ConnectionError,
        QueueApiError,
    )
    if isinstance(exc, _passthrough):
        return sanitize(str(exc), _ERROR_MAX)
    return f"{type(exc).__name__}: operation failed."


def tool_errors(shape: str = "dict") -> Callable:
    """Wrap a tool body in the canonical try/except → ``_safe_error`` pattern.

    Place this *between* ``@governed_tool`` and the function so the audit
    decorator and FastMCP still see the original signature.
    """

    def decorator(func: Callable) -> Callable:
        name = func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — sanitised below
                msg = _safe_error(e, name)
                if shape == "list":
                    return [{"error": msg, "hint": _DOCTOR_HINT}]
                if shape == "str":
                    return f"Error: {msg} {_DOCTOR_HINT}"
                payload = {"error": msg, "hint": _DOCTOR_HINT}
                # Flatten the exception into a dict and its type is gone
                # for good — so classify here, while it is still known,
                # whether the operation may nonetheless have taken effect.
                if isinstance(e, _UNDETERMINED_ERRORS):
                    return mark_unknown(payload)
                return payload

        return wrapper

    return decorator


mcp = FastMCP(
    "queue-aiops",
    instructions=(
        "Queue/cache broker operations over redis and rabbitmq: for a "
        "redis target — server/memory/keyspace reads, clients, slowlog, config, "
        "and a SCAN-budgeted big-key sample (never KEYS *); for a rabbitmq "
        "target — management-API overview, queues, connections, channels, "
        "policies, and node health. Flagship analyses: "
        "redis_memory_pressure_rca, redis_latency_rca, "
        "rabbitmq_queue_backlog_rca, connection_churn_analysis — transparent "
        "heuristics that show their numbers. Governed writes — config_set and "
        "set_policy/delete_policy (reversible, undo-recorded), declare_queue, "
        "kill_client (priorState only), plus purge_queue / delete_queue at "
        "risk=high with a dry_run preview and an approver (delete_queue's undo "
        "re-declares the captured definition; messages are not restored). Every "
        "tool runs through the queue-aiops governance harness (audit / budget / "
        "risk-tier / undo). A per-target 'platform' field selects the protocol "
        "shape; redis passwords are optional (lab instances), rabbitmq uses the "
        "management user's Basic auth."
    ),
)

_conn_mgr: Optional[ConnectionManager] = None


def _get_connection(target: Optional[str] = None) -> Any:
    """Return a broker connection, lazily initialising the manager."""
    global _conn_mgr  # noqa: PLW0603
    if _conn_mgr is None:
        config_path_str = os.environ.get("QUEUE_AIOPS_CONFIG")
        config_path = Path(config_path_str) if config_path_str else None
        _conn_mgr = ConnectionManager(load_config(config_path))
    return _conn_mgr.connect(target)
