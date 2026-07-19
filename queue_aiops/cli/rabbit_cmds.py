"""``queue-aiops rabbitmq`` — rabbitmq reads + governed writes."""

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
    dry_run_print,
    get_connection,
)

rabbitmq_app = typer.Typer(
    name="rabbitmq",
    help="rabbitmq: overview/queues/connections/channels/policies/nodes reads, "
    "plus governed purge/delete/declare queue and set/delete policy.",
    no_args_is_help=True,
)

VhostOption = Annotated[
    str, typer.Option("--vhost", help="Vhost (the default vhost is '/')")
]


@rabbitmq_app.command("overview")
@cli_errors
def rabbit_overview(target: TargetOption = None) -> None:
    """Broker identity + totals (queues, messages, connections, rates)."""
    from queue_aiops.ops import rabbit_reads as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.broker_overview(conn)))


@rabbitmq_app.command("queues")
@cli_errors
def rabbit_queues(
    vhost: Annotated[
        str | None, typer.Option("--vhost", help="Restrict to one vhost")
    ] = None,
    target: TargetOption = None,
) -> None:
    """Queues, deepest backlog first."""
    from queue_aiops.ops import rabbit_reads as ops

    conn, _ = get_connection(target)
    result = ops.list_queues(conn, vhost)
    console.print_json(json.dumps(result))
    if result.get("truncated"):
        console.print(
            f"[yellow]… truncated at {result.get('limit')} rows — "
            f"re-run with a higher limit to see the rest.[/yellow]"
        )


@rabbitmq_app.command("queue")
@cli_errors
def rabbit_queue(
    name: Annotated[str, typer.Argument(help="Queue name (from 'queues')")],
    vhost: VhostOption = "/",
    target: TargetOption = None,
) -> None:
    """One queue's full detail."""
    from queue_aiops.ops import rabbit_reads as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.queue_detail(conn, vhost, name)))


@rabbitmq_app.command("connections")
@cli_errors
def rabbit_connections(target: TargetOption = None) -> None:
    """Client connections grouped by peer host."""
    from queue_aiops.ops import rabbit_reads as ops

    conn, _ = get_connection(target)
    result = ops.list_connections(conn)
    console.print_json(json.dumps(result))
    if result.get("truncated"):
        console.print(
            f"[yellow]… truncated at {result.get('limit')} rows — "
            f"re-run with a higher limit to see the rest.[/yellow]"
        )


@rabbitmq_app.command("channels")
@cli_errors
def rabbit_channels(target: TargetOption = None) -> None:
    """Channels with unacked/prefetch pressure."""
    from queue_aiops.ops import rabbit_reads as ops

    conn, _ = get_connection(target)
    result = ops.list_channels(conn)
    console.print_json(json.dumps(result))
    if result.get("truncated"):
        console.print(
            f"[yellow]… truncated at {result.get('limit')} rows — "
            f"re-run with a higher limit to see the rest.[/yellow]"
        )


@rabbitmq_app.command("policies")
@cli_errors
def rabbit_policies(
    vhost: Annotated[
        str | None, typer.Option("--vhost", help="Restrict to one vhost")
    ] = None,
    target: TargetOption = None,
) -> None:
    """Policies with pattern, priority, definition."""
    from queue_aiops.ops import rabbit_reads as ops

    conn, _ = get_connection(target)
    result = ops.list_policies(conn, vhost)
    console.print_json(json.dumps(result))
    if result.get("truncated"):
        console.print(
            f"[yellow]… truncated at {result.get('limit')} rows — "
            f"re-run with a higher limit to see the rest.[/yellow]"
        )


@rabbitmq_app.command("nodes")
@cli_errors
def rabbit_nodes(target: TargetOption = None) -> None:
    """Node memory/disk/fd posture + watermark alarms."""
    from queue_aiops.ops import rabbit_reads as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.node_health(conn)))


@rabbitmq_app.command("purge")
@cli_errors
def rabbit_purge(
    name: Annotated[str, typer.Argument(help="Queue name to purge")],
    vhost: VhostOption = "/",
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Purge all ready messages from a queue (IRREVERSIBLE, risk=high)."""
    from mcp_server.tools import writes as gov

    if dry_run:
        dry_run_print(operation="purge_queue", api_call="DELETE queue contents",
                      parameters={"vhost": vhost, "name": name})
        return
    double_confirm("purge queue", name)
    console.print_json(json.dumps(gov.purge_queue(vhost=vhost, name=name, target=target)))


@rabbitmq_app.command("delete-queue")
@cli_errors
def rabbit_delete_queue(
    name: Annotated[str, typer.Argument(help="Queue name to delete")],
    vhost: VhostOption = "/",
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Delete a queue (risk=high; undo re-declares the captured definition)."""
    from mcp_server.tools import writes as gov

    if dry_run:
        dry_run_print(operation="delete_queue", api_call="DELETE queue",
                      parameters={"vhost": vhost, "name": name})
        return
    double_confirm("delete queue", name)
    console.print_json(json.dumps(gov.delete_queue(vhost=vhost, name=name, target=target)))


@rabbitmq_app.command("declare-queue")
@cli_errors
def rabbit_declare_queue(
    name: Annotated[str, typer.Argument(help="Queue name to declare")],
    vhost: VhostOption = "/",
    durable: Annotated[
        bool, typer.Option("--durable/--transient", help="Survive broker restarts")
    ] = True,
    auto_delete: Annotated[
        bool, typer.Option("--auto-delete", help="Delete when the last consumer leaves")
    ] = False,
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Declare (create) a queue (undo deletes it when newly created)."""
    from mcp_server.tools import writes as gov

    if dry_run:
        dry_run_print(operation="declare_queue", api_call="PUT queue",
                      parameters={"vhost": vhost, "name": name, "durable": durable,
                                  "auto_delete": auto_delete})
        return
    double_confirm("declare queue", name)
    console.print_json(json.dumps(gov.declare_queue(
        vhost=vhost, name=name, durable=durable, auto_delete=auto_delete, target=target
    )))


@rabbitmq_app.command("set-policy")
@cli_errors
def rabbit_set_policy(
    name: Annotated[str, typer.Argument(help="Policy name")],
    pattern: Annotated[str, typer.Argument(help="Regex the policy matches names against")],
    definition: Annotated[
        str, typer.Argument(help="Definition as JSON, e.g. '{\"max-length\": 100000}'")
    ],
    vhost: VhostOption = "/",
    priority: Annotated[int, typer.Option("--priority", help="Higher wins")] = 0,
    apply_to: Annotated[
        str, typer.Option("--apply-to", help="queues / exchanges / all")
    ] = "all",
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Create/update a policy (reversible — prior policy captured for undo)."""
    from mcp_server.tools import writes as gov

    try:
        definition_obj = json.loads(definition)
    except json.JSONDecodeError as exc:
        raise ValueError(f"'definition' must be valid JSON: {exc}") from exc
    if dry_run:
        dry_run_print(operation="set_policy", api_call="PUT policy",
                      parameters={"vhost": vhost, "name": name, "pattern": pattern,
                                  "definition": definition_obj, "priority": priority,
                                  "apply_to": apply_to})
        return
    double_confirm("set policy", name)
    console.print_json(json.dumps(gov.set_policy(
        vhost=vhost, name=name, pattern=pattern, definition=definition_obj,
        priority=priority, apply_to=apply_to, target=target,
    )))


@rabbitmq_app.command("delete-policy")
@cli_errors
def rabbit_delete_policy(
    name: Annotated[str, typer.Argument(help="Policy name to delete")],
    vhost: VhostOption = "/",
    target: TargetOption = None,
    dry_run: DryRunOption = False,
) -> None:
    """Delete a policy (reversible — its definition is captured for undo)."""
    from mcp_server.tools import writes as gov

    if dry_run:
        dry_run_print(operation="delete_policy", api_call="DELETE policy",
                      parameters={"vhost": vhost, "name": name})
        return
    double_confirm("delete policy", name)
    console.print_json(json.dumps(gov.delete_policy(vhost=vhost, name=name, target=target)))
