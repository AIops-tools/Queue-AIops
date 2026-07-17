"""``queue-aiops overview`` — one-shot broker health."""

from __future__ import annotations

import json

from queue_aiops.cli._common import TargetOption, cli_errors, console, get_connection


@cli_errors
def overview_cmd(target: TargetOption = None) -> None:
    """One-shot summary: platform + version + backlog/memory/client health."""
    from queue_aiops.ops import overview as ops

    conn, _ = get_connection(target)
    console.print_json(json.dumps(ops.queue_overview(conn)))
