"""``queue-aiops redis`` — redis reads + governed writes."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from queue_aiops.cli._common import (
    DryRunOption,
    TargetOption,
    cli_errors,
    console,
    double_confirm,
    dry_run_preview,
    get_connection,
)

redis_app = typer.Typer(
    name="redis",
    help="redis: server/memory/clients/slowlog/config/keyspace/big-key reads, "
    "plus governed config-set and kill-client.",
    no_args_is_help=True,
)


@redis_app.command("info")
@cli_errors
def redis_info(target: TargetOption = None) -> None:
    """Server identity + health basics (version, role, clients, ops/sec)."""
    from queue_aiops.ops import redis_reads as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.server_info(conn)))


@redis_app.command("memory")
@cli_errors
def redis_memory(target: TargetOption = None) -> None:
    """Memory posture: used vs maxmemory, policy, fragmentation."""
    from queue_aiops.ops import redis_reads as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.memory_stats(conn)))


@redis_app.command("clients")
@cli_errors
def redis_clients(target: TargetOption = None) -> None:
    """Connected clients grouped by source address."""
    from queue_aiops.ops import redis_reads as ops

    conn, _ = get_connection(target)
    result = ops.list_clients(conn)
    console.print_json(json.dumps(result))
    if result.get("truncated"):
        console.print(
            f"[yellow]… truncated at {result.get('limit')} rows — "
            f"only the busiest clients are shown.[/yellow]"
        )


@redis_app.command("slowlog")
@cli_errors
def redis_slowlog(
    count: Annotated[int, typer.Option("--count", "-n", help="Max entries")] = 128,
    target: TargetOption = None,
) -> None:
    """Recent SLOWLOG entries, slowest first."""
    from queue_aiops.ops import redis_reads as ops

    conn, _ = get_connection(target)
    result = ops.slowlog(conn, count)
    console.print_json(json.dumps(result))
    if result.get("truncated"):
        console.print(
            f"[yellow]… truncated at {result.get('limit')} rows — "
            f"re-run with a higher --count to see the rest.[/yellow]"
        )


@redis_app.command("config-get")
@cli_errors
def redis_config_get(
    pattern: Annotated[str, typer.Argument(help="Config-name glob, e.g. 'maxmemory*'")] = "*",
    target: TargetOption = None,
) -> None:
    """CONFIG GET for a glob pattern."""
    from queue_aiops.ops import redis_reads as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.config_get(conn, pattern)))


@redis_app.command("keyspace")
@cli_errors
def redis_keyspace(target: TargetOption = None) -> None:
    """Per-db key counts and expiry coverage."""
    from queue_aiops.ops import redis_reads as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.keyspace(conn)))


@redis_app.command("bigkeys")
@cli_errors
def redis_bigkeys(
    top: Annotated[int, typer.Option("--top", help="Rows to return, largest first")] = 20,
    target: TargetOption = None,
) -> None:
    """SCAN-budgeted big-key sample (never KEYS *)."""
    from queue_aiops.ops import redis_reads as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.big_key_sample(conn, top)))


@redis_app.command("config-set")
@cli_errors
def redis_config_set(
    parameter: Annotated[str, typer.Argument(help="Config parameter, e.g. maxmemory-policy")],
    value: Annotated[str, typer.Argument(help="New value")],
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Set one config parameter (reversible — prior value captured for undo)."""
    from mcp_server.tools import writes as gov

    if dry_run:
        # Through the governed call: redis_config_set refuses the self-affecting
        # parameters, so a preview must report that rather than a green banner.
        dry_run_preview(
            gov.redis_config_set(parameter=parameter, value=value, dry_run=True,
                                 target=target),
            operation="redis_config_set", api_call="CONFIG SET",
            parameters={"parameter": parameter, "value": value})
        return
    double_confirm("set config parameter", parameter)
    console.print_json(
        json.dumps(gov.redis_config_set(parameter=parameter, value=value, target=target))
    )


@redis_app.command("kill-client")
@cli_errors
def redis_kill_client(
    client_id: Annotated[int, typer.Option("--id", help="Client id (from 'clients')")] = 0,
    addr: Annotated[str, typer.Option("--addr", help="Or the client's ip:port")] = "",
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Disconnect one client (irreversible; its info is captured as priorState)."""
    from mcp_server.tools import writes as gov

    who = str(client_id) if client_id else addr
    if dry_run:
        # Through the governed call: redis_kill_client refuses this tool's own
        # connection, so a preview must report that rather than a green banner.
        dry_run_preview(
            gov.redis_kill_client(client_id=client_id, addr=addr, dry_run=True,
                                  target=target),
            operation="redis_kill_client", api_call="CLIENT KILL",
            parameters={"client_id": client_id, "addr": addr})
        return
    double_confirm("kill client", who or "(unspecified)")
    console.print_json(
        json.dumps(gov.redis_kill_client(client_id=client_id, addr=addr, target=target))
    )
