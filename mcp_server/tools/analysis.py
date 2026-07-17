"""Flagship broker-analysis MCP tools (read-only)."""

from typing import Any, Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from queue_aiops.governance import governed_tool
from queue_aiops.ops import analysis as ops
from queue_aiops.ops._util import as_obj, num


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_memory_pressure_rca(
    used_pct: float = 85.0,
    telemetry: Optional[dict[str, Any]] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] Diagnose redis memory pressure → cause + action, with numbers.

    The flagship memory RCA: used vs maxmemory (+ eviction policy — noeviction
    near the limit means writes will OOM), evicted-keys pressure, fragmentation
    ratio (high = defrag, below 1 = likely swapping), and the SCAN-budgeted
    big-key sample. Every finding carries its numbers, not a black-box verdict.
    Pass 'telemetry' for pure analysis, or a target to pull live.

    Args:
        used_pct: used/maxmemory %% at/above which pressure is flagged (default 85).
        telemetry: Injected {memory:{...memory_stats fields}, evictedKeys,
            bigKeys:{topKeys:[...]}}; skips the live pull.
        target: redis target name from config; omit for the default.

    Returns dict: {pressure, usedPctOfMax, maxmemoryPolicy, fragmentationRatio,
        evictedKeys, thresholds, findings:[{cause, action, evidence}], note}.
    """
    if telemetry is None:
        telemetry = ops.pull_memory_telemetry(_get_connection(target))
    telemetry = as_obj(telemetry)
    return ops.redis_memory_pressure_rca(
        as_obj(telemetry.get("memory")),
        evicted_keys=num(telemetry.get("evictedKeys")),
        big_keys=as_obj(telemetry.get("bigKeys")),
        used_pct=used_pct,
    )


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def redis_latency_rca(
    slow_us: float = 10_000.0,
    telemetry: Optional[dict[str, Any]] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] Diagnose redis latency → slowlog digest + stall causes + actions.

    The flagship latency RCA: digests the SLOWLOG by command pattern (flagging
    O(N)/blocking commands), and reads stall signals from INFO — blocked
    clients, fork stalls (BGSAVE/AOF rewrite), delayed AOF fsyncs (slow disk),
    and dataset loading. Every finding carries its numbers. Pass 'telemetry'
    for pure analysis, or a target to pull live.

    Args:
        slow_us: Slowlog duration (microseconds) at/above which a pattern is
            flagged (default 10000 = 10ms).
        telemetry: Injected {slowlog:[{command, durationUs}], blockedClients,
            latestForkUsec, aofDelayedFsync, aofRewriteInProgress,
            rdbBgsaveInProgress, loading}; skips the live pull.
        target: redis target name from config; omit for the default.

    Returns dict: {slowlogPatterns, patternsOverThreshold, thresholds,
        findings:[{cause, action, evidence}], note}.
    """
    if telemetry is None:
        telemetry = ops.pull_latency_telemetry(_get_connection(target))
    return ops.redis_latency_rca(telemetry, slow_us=slow_us)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def rabbitmq_queue_backlog_rca(
    vhost: Optional[str] = None,
    top: int = 20,
    queues: Optional[list[dict[str, Any]]] = None,
    nodes: Optional[list[dict[str, Any]]] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] Rank backlogged queues and map each to a cause + action.

    The flagship backlog RCA: flags queues over the backlog threshold and
    classifies each — no consumers attached, unacked pileup (consumers not
    acking), publish rate outpacing delivery, or residual backlog — plus
    global memory/disk watermark alarms that block every publisher via flow
    control. Every finding carries its numbers. Pass 'queues' (and optionally
    'nodes') for pure analysis, or a target to pull live.

    Args:
        vhost: Restrict the live pull to one vhost; omit for all.
        top: How many queue rows to return, deepest first (default 20).
        queues: Injected rows {name, vhost, messages, messagesReady,
            messagesUnacked, consumers, publishRate, deliverRate, state}.
        nodes: Injected node_health rows {name, memAlarm, diskAlarm, ...}.
        target: rabbitmq target name from config; omit for the default.

    Returns dict: {queuesEvaluated, backloggedCount, globalFindings,
        queues:[{name, vhost, messages, consumers, cause, action, ...}],
        thresholds, note}.
    """
    if queues is None:
        pulled = ops.pull_backlog_telemetry(_get_connection(target), vhost)
        queues = pulled.get("queues", [])
        if nodes is None:
            nodes = pulled.get("nodes", [])
    return ops.rabbitmq_queue_backlog_rca(queues, nodes=nodes, top=top)


@mcp.tool()
@governed_tool(risk_level="low")
@tool_errors("dict")
def connection_churn_analysis(
    snapshot: Optional[dict[str, Any]] = None,
    history: Optional[dict[str, Any]] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] Connection/channel churn → cause + action; works on both platforms.

    The flagship churn analysis: for a redis target — new-connections rate vs
    steady clients (reconnect-per-operation smell) and rejected connections
    (maxclients); for a rabbitmq target — overview churn rates, channels-per-
    connection ratio (leak smell), and growth vs a prior snapshot. Both group
    clients by source so a finding can be pinned to an app. Call it once, keep
    the returned snapshot fields, and pass them back later as 'history' for
    delta-based churn. Pass 'snapshot' for pure analysis, or a target to pull
    live.

    Args:
        snapshot: Injected snapshot (from a prior call's live pull shape);
            skips the live pull.
        history: A prior snapshot for delta analysis (optional).
        target: Broker target name from config; omit for the default.

    Returns dict: {platform, metrics, bySource, comparedToHistory, thresholds,
        findings:[{cause, action, evidence}], note}.
    """
    if snapshot is None:
        snapshot = ops.pull_churn_snapshot(_get_connection(target))
    return ops.connection_churn_analysis(snapshot, history=history)
