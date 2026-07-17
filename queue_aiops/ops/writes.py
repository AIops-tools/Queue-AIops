"""Governed broker writes — the only state-changing operations in the tool.

Every reversible write reads the broker's current state **before** it changes
anything, so the harness records a faithful undo / audit trail (the before-state
is fetched via a real read, never guessed):

  * ``config_set`` (redis) — reads the parameter's current value via CONFIG GET
    first; undo sets it back.
  * ``set_policy`` / ``delete_policy`` (rabbitmq) — read the current policy
    first; undo restores the prior policy (or deletes a policy that did not
    exist before).
  * ``delete_queue`` (rabbitmq) — reads the queue's definition (durable /
    auto_delete / arguments) first; undo re-declares the queue with the
    captured properties. The *messages* in it are gone — the undo restores the
    queue definition, not its contents, and says so.

Irreversible writes record priorState only (no undo): ``kill_client`` (the
connection is gone; priorState = the client row) and ``purge_queue`` (messages
are destroyed; priorState = the message count).

Each function returns a plain descriptor; the MCP layer adds dry-run + the
governance harness (risk tier + audit + undo).
"""

from __future__ import annotations

import re
from typing import Any

from queue_aiops.connection import QueueApiError
from queue_aiops.ops._util import as_obj, num, s

# CONFIG SET parameter names are plain words (e.g. maxmemory-policy); reject
# anything else before it reaches the wire.
_CONFIG_PARAM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# ── redis: config_set (reversible) ───────────────────────────────────────────


def config_set(conn: Any, parameter: str, value: str) -> dict:
    """[WRITE][med] Set one redis config parameter, capturing its prior value.

    Reads the parameter first (CONFIG GET) so ``priorState.value`` reflects
    what it *was* (drives a faithful undo). Runtime-only — CONFIG SET does not
    persist to the config file unless CONFIG REWRITE is run out-of-band.
    """
    parameter = str(parameter).strip().lower()
    if not _CONFIG_PARAM_RE.match(parameter):
        raise ValueError(
            f"Invalid config parameter name '{parameter[:64]}': expected a "
            f"plain word like 'maxmemory-policy'."
        )
    prior = conn.redis_config_get(parameter)
    if parameter not in prior:
        raise KeyError(
            f"Config parameter '{parameter}' does not exist on this instance — "
            f"check the name with config_get first."
        )
    prior_value = s(prior.get(parameter), 128)
    conn.redis_config_set(parameter, str(value))
    return {
        "action": "config_set",
        "parameter": parameter,
        "value": s(value, 128),
        "priorState": {"value": prior_value},
        "note": "Runtime change only — not persisted to the config file.",
    }


# ── redis: kill_client (irreversible; priorState only) ──────────────────────


def kill_client(conn: Any, client_id: int = 0, addr: str = "") -> dict:
    """[WRITE][med] Disconnect one client by id or addr. IRREVERSIBLE.

    Captures the client's CLIENT LIST row as priorState before the kill (who it
    was, from where, last command) — the connection itself cannot be restored,
    so no undo is recorded. Most clients transparently reconnect.
    """
    if not client_id and not addr:
        raise ValueError("Pass client_id (from list_clients) or addr ('ip:port').")
    match: dict = {}
    for c in conn.redis_client_list():
        if (client_id and str(c.get("id")) == str(client_id)) or (
            addr and str(c.get("addr")) == str(addr)
        ):
            match = {
                "id": s(c.get("id"), 32),
                "addr": s(c.get("addr"), 64),
                "name": s(c.get("name"), 64),
                "lastCommand": s(c.get("cmd"), 64),
                "ageSeconds": num(c.get("age")),
            }
            break
    killed = (
        conn.redis_client_kill_id(int(client_id))
        if client_id
        else conn.redis_client_kill_addr(str(addr))
    )
    return {
        "action": "kill_client",
        "clientId": s(client_id, 32) if client_id else "",
        "addr": s(addr, 64),
        "killed": int(killed),
        "priorState": {"client": match},
    }


# ── rabbitmq: purge_queue (irreversible; priorState only) ────────────────────


def _queue_def(conn: Any, vhost: str, name: str) -> dict:
    """Fetch a queue's current definition + counts (raises if it is missing)."""
    obj = as_obj(
        conn.platform.normalise(conn.get(conn.platform.path("queue", vhost=vhost, name=name)))
    )
    return {
        "messages": num(obj.get("messages")),
        "durable": bool(obj.get("durable")),
        "autoDelete": bool(obj.get("auto_delete")),
        "arguments": as_obj(obj.get("arguments")),
    }


def purge_queue(conn: Any, vhost: str, name: str) -> dict:
    """[WRITE][high] Purge all ready messages from a queue. IRREVERSIBLE.

    Reads the queue first so priorState carries the message count that is about
    to be destroyed (audit evidence). Unacked messages are not purged.
    """
    prior = _queue_def(conn, vhost, name)
    conn.delete(conn.platform.path("queue_purge", vhost=vhost, name=name))
    return {
        "action": "purge_queue",
        "vhost": s(vhost, 64),
        "queue": s(name, 128),
        "priorState": {"messages": prior["messages"]},
        "note": "Messages are destroyed — no undo. Unacked messages are not purged.",
    }


# ── rabbitmq: delete/declare queue (definition-reversible) ───────────────────


def delete_queue(conn: Any, vhost: str, name: str) -> dict:
    """[WRITE][high] Delete a queue, capturing its definition for the undo.

    Reads the queue first so priorState carries the full definition (durable /
    auto_delete / arguments) and the message count. The undo re-declares the
    queue with the captured properties — the queue comes back, its messages do
    not (that is stated in the descriptor, not hidden).
    """
    prior = _queue_def(conn, vhost, name)
    conn.delete(conn.platform.path("queue", vhost=vhost, name=name))
    return {
        "action": "delete_queue",
        "vhost": s(vhost, 64),
        "queue": s(name, 128),
        "priorState": prior,
        "note": (
            "Undo re-declares the queue with the captured definition; the "
            f"{int(prior['messages'])} message(s) it held are NOT restored."
        ),
    }


def declare_queue(
    conn: Any,
    vhost: str,
    name: str,
    durable: bool = True,
    auto_delete: bool = False,
    arguments: dict | None = None,
) -> dict:
    """[WRITE][med] Declare (create) a queue via PUT; idempotent when identical.

    Reads first so priorState records whether the queue already existed. This
    is also the replay target for delete_queue's undo descriptor.
    """
    existed = True
    try:
        _queue_def(conn, vhost, name)
    except QueueApiError as exc:
        if exc.status_code != 404:
            raise
        existed = False
    body = {
        "durable": bool(durable),
        "auto_delete": bool(auto_delete),
        "arguments": as_obj(arguments),
    }
    conn.put(conn.platform.path("queue", vhost=vhost, name=name), json=body)
    return {
        "action": "declare_queue",
        "vhost": s(vhost, 64),
        "queue": s(name, 128),
        "durable": bool(durable),
        "autoDelete": bool(auto_delete),
        "arguments": as_obj(arguments),
        "priorState": {"existed": existed},
    }


# ── rabbitmq: policies (reversible) ──────────────────────────────────────────


def _policy_or_none(conn: Any, vhost: str, name: str) -> dict | None:
    """Fetch a policy's current definition; None when it does not exist."""
    try:
        obj = as_obj(
            conn.platform.normalise(
                conn.get(conn.platform.path("policy", vhost=vhost, name=name))
            )
        )
    except QueueApiError as exc:
        if exc.status_code == 404:
            return None
        raise
    return {
        "pattern": s(obj.get("pattern"), 128),
        "definition": as_obj(obj.get("definition")),
        "priority": num(obj.get("priority")),
        "applyTo": s(obj.get("apply-to") or obj.get("apply_to") or "all", 32),
    }


def set_policy(
    conn: Any,
    vhost: str,
    name: str,
    pattern: str,
    definition: dict,
    priority: int = 0,
    apply_to: str = "all",
) -> dict:
    """[WRITE][med] Create/update a policy, capturing the prior one for undo.

    Reads the policy first: if it existed, undo restores the prior fields; if
    it is new, undo deletes it.
    """
    if not isinstance(definition, dict) or not definition:
        raise ValueError(
            "Policy 'definition' must be a non-empty object, e.g. "
            "{'max-length': 100000} or {'message-ttl': 60000}."
        )
    prior = _policy_or_none(conn, vhost, name)
    body = {
        "pattern": str(pattern),
        "definition": definition,
        "priority": int(priority),
        "apply-to": str(apply_to),
    }
    conn.put(conn.platform.path("policy", vhost=vhost, name=name), json=body)
    return {
        "action": "set_policy",
        "vhost": s(vhost, 64),
        "policy": s(name, 128),
        "pattern": s(pattern, 128),
        "definition": as_obj(definition),
        "priority": int(priority),
        "applyTo": s(apply_to, 32),
        "priorState": {"existed": prior is not None, "policy": prior},
    }


def delete_policy(conn: Any, vhost: str, name: str) -> dict:
    """[WRITE][med] Delete a policy, capturing its definition for the undo.

    Reads the policy first so the undo can re-create it exactly.
    """
    prior = _policy_or_none(conn, vhost, name)
    if prior is None:
        raise KeyError(
            f"Policy '{name}' does not exist in vhost '{vhost}' — list_policies "
            f"shows what is defined."
        )
    conn.delete(conn.platform.path("policy", vhost=vhost, name=name))
    return {
        "action": "delete_policy",
        "vhost": s(vhost, 64),
        "policy": s(name, 128),
        "priorState": {"policy": prior},
    }
