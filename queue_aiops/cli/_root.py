"""Top-level Typer app: assembles sub-apps and top-level commands."""

from __future__ import annotations

import typer

from queue_aiops.cli._common import cli_errors
from queue_aiops.cli.analyze import analyze_app
from queue_aiops.cli.doctor import doctor_cmd
from queue_aiops.cli.init import init_cmd
from queue_aiops.cli.overview import overview_cmd
from queue_aiops.cli.rabbit_cmds import rabbitmq_app
from queue_aiops.cli.redis_cmds import redis_app
from queue_aiops.cli.secret import secret_app
from queue_aiops.cli.undo import undo_app

app = typer.Typer(
    name="queue-aiops",
    help="Governed AI-ops for redis + rabbitmq: memory/latency/backlog/churn "
    "RCAs, slowlog and big-key sampling, queues/policies, and governed writes "
    "(config set, client kill, purge/delete queue, policies).",
    no_args_is_help=True,
)

app.add_typer(redis_app, name="redis")
app.add_typer(rabbitmq_app, name="rabbitmq")
app.add_typer(analyze_app, name="analyze")
app.add_typer(secret_app, name="secret")
app.add_typer(undo_app, name="undo")
app.command("init")(init_cmd)
app.command("overview")(overview_cmd)
app.command("doctor")(doctor_cmd)


@app.command("mcp")
@cli_errors
def mcp_cmd() -> None:
    """Start the MCP server (stdio transport).

    Single-command entry point for MCP clients (does not go through uvx/PyPI
    resolution at launch):
        queue-aiops mcp
    """
    import sys

    if sys.version_info < (3, 11):
        typer.echo(
            f"ERROR: queue-aiops requires Python >= 3.11 "
            f"(got {sys.version_info.major}.{sys.version_info.minor}).\n"
            f"Fix: uv python install 3.12 && "
            f"uv tool install --python 3.12 --force queue-aiops",
            err=True,
        )
        raise typer.Exit(2)

    from mcp_server.server import main as _mcp_main

    _mcp_main()


if __name__ == "__main__":
    app()
