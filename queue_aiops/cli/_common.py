"""Shared helpers for queue-aiops CLI sub-modules."""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

console = Console()

# ─── Shared Option types ───────────────────────────────────────────────────

TargetOption = Annotated[
    str | None, typer.Option("--target", "-t", help="Target name from config")
]
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Print the API call without executing")
]


def _cli_error_types() -> tuple[type[BaseException], ...]:
    """Exceptions translated to a one-line teaching error instead of a traceback.

    ``BudgetExceeded`` (and the retained ``PolicyDenied`` type) are raised by
    ``@governed_tool`` OUTSIDE the tool body, so ``tool_errors`` never sees them
    and they never arrive as an ``{"error": ...}`` dict. Their message is the
    teaching text (e.g. which budget was hit) — without them here such a refusal
    reaches the CLI as a traceback instead.
    """
    from queue_aiops.connection import QueueApiError
    from queue_aiops.governance import BudgetExceeded, PolicyDenied

    return (QueueApiError, PolicyDenied, BudgetExceeded, KeyError, OSError, ValueError)


def cli_errors(fn: Callable) -> Callable:
    """Translate known exceptions into one red line + exit code 1."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except _cli_error_types() as e:
            message = str(e)
            if isinstance(e, KeyError):
                message = f"Missing required key or environment variable: {message}"
            console.print(f"[red]Error: {message}[/]")
            raise typer.Exit(1) from e

    return wrapper


def get_connection(target: str | None, config_path: Path | None = None) -> tuple[Any, Any]:
    """Return a (conn, config) tuple for the given target."""
    from queue_aiops.config import load_config
    from queue_aiops.connection import ConnectionManager

    cfg = load_config(config_path)
    mgr = ConnectionManager(cfg)
    return mgr.connect(target), cfg


def dry_run_print(*, operation: str, api_call: str, parameters: dict | None = None) -> None:
    """Print a dry-run preview of the API call that would be made."""
    console.print("\n[bold magenta][DRY-RUN] No changes will be made.[/]")
    console.print(f"[magenta]  Operation: {operation}[/]")
    console.print(f"[magenta]  API Call:  {api_call}[/]")
    for k, v in (parameters or {}).items():
        console.print(f"[magenta]  Param:     {k} = {v}[/]")
    console.print("[magenta]  Run without --dry-run to execute.[/]\n")


def dry_run_preview(
    preview: Any, *, operation: str, api_call: str, parameters: dict | None = None
) -> None:
    """Render a GOVERNED dry-run result as the human-readable DRY-RUN banner.

    ``preview`` must come from calling the governed tool with ``dry_run=True``,
    so every guard it carries has already run against the real target. A refusal
    arrives as ``{"error": ...}`` (``tool_errors`` flattens the exception) — it is
    printed like any other CLI error and exits non-zero, exactly as the real
    write would. Printing a green banner for a call that is about to be refused
    is the preview being wrong, not merely incomplete.

    On the allowed path the banner is byte-for-byte what it always was: routing
    through the governed call buys the guard and the audit row, not a new
    serialization.
    """
    if isinstance(preview, dict) and preview.get("error"):
        console.print(f"[red]Error: {preview['error']}[/]")
        raise typer.Exit(1)
    dry_run_print(operation=operation, api_call=api_call, parameters=parameters)


def double_confirm(action: str, resource: str) -> None:
    """Require two confirmations for a destructive operation."""
    console.print(f"[bold yellow]⚠️  About to: {action} '{resource}'[/]")
    typer.confirm(f"Confirm 1/2: {action} '{resource}'?", abort=True)
    typer.confirm(
        f"Confirm 2/2: really {action} '{resource}'? This may be irreversible.",
        abort=True,
    )
