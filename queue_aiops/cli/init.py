"""``queue-aiops init`` — a friendly, interactive onboarding wizard.

Walks a new user through connecting their first broker target: collects the
non-secret connection details into ``config.yaml`` and the secret into the
*encrypted* store (never plaintext on disk). For a redis target the password is
optional — an auth-less lab instance is a legitimate setup — while a rabbitmq
target always needs the management user's password. Designed to be run on a
terminal; everything it needs is prompted with sensible defaults.
"""

from __future__ import annotations

import getpass

import typer
import yaml

from queue_aiops.cli._common import cli_errors, console
from queue_aiops.config import CONFIG_DIR, CONFIG_FILE
from queue_aiops.governance.paths import ops_path
from queue_aiops.platform import RABBITMQ, REDIS, get_platform

# Starter policy: keeps the secure-by-default gate (high/critical writes need a
# named approver) explicit and editable, and shows the other rule kinds.
DEFAULT_RULES_YAML = """\
# queue-aiops policy rules — hot-reloaded on change (no restart needed).
# Kinds: deny rules, maintenance_window, risk_tiers (graduated autonomy).

risk_tiers:
  - name: high-risk-requires-approver
    tier: dual
    min_risk_level: high
    reason: >-
      High/critical writes need a named human approver — set
      QUEUE_AUDIT_APPROVED_BY (and QUEUE_AUDIT_RATIONALE) before the call.

# deny:
#   - name: no-prod-purges
#     operations: ["purge_queue", "delete_queue"]
#     environments: ["production"]
#     reason: "Destroying queued messages in production goes through change management."

# maintenance_window:
#   start: "22:00"
#   end: "06:00"
"""


def _write_default_rules() -> None:
    """Seed a starter rules.yaml (only when none exists) so the policy layer
    is explicit from day one; never overwrites an operator-authored file."""
    rules_path = ops_path("rules.yaml")
    if rules_path.exists():
        return
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(DEFAULT_RULES_YAML, "utf-8")
    console.print(f"[green]✓ Wrote default policy rules:[/] {rules_path}")


def _load_existing_targets() -> list[dict]:
    if not CONFIG_FILE.exists():
        return []
    raw = yaml.safe_load(CONFIG_FILE.read_text("utf-8")) or {}
    return list(raw.get("targets", []))


def _write_targets(targets: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    CONFIG_FILE.write_text(yaml.safe_dump({"targets": targets}, sort_keys=False), "utf-8")


@cli_errors
def init_cmd() -> None:
    """Interactively set up your first broker connection."""
    from queue_aiops.secretstore import SecretStore, resolve_master_password

    console.print("[bold cyan]Queue AIops — setup wizard[/]")
    console.print(
        "This collects redis or rabbitmq connection details (saved to "
        "config.yaml) and your secret (saved [bold]encrypted[/] to "
        "secrets.enc).\n"
    )

    console.print("[bold]Step 1 — master password[/]")
    console.print(
        "[dim]Encrypts secrets.enc. You'll set it via the "
        "QUEUE_AIOPS_MASTER_PASSWORD env var for non-interactive/MCP use.[/]"
    )
    password = resolve_master_password(confirm_if_new=True)
    store = SecretStore.unlock(password)

    targets = _load_existing_targets()
    existing_names = {t.get("name") for t in targets}

    while True:
        console.print("\n[bold]Step 2 — add a target[/]")
        name = typer.prompt("Target name (e.g. cache1 or broker1)").strip()
        if name in existing_names:
            if not typer.confirm(f"'{name}' already exists — overwrite?", default=False):
                continue
            targets = [t for t in targets if t.get("name") != name]

        platform = typer.prompt(
            f"Platform ({REDIS} / {RABBITMQ})", default=REDIS
        ).strip().lower()
        if platform not in (REDIS, RABBITMQ):
            console.print("[red]Platform must be 'redis' or 'rabbitmq'.[/]")
            continue

        host = typer.prompt("Host (IP or FQDN)").strip()
        port = typer.prompt("Port", default=get_platform(platform).default_port, type=int)
        db = 0
        if platform == REDIS:
            db = typer.prompt("Redis logical db index", default=0, type=int)
            use_tls = typer.confirm("Use TLS (rediss://)?", default=False)
        else:
            use_tls = typer.confirm("Use HTTPS for the management API?", default=False)
        verify_ssl = True
        if use_tls:
            console.print("[dim]Lab/self-signed setups can answer No here.[/]")
            verify_ssl = typer.confirm(
                "Verify TLS certificate? (No for self-signed lab certs)", default=True
            )

        username = ""
        if platform == RABBITMQ:
            username = typer.prompt("Management user (needs 'monitoring'/'management' tag)")
            username = username.strip()
            secret = getpass.getpass(f"Password for '{username}' on '{name}' (hidden): ")
            if not secret:
                console.print("[red]rabbitmq needs the management user's password.[/]")
                continue
            store = store.set(name, secret)
            secret_note = "secret encrypted"  # nosec B105 — a UI label, not a secret
        else:
            secret = getpass.getpass(
                f"Redis password for '{name}' (hidden; empty for no AUTH): "
            )
            if secret:
                store = store.set(name, secret)
                secret_note = "secret encrypted"  # nosec B105 — a UI label, not a secret
            else:
                secret_note = "no AUTH — lab mode"  # nosec B105 — a UI label, not a secret

        entry = {
            "name": name,
            "platform": platform,
            "host": host,
            "port": port,
            "username": username,
            "db": db,
            "use_tls": use_tls,
            "verify_ssl": verify_ssl,
        }
        targets.append(entry)
        existing_names.add(name)
        _write_targets(targets)
        console.print(f"[green]✓ Saved target '{name}' ({platform}, {secret_note}).[/]")

        if not typer.confirm("\nAdd another target?", default=False):
            break

    _write_default_rules()
    console.print(f"\n[green]✓ Setup complete.[/] Config: {CONFIG_FILE}")
    console.print(
        "[dim]Tip: export QUEUE_AIOPS_MASTER_PASSWORD=... in your shell profile "
        "so the MCP server and CLI can unlock secrets non-interactively.[/]"
    )
    if typer.confirm("Run a connectivity check now (queue-aiops doctor)?", default=True):
        from queue_aiops.doctor import run_doctor

        raise typer.Exit(run_doctor())
