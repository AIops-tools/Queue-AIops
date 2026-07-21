"""Governed broker-write MCP tools (the only state-changing tools).

Every tool is wrapped with the governance harness (audit + descriptive risk
tier) and takes a ``dry_run`` preview. Reversible writes pass an ``undo=``
callback that turns the fetched before-state into an inverse descriptor the
harness records; irreversible ones (kill_client, purge_queue) record only the
priorState in the result.

Risk tiers: purge_queue / delete_queue = high (destroy messages / a queue);
redis_config_set / redis_kill_client / declare_queue / set_policy /
delete_policy = medium.
"""

from typing import Any, Optional

from mcp_server._shared import _get_connection, mcp, tool_errors
from queue_aiops.governance import governed_tool
from queue_aiops.ops import writes as ops

# ── undo descriptors (built from the fetched before-state) ──────────────────


def _config_set_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of redis_config_set: set the parameter back to its prior value."""
    if not isinstance(result, dict):
        return None
    prior = (result.get("priorState") or {}).get("value")
    if prior is None:
        return None
    return {
        "tool": "redis_config_set",
        "params": {"parameter": params.get("parameter"), "value": prior},
        "skill": "queue-aiops",
        "note": "Inverse of redis_config_set: restore the parameter's prior value.",
    }


def _delete_queue_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of delete_queue: re-declare the queue with the captured definition.

    The queue's *messages* are gone — this restores the definition only, and the
    note says so.
    """
    if not isinstance(result, dict):
        return None
    prior = result.get("priorState") or {}
    return {
        "tool": "declare_queue",
        "params": {
            "vhost": params.get("vhost"),
            "name": params.get("name"),
            "durable": bool(prior.get("durable", True)),
            "auto_delete": bool(prior.get("autoDelete", False)),
            "arguments": prior.get("arguments") or {},
        },
        "skill": "queue-aiops",
        "note": "Inverse of delete_queue: re-declare the captured definition "
        "(messages are NOT restored).",
    }


def _declare_queue_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of declare_queue: delete the queue — only when it was newly created."""
    if not isinstance(result, dict):
        return None
    if (result.get("priorState") or {}).get("existed"):
        return None  # the queue predates this call; deleting it would not be an undo
    return {
        "tool": "delete_queue",
        "params": {"vhost": params.get("vhost"), "name": params.get("name")},
        "skill": "queue-aiops",
        "note": "Inverse of declare_queue: delete the queue that was created.",
    }


def _set_policy_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of set_policy: restore the prior policy, or delete it if it is new."""
    if not isinstance(result, dict):
        return None
    prior_state = result.get("priorState") or {}
    prior = prior_state.get("policy")
    if prior_state.get("existed") and isinstance(prior, dict):
        return {
            "tool": "set_policy",
            "params": {
                "vhost": params.get("vhost"),
                "name": params.get("name"),
                "pattern": prior.get("pattern"),
                "definition": prior.get("definition") or {},
                "priority": int(prior.get("priority") or 0),
                "apply_to": prior.get("applyTo") or "all",
            },
            "skill": "queue-aiops",
            "note": "Inverse of set_policy: restore the prior policy fields.",
        }
    return {
        "tool": "delete_policy",
        "params": {"vhost": params.get("vhost"), "name": params.get("name")},
        "skill": "queue-aiops",
        "note": "Inverse of set_policy: delete the policy that was newly created.",
    }


def _delete_policy_undo(params: dict[str, Any], result: Any) -> Optional[dict]:
    """Inverse of delete_policy: re-create the captured policy."""
    if not isinstance(result, dict):
        return None
    prior = (result.get("priorState") or {}).get("policy")
    if not isinstance(prior, dict):
        return None
    return {
        "tool": "set_policy",
        "params": {
            "vhost": params.get("vhost"),
            "name": params.get("name"),
            "pattern": prior.get("pattern"),
            "definition": prior.get("definition") or {},
            "priority": int(prior.get("priority") or 0),
            "apply_to": prior.get("applyTo") or "all",
        },
        "skill": "queue-aiops",
        "note": "Inverse of delete_policy: re-create the captured policy.",
    }


# ── redis writes ─────────────────────────────────────────────────────────────


@mcp.tool()
@governed_tool(risk_level="medium", undo=_config_set_undo)
@tool_errors("dict")
def redis_config_set(
    parameter: str,
    value: str,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE][risk=medium] Set one redis config parameter; reversible.

    Reads the parameter first (CONFIG GET) so the harness records an undo that
    restores its prior value. Runtime-only — not persisted to the config file.
    Pass dry_run=True to preview.

    Refuses the parameters that would lock this tool out of the instance
    (requirepass, masterauth, bind, port, protected-mode, maxclients,
    unixsocket, aclfile, tls-*) — the undo could never be replayed over a
    connection those settings break. Change those in redis.conf and restart.
    The refusal applies under dry_run too: a preview whose real call would be
    refused must report that, not a green 'wouldSet'.

    Args:
        parameter: Config parameter name (e.g. maxmemory-policy), from redis_config_get.
        value: New value.
        dry_run: If True, preview without changing.
        target: redis target name from config; omit for the default.
    """
    conn = _get_connection(target)
    # Ahead of the dry_run return: a preview whose real call would be refused
    # must say so, or the caller reads the refusal as transient and retries.
    ops.guard_config_set(parameter)
    if dry_run:
        return {"dryRun": True, "wouldSet": {"parameter": parameter, "value": value}}
    return ops.config_set(conn, parameter, value)


@mcp.tool()
@governed_tool(risk_level="medium")
@tool_errors("dict")
def redis_kill_client(
    client_id: int = 0,
    addr: str = "",
    dry_run: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE][risk=medium] Disconnect one redis client by id or addr. IRREVERSIBLE.

    Captures the client's CLIENT LIST row as priorState (who/where/last
    command) before the kill — the connection cannot be restored, so no undo
    is recorded; most clients transparently reconnect. Pass dry_run=True to
    preview.

    Refuses this tool's own connection, by id or by addr — including under
    dry_run, which must report a refusal rather than preview a call that will be
    refused.

    Args:
        client_id: Client id from redis_clients (preferred).
        addr: Or the client's 'ip:port' address.
        dry_run: If True, preview without disconnecting.
        target: redis target name from config; omit for the default.
    """
    conn = _get_connection(target)
    # Ahead of the dry_run return: a preview whose real call would be refused
    # must say so, or the caller reads the refusal as transient and retries.
    ops.guard_kill_client(conn, client_id=client_id, addr=addr)
    if dry_run:
        return {"dryRun": True, "wouldKill": {"clientId": client_id, "addr": addr}}
    return ops.kill_client(conn, client_id=client_id, addr=addr)


# ── rabbitmq queue writes ────────────────────────────────────────────────────


@mcp.tool()
@governed_tool(risk_level="high")
@tool_errors("dict")
def purge_queue(
    vhost: str,
    name: str,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE][risk=high] Purge all ready messages from a rabbitmq queue. IRREVERSIBLE.

    Reads the queue first so priorState carries the message count about to be
    destroyed (audit evidence); unacked messages are not purged. No undo —
    purged messages cannot be restored. Pass dry_run=True to preview.

    Args:
        vhost: The queue's vhost (the default vhost is '/').
        name: Queue name (from list_queues).
        dry_run: If True, preview without purging.
        target: rabbitmq target name from config; omit for the default.
    """
    conn = _get_connection(target)
    if dry_run:
        return {"dryRun": True, "wouldPurge": {"vhost": vhost, "queue": name}}
    return ops.purge_queue(conn, vhost, name)


@mcp.tool()
@governed_tool(risk_level="high", undo=_delete_queue_undo)
@tool_errors("dict")
def delete_queue(
    vhost: str,
    name: str,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE][risk=high] Delete a rabbitmq queue; the undo re-declares its definition.

    Reads the queue first so priorState carries the definition (durable /
    auto_delete / arguments) and message count; the recorded undo re-declares
    the queue with those properties — its messages are NOT restored. Pass
    dry_run=True to preview.

    Args:
        vhost: The queue's vhost (the default vhost is '/').
        name: Queue name (from list_queues).
        dry_run: If True, preview without deleting.
        target: rabbitmq target name from config; omit for the default.
    """
    conn = _get_connection(target)
    if dry_run:
        return {"dryRun": True, "wouldDelete": {"vhost": vhost, "queue": name}}
    return ops.delete_queue(conn, vhost, name)


@mcp.tool()
@governed_tool(risk_level="medium", undo=_declare_queue_undo)
@tool_errors("dict")
def declare_queue(
    vhost: str,
    name: str,
    durable: bool = True,
    auto_delete: bool = False,
    arguments: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE][risk=medium] Declare (create) a rabbitmq queue; undo deletes it if new.

    Records whether the queue already existed; the undo (delete) is recorded
    only for a newly-created queue. Also the replay target for delete_queue's
    undo. Pass dry_run=True to preview.

    Args:
        vhost: The queue's vhost (the default vhost is '/').
        name: Queue name to declare.
        durable: Survive broker restarts (default True).
        auto_delete: Delete when the last consumer disconnects (default False).
        arguments: Optional x-arguments (e.g. {'x-queue-type': 'quorum'}).
        dry_run: If True, preview without declaring.
        target: rabbitmq target name from config; omit for the default.
    """
    conn = _get_connection(target)
    if dry_run:
        return {
            "dryRun": True,
            "wouldDeclare": {
                "vhost": vhost, "queue": name, "durable": durable,
                "autoDelete": auto_delete, "arguments": arguments or {},
            },
        }
    return ops.declare_queue(
        conn, vhost, name, durable=durable, auto_delete=auto_delete, arguments=arguments
    )


# ── rabbitmq policy writes ───────────────────────────────────────────────────


@mcp.tool()
@governed_tool(risk_level="medium", undo=_set_policy_undo)
@tool_errors("dict")
def set_policy(
    vhost: str,
    name: str,
    pattern: str,
    definition: dict[str, Any],
    priority: int = 0,
    apply_to: str = "all",
    dry_run: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE][risk=medium] Create/update a rabbitmq policy; reversible.

    Reads the policy first: the recorded undo restores the prior policy fields,
    or deletes the policy when it is newly created. Pass dry_run=True to
    preview.

    Args:
        vhost: The policy's vhost (the default vhost is '/').
        name: Policy name.
        pattern: Regex the policy matches queue/exchange names against.
        definition: Policy definition object (e.g. {'max-length': 100000}).
        priority: Policy priority (higher wins; default 0).
        apply_to: 'queues', 'exchanges', or 'all' (default).
        dry_run: If True, preview without changing.
        target: rabbitmq target name from config; omit for the default.
    """
    conn = _get_connection(target)
    if dry_run:
        return {
            "dryRun": True,
            "wouldSetPolicy": {
                "vhost": vhost, "policy": name, "pattern": pattern,
                "definition": definition, "priority": priority, "applyTo": apply_to,
            },
        }
    return ops.set_policy(
        conn, vhost, name, pattern, definition, priority=priority, apply_to=apply_to
    )


@mcp.tool()
@governed_tool(risk_level="medium", undo=_delete_policy_undo)
@tool_errors("dict")
def delete_policy(
    vhost: str,
    name: str,
    dry_run: bool = False,
    target: Optional[str] = None,
) -> dict:
    """[WRITE][risk=medium] Delete a rabbitmq policy; the undo re-creates it.

    Reads the policy first so the recorded undo can re-create it exactly. Pass
    dry_run=True to preview.

    Args:
        vhost: The policy's vhost (the default vhost is '/').
        name: Policy name (from list_policies).
        dry_run: If True, preview without deleting.
        target: rabbitmq target name from config; omit for the default.
    """
    conn = _get_connection(target)
    if dry_run:
        return {"dryRun": True, "wouldDeletePolicy": {"vhost": vhost, "policy": name}}
    return ops.delete_policy(conn, vhost, name)
