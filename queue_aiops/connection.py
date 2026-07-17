"""Connection management for brokers (redis + rabbitmq).

A single :class:`QueueConnection` speaks the protocol of its target's platform:

  * **redis** — the Redis wire protocol via the ``redis`` Python client
    (password optional, TLS optional, 30s socket timeouts,
    ``decode_responses=True`` so all values come back as ``str``). Typed
    ``redis_*`` helpers cover exactly the command surface the ops layer needs —
    INFO / SLOWLOG / CLIENT LIST / CLIENT KILL / CONFIG GET/SET /
    MEMORY STATS/USAGE / SCAN / DBSIZE / PING. There is deliberately **no**
    generic "run any command" passthrough, and big-key sampling is SCAN-based
    with a hard budget — never ``KEYS *``.
  * **rabbitmq** — the management HTTP API with HTTP Basic auth. Ops modules
    never hard-code a path: they ask ``conn.platform.path("queues")`` for the
    concrete URL (every interpolated segment percent-encoded, including the
    default vhost ``/``) and ``conn.platform.rows()`` to unwrap list payloads.

All failures are translated centrally into ``QueueApiError`` with a teaching
message — protocol errors are translated at the connection layer rather than
leaking raw tracebacks. The underlying client is injectable for tests: pass
``client=`` a mock implementing the redis client methods (redis targets) or
``request`` / ``close`` (rabbitmq targets). No live broker is needed in tests.
"""

from __future__ import annotations

from typing import Any

import httpx

from queue_aiops.config import AppConfig, TargetConfig, load_config
from queue_aiops.platform import RABBITMQ, REDIS

_TIMEOUT = 30.0


class QueueApiError(Exception):
    """A broker call failed; carries a teaching message + status/context."""

    def __init__(self, message: str, *, status_code: int | None = None, path: str = "") -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(message)


def _teaching_message(status: int, path: str, body: str, label: str) -> str:
    """Map a non-2xx management-API status to an actionable, teaching message."""
    snippet = body[:200].strip()
    if status in (401, 403):
        return (
            f"Authentication/authorization failed ({status}) on {label} {path}. "
            f"Check the management user/password and that the user has the "
            f"'monitoring' or 'management' tag (and access to the vhost). {snippet}"
        )
    if status == 404:
        return (
            f"Resource not found (404) on {label} {path}. The vhost or "
            f"queue/policy name may be stale — list the parent collection first "
            f"(the default vhost is '/', sent percent-encoded). {snippet}"
        )
    if status == 400:
        return (
            f"Bad request (400) on {label} {path}. The broker rejected the "
            f"request — check required fields and value formats. {snippet}"
        )
    if status in (500, 502, 503, 504):
        return (
            f"{label} server error ({status}) on {path}. The broker may be "
            f"busy; retry shortly. {snippet}"
        )
    return f"{label} API error ({status}) on {path}. {snippet}"


class QueueConnection:
    """A single authenticated session against one redis or rabbitmq target."""

    def __init__(self, target: TargetConfig, client: Any | None = None) -> None:
        self._target = target
        self._client = client or self._build_client(target)

    @staticmethod
    def _build_client(target: TargetConfig) -> Any:
        if target.platform == REDIS:
            import redis as redis_pkg  # lazy: only redis targets need it

            kwargs: dict[str, Any] = {
                "host": target.host,
                "port": target.port,
                "db": target.db,
                "socket_timeout": _TIMEOUT,
                "socket_connect_timeout": _TIMEOUT,
                "decode_responses": True,
            }
            if target.secret:
                kwargs["password"] = target.secret
            if target.use_tls:
                kwargs["ssl"] = True
                kwargs["ssl_cert_reqs"] = "required" if target.verify_ssl else "none"
            return redis_pkg.Redis(**kwargs)
        return httpx.Client(
            base_url=target.base_url,
            verify=target.verify_ssl,
            timeout=_TIMEOUT,
            auth=httpx.BasicAuth(target.username, target.secret),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    @property
    def target(self) -> TargetConfig:
        return self._target

    @property
    def platform(self) -> Any:
        return self._target.platform_obj

    def _require(self, platform_name: str) -> None:
        """Fail fast (teaching) when an op is asked of the wrong platform."""
        if self._target.platform != platform_name:
            raise QueueApiError(
                f"Target '{self._target.name}' is a {self._target.platform} "
                f"target; this operation needs a {platform_name} target. "
                f"Pass a {platform_name} target name, or check the config."
            )

    def close(self) -> None:
        self._client.close()

    # ── redis command helpers (typed surface, no generic passthrough) ──────
    def _redis_call(self, description: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """Guard the platform FIRST, then resolve + run the client method."""
        self._require(REDIS)
        fn = getattr(self._client, method)
        try:
            return fn(*args, **kwargs)
        except QueueApiError:
            raise
        except Exception as exc:  # redis.RedisError, socket errors — translated
            raise QueueApiError(
                f"Redis call failed ({description}) against "
                f"{self._target.host}:{self._target.port}: {exc}. Check "
                f"host/port, password (AUTH), and that the instance is up.",
                path=description,
            ) from exc

    def redis_ping(self) -> bool:
        return bool(self._redis_call("PING", "ping"))

    def redis_info(self, section: str | None = None) -> dict:
        out = (
            self._redis_call(f"INFO {section}", "info", section)
            if section
            else self._redis_call("INFO", "info")
        )
        return out if isinstance(out, dict) else {}

    def redis_slowlog(self, count: int = 128) -> list:
        out = self._redis_call("SLOWLOG GET", "slowlog_get", count)
        return out if isinstance(out, list) else []

    def redis_client_list(self) -> list[dict]:
        out = self._redis_call("CLIENT LIST", "client_list")
        return [c for c in out if isinstance(c, dict)] if isinstance(out, list) else []

    def redis_client_kill_id(self, client_id: int) -> int:
        """CLIENT KILL ID <id>; returns the number of clients killed."""
        out = self._redis_call(
            "CLIENT KILL ID", "client_kill_filter", _id=str(int(client_id))
        )
        return int(out or 0)

    def redis_client_kill_addr(self, addr: str) -> int:
        """CLIENT KILL ADDR <addr>; returns the number of clients killed."""
        out = self._redis_call("CLIENT KILL ADDR", "client_kill_filter", addr=str(addr))
        return int(out or 0)

    def redis_config_get(self, pattern: str) -> dict:
        out = self._redis_call(f"CONFIG GET {pattern}", "config_get", pattern)
        return out if isinstance(out, dict) else {}

    def redis_config_set(self, param: str, value: str) -> bool:
        return bool(self._redis_call(f"CONFIG SET {param}", "config_set", param, value))

    def redis_memory_stats(self) -> dict:
        out = self._redis_call("MEMORY STATS", "memory_stats")
        return out if isinstance(out, dict) else {}

    def redis_memory_usage(self, key: str, samples: int = 0) -> int | None:
        out = self._redis_call("MEMORY USAGE", "memory_usage", key, samples=samples)
        return int(out) if out is not None else None

    def redis_scan(self, cursor: int = 0, count: int = 500) -> tuple[int, list[str]]:
        """One SCAN page. The caller enforces the scan budget."""
        out = self._redis_call("SCAN", "scan", cursor=cursor, count=count)
        nxt, keys = (out if isinstance(out, (tuple, list)) and len(out) == 2 else (0, []))
        return int(nxt), [str(k) for k in (keys or [])]

    def redis_dbsize(self) -> int:
        return int(self._redis_call("DBSIZE", "dbsize") or 0)

    # ── rabbitmq management-API helpers (HTTP) ──────────────────────────────
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._require(RABBITMQ)
        label = self._target.platform_obj.label
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise QueueApiError(
                f"Could not reach {label} at {self._target.base_url} "
                f"({method} {path}): {exc}. Check host/port and that the "
                f"management plugin is enabled.",
                path=path,
            ) from exc
        if not (200 <= resp.status_code < 300):
            raise QueueApiError(
                _teaching_message(resp.status_code, path, resp.text, label),
                status_code=resp.status_code,
                path=path,
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._request("DELETE", path, **kwargs)


class ConnectionManager:
    """Manages connections to multiple broker targets with session reuse."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._connections: dict[str, QueueConnection] = {}

    @classmethod
    def from_config(cls, config: AppConfig | None = None) -> ConnectionManager:
        cfg = config or load_config()
        return cls(cfg)

    def connect(self, target_name: str | None = None) -> QueueConnection:
        target = (
            self._config.get_target(target_name)
            if target_name
            else self._config.default_target
        )
        cached = self._connections.get(target.name)
        if cached is not None:
            return cached
        conn = QueueConnection(target)
        self._connections[target.name] = conn
        return conn

    def disconnect(self, target_name: str) -> None:
        conn = self._connections.pop(target_name, None)
        if conn is not None:
            conn.close()

    def disconnect_all(self) -> None:
        for name in list(self._connections):
            self.disconnect(name)

    def list_targets(self) -> list[str]:
        return [t.name for t in self._config.targets]

    def list_connected(self) -> list[str]:
        return list(self._connections.keys())
