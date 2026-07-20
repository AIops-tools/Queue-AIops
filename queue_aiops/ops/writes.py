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

``kill_client`` additionally refuses this tool's OWN connection, by id or by
addr (:class:`SelfLockout`). A kill has no undo to begin with, so aiming one at
the calling connection is the purest form of an operation destroying its own
reversibility. ``redis_reads.list_clients`` now hides that connection too — the
sibling DBA tools have always filtered themselves out of their activity reads
(``pg_backend_pid()`` / ``CONNECTION_ID()``), and listing it handed an agent
hunting "the idle client" its own id.

``config_set`` additionally refuses a static denylist of self-affecting
parameters (:class:`SelfLockout`) — ``requirepass`` and friends re-key or move
the connection this tool depends on, so the undo it records could never be
replayed. The refusal is ordered ahead of the prior-value read so a plaintext
credential is never captured into audit.db or the undo store.

Each function returns a plain descriptor; the MCP layer adds dry-run + the
governance harness (risk tier + audit + undo).
"""

from __future__ import annotations

import re
from typing import Any

from queue_aiops.connection import QueueApiError
from queue_aiops.ops._util import as_obj, num, opt, s

# CONFIG SET parameter names are plain words (e.g. maxmemory-policy); reject
# anything else before it reaches the wire.
_CONFIG_PARAM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# CONFIG SET parameters that would cut this tool off from the very instance it
# is managing — each either re-keys the credential we authenticate with or moves
# the socket we are connected over. Setting one leaves the recorded undo needing
# a connection that can no longer be made, i.e. the write destroys its own
# reversibility. The list is STATIC (no runtime detection), so there is no
# fail-open case: a name is either on it or it is not.
_SELF_AFFECTING_PARAMS: dict[str, str] = {
    "requirepass": "it invalidates the credential this connection authenticates with",
    "masterauth": "it re-keys the credential this instance uses to reach its master",
    "bind": "it can drop the interface this connection arrived on",
    "protected-mode": "it can start refusing every non-loopback client, this one included",
    "maxclients": "it can be set below the live connection count, evicting this one",
    "port": "it moves the listener out from under this connection",
    "unixsocket": "it moves the socket out from under a socket-connected client",
    "aclfile": "it repoints the whole ACL set, this tool's own user included",
}
_SELF_AFFECTING_PREFIX = "tls-"


class SelfLockout(ValueError):  # noqa: N818 — teaching error, reads as a statement
    """Refused: the operation would cut this tool off from the broker it manages."""


def _normalise_param(parameter: str) -> str:
    return str(parameter).strip().lower()


def _self_affecting_reason(parameter: str) -> str | None:
    """Why ``parameter`` would lock this tool out, or None if it is safe to set."""
    reason = _SELF_AFFECTING_PARAMS.get(parameter)
    if reason is not None:
        return reason
    if parameter.startswith(_SELF_AFFECTING_PREFIX):
        return "it changes the TLS terms of this very connection"
    return None


def guard_config_set(parameter: str) -> None:
    """Raise the :class:`SelfLockout` ``config_set`` would raise, without any I/O.

    Called by ``config_set`` itself *and* by the MCP wrapper ahead of its
    ``dry_run`` early return, so a preview of a denylisted parameter reports the
    refusal instead of a green ``wouldSet``. The denylist is static, so the
    preview and the real call cannot diverge and the guard costs nothing.

    Normalises the name itself, so it cannot be side-stepped by case or padding
    on either path.
    """
    lockout_reason = _self_affecting_reason(_normalise_param(parameter))
    if lockout_reason is None:
        return
    raise SelfLockout(
        f"Refusing to CONFIG SET '{_normalise_param(parameter)}': {lockout_reason}, "
        f"destroying this write's own reversibility (the undo could not replay). "
        f"Change it in redis.conf and restart, then re-store the credential with "
        f"'queue-aiops secret set <target>'."
    )


# ── redis: config_set (reversible) ───────────────────────────────────────────


def config_set(conn: Any, parameter: str, value: str) -> dict:
    """[WRITE][med] Set one redis config parameter, capturing its prior value.

    Reads the parameter first (CONFIG GET) so ``priorState.value`` reflects
    what it *was* (drives a faithful undo). Runtime-only — CONFIG SET does not
    persist to the config file unless CONFIG REWRITE is run out-of-band.

    **Refuses the parameters that would lock this tool out of the instance**
    (``requirepass``, ``bind``, ``port``, ``protected-mode``, ``maxclients``,
    ``masterauth``, ``unixsocket``, ``aclfile``, ``tls-*``). The refusal happens
    *before* the prior value is read, which matters twice over: for
    ``requirepass`` / ``masterauth`` the prior value IS a plaintext credential,
    and capturing it would write that secret into audit.db and the undo store.
    """
    parameter = _normalise_param(parameter)
    if not _CONFIG_PARAM_RE.match(parameter):
        raise ValueError(
            f"Invalid config parameter name '{parameter[:64]}': expected a "
            f"plain word like 'maxmemory-policy'."
        )
    guard_config_set(parameter)
    prior = conn.redis_config_get(parameter)
    if parameter not in prior:
        raise KeyError(
            f"Config parameter '{parameter}' does not exist on this instance — "
            f"check the name with config_get first."
        )
    prior_value = opt(prior.get(parameter), 128)
    conn.redis_config_set(parameter, str(value))
    return {
        "action": "config_set",
        "parameter": parameter,
        "value": s(value, 128),
        "priorState": {"value": prior_value},
        "note": "Runtime change only — not persisted to the config file.",
    }


# ── redis: kill_client (irreversible; priorState only) ──────────────────────


def _targets_own_connection(conn: Any, client_id: int, addr: str) -> bool:
    """Whether (client_id, addr) resolves to this tool's own redis connection.

    One probe (CLIENT ID) covers both selectors: the id is compared directly,
    and ``addr`` is resolved through the CLIENT LIST row carrying that id — so
    an agent cannot side-step the guard by passing its own ip:port instead of
    its own id. Returns False when the identity cannot be determined, because
    unknown must never read as "it is me".
    """
    own_id = conn.redis_client_id() if hasattr(conn, "redis_client_id") else None
    if own_id is None:
        return False
    if client_id and str(client_id) == str(own_id):
        return True
    if not addr:
        return False
    return any(
        str(c.get("id")) == str(own_id) and str(c.get("addr")) == str(addr)
        for c in conn.redis_client_list()
    )


def guard_kill_client(conn: Any, client_id: int = 0, addr: str = "") -> None:
    """Raise the :class:`SelfLockout` ``kill_client`` would raise, without killing.

    Called by ``kill_client`` itself *and* by the MCP wrapper ahead of its
    ``dry_run`` early return, so a preview of a self-kill reports the refusal
    instead of a green ``wouldKill``. Both paths run this one function, so the
    preview and the real call can never disagree.

    Fails open on an undeterminable client id: unknown is never "it is me".
    """
    if not _targets_own_connection(conn, client_id, addr):
        return
    who = str(client_id) if client_id else addr
    raise SelfLockout(
        f"Refusing to kill client '{who}': that is the connection this tool is "
        f"calling through. CLIENT KILL has no undo, so disconnecting yourself "
        f"ends the session the audit row is written from and drops the very "
        f"call issuing it. list_clients already excludes it — pick a client id "
        f"from there, or use redis-cli if you really must kill this one."
    )


def kill_client(conn: Any, client_id: int = 0, addr: str = "") -> dict:
    """[WRITE][med] Disconnect one client by id or addr. IRREVERSIBLE.

    Captures the client's CLIENT LIST row as priorState before the kill (who it
    was, from where, last command) — the connection itself cannot be restored,
    so no undo is recorded. Most clients transparently reconnect.

    **Refuses this tool's own connection**, by id or by addr. A kill has no undo
    to begin with, so aiming one at the calling connection is the purest form of
    an operation destroying its own reversibility. If the identity cannot be
    determined the call proceeds (unknown is never treated as "it is me").
    """
    if not client_id and not addr:
        raise ValueError("Pass client_id (from list_clients) or addr ('ip:port').")
    guard_kill_client(conn, client_id, addr)
    match: dict = {}
    for c in conn.redis_client_list():
        if (client_id and str(c.get("id")) == str(client_id)) or (
            addr and str(c.get("addr")) == str(addr)
        ):
            match = {
                "id": opt(c.get("id"), 32),
                "addr": opt(c.get("addr"), 64),
                "name": opt(c.get("name"), 64),
                "lastCommand": opt(c.get("cmd"), 64),
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
        "pattern": opt(obj.get("pattern"), 128),
        "definition": as_obj(obj.get("definition")),
        "priority": num(obj.get("priority")),
        "applyTo": opt(obj.get("apply-to") or obj.get("apply_to") or "all", 32),
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
