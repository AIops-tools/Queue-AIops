"""Platform descriptors — the message/cache brokers queue-aiops speaks to.

queue-aiops is multi-platform by construction. A registry maps a *platform
name* to a :class:`Platform` descriptor that captures everything the connection
and ops layers need to talk to that broker: which protocol it speaks, the
default port, and — for an HTTP management plane — the concrete REST path for
each *logical resource* plus how a raw response is normalised (injection-safe).

v0.1 registers two platforms:

  * **redis** — the Redis wire protocol (RESP) via the ``redis`` Python client.
    Password auth (``AUTH``) is optional (an auth-less lab instance is common),
    TLS optional. There are no REST paths; the connection layer exposes typed
    command helpers (INFO / SLOWLOG / CLIENT LIST / CONFIG / MEMORY / SCAN).
  * **rabbitmq** — the RabbitMQ management HTTP API (``/api/...``) with HTTP
    Basic auth (management-plugin user). Queue and policy names — and the vhost,
    whose default is literally ``/`` — are percent-encoded into every path.

Additional brokers can ``register`` their own descriptor later without touching
the ops / CLI / MCP layers — a registry keyed by ``platform`` name.

The concrete REST paths below are modelled from the management API's public
documentation and are exercised against mocked responses only; see the README's
preview note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from queue_aiops.governance import sanitize


def _seg(value: Any) -> str:
    """URL-encode one path segment so agent-supplied identifiers (queue names,
    policy names, and especially the default vhost ``/``) cannot smuggle ``/``,
    ``../`` or query metacharacters into the request URL."""
    return quote(str(value), safe="")


# ─── registered platform names ──────────────────────────────────────────────
REDIS = "redis"
RABBITMQ = "rabbitmq"
PLATFORMS = (REDIS, RABBITMQ)

# Protocol kinds.
KIND_CLIENT = "client"  # native wire protocol via a Python client (redis)
KIND_HTTP = "http"  # HTTP management API (rabbitmq)

# Bounds for the response normaliser (defensive against a hostile broker).
_MAX_STR = 512
_MAX_DEPTH = 8

# Keys under which an HTTP platform wraps a list payload, tried in order before
# falling back to a bare JSON array (the management API pages under ``items``).
_LIST_KEYS = ("items", "rows", "data", "results")


def _sanitize_obj(obj: Any, depth: int = 0) -> Any:
    """Recursively fold broker-returned data into injection-safe values.

    Every string leaf passes through ``sanitize`` (bounded length); numbers,
    booleans and ``None`` pass through unchanged. Depth is capped so a
    pathological nesting cannot exhaust the stack.
    """
    if depth > _MAX_DEPTH:
        return None
    if isinstance(obj, dict):
        return {str(k): _sanitize_obj(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_obj(v, depth + 1) for v in obj]
    if isinstance(obj, str):
        return sanitize(obj, _MAX_STR)
    return obj


@dataclass(frozen=True)
class Platform:
    """A broker's shape: protocol kind + logical-resource path map + normaliser."""

    name: str
    label: str
    kind: str
    default_port: int
    paths: dict[str, str] = field(default_factory=dict)

    @property
    def is_http(self) -> bool:
        return self.kind == KIND_HTTP

    def path(self, resource: str, **fmt: Any) -> str:
        """Return the concrete REST path for a logical ``resource``.

        Raises a teaching ``KeyError`` when the resource is not mapped for this
        platform (so a caller asking for an unsupported surface fails fast with
        the list of what *is* available, rather than hitting a confusing 404).

        Every substituted value is URL-encoded (``quote(..., safe="")``) so an
        agent-supplied identifier can never rewrite the path (e.g. via ``../``
        or the vhost ``/``).
        """
        try:
            template = self.paths[resource]
        except KeyError as exc:
            available = ", ".join(sorted(self.paths)) or "(none)"
            raise KeyError(
                f"Resource '{resource}' is not mapped for platform '{self.name}'. "
                f"Mapped resources: {available}."
            ) from exc
        if not fmt:
            return template
        return template.format(**{k: _seg(v) for k, v in fmt.items()})

    def supports(self, resource: str) -> bool:
        return resource in self.paths

    def rows(self, payload: Any) -> list[dict]:
        """Normalise a list payload to a sanitised list of dict rows.

        A bare JSON array passes through; a dict is unwrapped via the first of
        ``items``/``rows``/``data``/``results`` that is present. Every row is
        run through the injection-safe normaliser.
        """
        if isinstance(payload, dict):
            items: Any = []
            for key in _LIST_KEYS:
                value = payload.get(key)
                if isinstance(value, list):
                    items = value
                    break
        else:
            items = payload
        return [_sanitize_obj(r) for r in (items or []) if isinstance(r, dict)]

    def normalise(self, payload: Any) -> Any:
        """Return an injection-safe copy of a raw response payload."""
        return _sanitize_obj(payload)


# ─── registry ───────────────────────────────────────────────────────────────
_REGISTRY: dict[str, Platform] = {}


def register(platform: Platform) -> None:
    """Register a platform descriptor under its name (idempotent overwrite)."""
    _REGISTRY[platform.name] = platform


def get_platform(name: str) -> Platform:
    """Return the descriptor for ``name`` or raise with the registered names."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(
            f"Unknown platform '{name}'. Registered platforms: {available}."
        ) from exc


def platform_names() -> tuple[str, ...]:
    """All registered platform names (sorted)."""
    return tuple(sorted(_REGISTRY))


# ─── RabbitMQ management HTTP API (/api/..., HTTP Basic) ─────────────────────
_RABBITMQ_PATHS = {
    # cluster / broker
    "overview": "/api/overview",
    "nodes": "/api/nodes",
    "whoami": "/api/whoami",
    # queues
    "queues": "/api/queues",
    "queues_vhost": "/api/queues/{vhost}",
    "queue": "/api/queues/{vhost}/{name}",
    "queue_purge": "/api/queues/{vhost}/{name}/contents",
    # connections / channels / consumers
    "connections": "/api/connections",
    "channels": "/api/channels",
    "consumers": "/api/consumers",
    # policies
    "policies": "/api/policies",
    "policies_vhost": "/api/policies/{vhost}",
    "policy": "/api/policies/{vhost}/{name}",
}

register(
    Platform(
        name=REDIS,
        label="Redis (RESP client)",
        kind=KIND_CLIENT,
        default_port=6379,
    )
)
register(
    Platform(
        name=RABBITMQ,
        label="RabbitMQ management API",
        kind=KIND_HTTP,
        default_port=15672,
        paths=_RABBITMQ_PATHS,
    )
)


__all__ = [
    "REDIS",
    "RABBITMQ",
    "PLATFORMS",
    "KIND_CLIENT",
    "KIND_HTTP",
    "Platform",
    "register",
    "get_platform",
    "platform_names",
]
