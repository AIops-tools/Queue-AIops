"""``queue-aiops analyze`` — the flagship RCA analyses (read-only)."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from queue_aiops.cli._common import TargetOption, cli_errors, console, get_connection

analyze_app = typer.Typer(
    name="analyze",
    help="Flagship RCAs: redis memory pressure + latency, rabbitmq queue "
    "backlog, and connection churn (both platforms).",
    no_args_is_help=True,
)


@analyze_app.command("memory")
@cli_errors
def analyze_memory(
    used_pct: Annotated[
        float, typer.Option("--used-pct", help="used/maxmemory %% pressure threshold")
    ] = 85.0,
    target: TargetOption = None,
) -> None:
    """redis memory-pressure RCA: maxmemory, eviction, fragmentation, big keys."""
    from queue_aiops.ops import analysis as ops
    from queue_aiops.ops._util import as_obj, num

    conn, _ = get_connection(target)
    telemetry = ops.pull_memory_telemetry(conn)
    console.print_json(json.dumps(ops.redis_memory_pressure_rca(
        as_obj(telemetry.get("memory")),
        evicted_keys=num(telemetry.get("evictedKeys")),
        big_keys=as_obj(telemetry.get("bigKeys")),
        used_pct=used_pct,
    )))


@analyze_app.command("latency")
@cli_errors
def analyze_latency(
    slow_us: Annotated[
        float, typer.Option("--slow-us", help="Slowlog threshold in microseconds")
    ] = 10_000.0,
    target: TargetOption = None,
) -> None:
    """redis latency RCA: slowlog digest, blocked clients, fork/AOF stalls."""
    from queue_aiops.ops import analysis as ops

    conn, _ = get_connection(target)
    telemetry = ops.pull_latency_telemetry(conn)
    console.print_json(json.dumps(ops.redis_latency_rca(telemetry, slow_us=slow_us)))


@analyze_app.command("backlog")
@cli_errors
def analyze_backlog(
    vhost: Annotated[
        str | None, typer.Option("--vhost", help="Restrict to one vhost")
    ] = None,
    top: Annotated[int, typer.Option("--top", help="Queue rows, deepest first")] = 20,
    target: TargetOption = None,
) -> None:
    """rabbitmq queue-backlog RCA: consumers, unacked pileups, watermark blocks."""
    from queue_aiops.ops import analysis as ops

    conn, _ = get_connection(target)
    telemetry = ops.pull_backlog_telemetry(conn, vhost)
    console.print_json(json.dumps(ops.rabbitmq_queue_backlog_rca(
        telemetry.get("queues", []), nodes=telemetry.get("nodes", []), top=top,
    )))


@analyze_app.command("churn")
@cli_errors
def analyze_churn(target: TargetOption = None) -> None:
    """Connection-churn analysis (both platforms): counts, rates, by source."""
    from queue_aiops.ops import analysis as ops

    conn, _ = get_connection(target)
    snapshot = ops.pull_churn_snapshot(conn)
    console.print_json(json.dumps(ops.connection_churn_analysis(snapshot)))
