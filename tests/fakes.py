"""Shared fakes: an in-memory redis client and a rabbitmq management HTTP client.

Both are injected through ``QueueConnection(target, client=...)`` so every test
exercises the REAL connection layer (platform guards, error translation, typed
helpers) with zero live brokers.
"""

from __future__ import annotations

import json
from typing import Any

from queue_aiops.config import TargetConfig
from queue_aiops.connection import QueueConnection
from queue_aiops.platform import RABBITMQ, REDIS


class FakeRedis:
    """Stands in for redis.Redis: canned returns + a call log."""

    def __init__(
        self,
        info: dict | None = None,
        slowlog: list | None = None,
        clients: list | None = None,
        config: dict | None = None,
        memory_stats: dict | None = None,
        memory_usage: dict | None = None,
        scan_pages: list | None = None,
        dbsize: int = 0,
        kill_result: int = 1,
    ) -> None:
        self._info = info or {}
        self._slowlog = slowlog or []
        self._clients = clients or []
        self._config = dict(config or {})
        self._memory_stats = memory_stats or {}
        self._memory_usage = memory_usage or {}
        self._scan_pages = list(scan_pages or [(0, [])])
        self._dbsize = dbsize
        self._kill_result = kill_result
        self.calls: list[tuple] = []

    def ping(self):
        self.calls.append(("ping",))
        return True

    def info(self, section: str | None = None):
        self.calls.append(("info", section))
        if section is None:
            return self._info.get("all", {})
        return self._info.get(section, {})

    def slowlog_get(self, count: int):
        self.calls.append(("slowlog_get", count))
        return self._slowlog[:count]

    def client_list(self):
        self.calls.append(("client_list",))
        return self._clients

    def client_kill_filter(self, _id: str | None = None, addr: str | None = None):
        self.calls.append(("client_kill_filter", _id, addr))
        return self._kill_result

    def config_get(self, pattern: str):
        self.calls.append(("config_get", pattern))
        if pattern in self._config:
            return {pattern: self._config[pattern]}
        if pattern == "*":
            return dict(self._config)
        return {}

    def config_set(self, param: str, value: str):
        self.calls.append(("config_set", param, value))
        self._config[param] = value
        return True

    def memory_stats(self):
        self.calls.append(("memory_stats",))
        return self._memory_stats

    def memory_usage(self, key: str, samples: int = 0):
        self.calls.append(("memory_usage", key))
        return self._memory_usage.get(key)

    def scan(self, cursor: int = 0, count: int = 500):
        """Pages are consumed positionally: the cursor is the page index."""
        self.calls.append(("scan", cursor, count))
        idx = int(cursor)
        if idx >= len(self._scan_pages):
            return (0, [])
        return self._scan_pages[idx]

    def dbsize(self):
        self.calls.append(("dbsize",))
        return self._dbsize

    def close(self):
        self.calls.append(("close",))


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


class FakeHttp:
    """Stands in for httpx.Client: (method, path) → payload/Response routes."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes = dict(routes or {})
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method: str, path: str, **kwargs: Any):
        self.requests.append((method, path, kwargs))
        key = (method.upper(), path)
        if key not in self.routes:
            return FakeResponse(404, {"error": "Object Not Found"})
        hit = self.routes[key]
        if isinstance(hit, FakeResponse):
            return hit
        return FakeResponse(200, hit)

    def close(self):
        self.requests.append(("CLOSE", "", {}))


def redis_target(name: str = "cache1") -> TargetConfig:
    return TargetConfig(name=name, platform=REDIS, host="cache.example.com")


def rabbit_target(name: str = "broker1") -> TargetConfig:
    return TargetConfig(
        name=name, platform=RABBITMQ, host="mq.example.com", username="admin"
    )


def redis_conn(**fake_kwargs: Any) -> QueueConnection:
    return QueueConnection(redis_target(), client=FakeRedis(**fake_kwargs))


def rabbit_conn(routes: dict[tuple[str, str], Any] | None = None) -> QueueConnection:
    return QueueConnection(rabbit_target(), client=FakeHttp(routes))
