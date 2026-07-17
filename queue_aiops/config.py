"""Configuration management for Queue AIops.

Loads broker connection targets from a YAML config file. Each target names its
``platform`` — ``redis`` (RESP wire protocol via the Python client) or
``rabbitmq`` (management HTTP API) — so one config can span a mixed estate. See
:mod:`queue_aiops.platform` for how the platform name selects the protocol
shape (client vs HTTP + resource paths).

The secret is NEVER stored in the config file or in plaintext on disk: it lives
in the encrypted store ``~/.queue-aiops/secrets.enc`` (see
:mod:`queue_aiops.secretstore`). For redis the secret is the ``AUTH`` password
— **optional**, because auth-less lab instances are common (reachability is
then the boundary). For rabbitmq it is the management user's password that
pairs with ``username`` (presented as HTTP Basic auth) and is required. A
legacy env var (``QUEUE_<TARGET>_SECRET``) is honoured as a fallback.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from queue_aiops.governance.paths import ops_home
from queue_aiops.platform import PLATFORMS, RABBITMQ, REDIS, get_platform
from queue_aiops.secretstore import SecretStoreError, get_secret, has_store

if TYPE_CHECKING:
    from queue_aiops.platform import Platform

CONFIG_DIR = ops_home()
CONFIG_FILE = CONFIG_DIR / "config.yaml"
ENV_FILE = CONFIG_DIR / ".env"

SECRET_ENV_PREFIX = "QUEUE_"  # nosec B105 — env-var name, not a secret
SECRET_ENV_SUFFIX = "_SECRET"  # nosec B105 — env-var name, not a secret

_log = logging.getLogger("queue-aiops.config")


def _secret_env_key(name: str) -> str:
    """Legacy per-target secret env var name, e.g. QUEUE_CACHE1_SECRET."""
    return f"{SECRET_ENV_PREFIX}{name.upper().replace('-', '_')}{SECRET_ENV_SUFFIX}"


def _resolve_secret(name: str, *, required: bool) -> str:
    """Return a target's secret: encrypted store first, then legacy env var.

    ``required=False`` (redis) returns "" when no secret is stored anywhere —
    an auth-less lab instance is a legitimate setup, not an error.
    """
    if has_store():
        try:
            return get_secret(name)
        except SecretStoreError:
            pass  # fall through to legacy env var
    legacy = os.environ.get(_secret_env_key(name))
    if legacy:
        _log.warning(
            "Using plaintext env var %s. Migrate to the encrypted store with "
            "'queue-aiops secret migrate'.",
            _secret_env_key(name),
        )
        return legacy
    if not required:
        return ""
    raise OSError(
        f"No secret for target '{name}'. Add one with "
        f"'queue-aiops secret set {name}' (stored encrypted), or run "
        f"'queue-aiops init'."
    )


@dataclass(frozen=True)
class TargetConfig:
    """A connection target for one broker.

    ``platform`` is ``redis`` or ``rabbitmq`` (validated at construction).
    ``username`` holds the rabbitmq management user (unused for redis); ``db``
    is the redis logical database index (unused for rabbitmq). ``use_tls``
    selects rediss:// / https://; ``verify_ssl`` governs certificate checks.
    The secret (redis password — optional — / rabbitmq password) comes from the
    encrypted store.
    """

    name: str
    platform: str = REDIS
    host: str = ""
    port: int = 0
    username: str = ""
    db: int = 0
    use_tls: bool = False
    verify_ssl: bool = True

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError(
                f"Target '{self.name}': platform must be one of {PLATFORMS}, "
                f"got '{self.platform}'."
            )
        if not self.port:
            object.__setattr__(self, "port", self.platform_obj.default_port)

    @property
    def platform_obj(self) -> Platform:
        return get_platform(self.platform)

    @property
    def secret_required(self) -> bool:
        """Redis auth is optional (lab instances); rabbitmq Basic auth is not."""
        return self.platform == RABBITMQ

    @property
    def secret(self) -> str:
        return _resolve_secret(self.name, required=self.secret_required)

    @property
    def base_url(self) -> str:
        """HTTP base URL (rabbitmq management API)."""
        scheme = "https" if self.use_tls else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class AppConfig:
    """Top-level application config."""

    targets: tuple[TargetConfig, ...] = ()

    def get_target(self, name: str) -> TargetConfig:
        for t in self.targets:
            if t.name == name:
                return t
        available = ", ".join(t.name for t in self.targets) or "(none)"
        raise KeyError(f"Target '{name}' not found. Available: {available}")

    @property
    def default_target(self) -> TargetConfig:
        if not self.targets:
            raise ValueError("No targets configured. Check config.yaml")
        return self.targets[0]


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load config from YAML; the secret comes from the encrypted store."""
    path = config_path or CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Run 'queue-aiops init' to set up a redis or rabbitmq target, "
            f"or create {CONFIG_FILE} with a 'targets' list."
        )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    targets = tuple(
        TargetConfig(
            name=t["name"],
            platform=t.get("platform", REDIS),
            host=t["host"],
            port=t.get("port", 0),
            username=t.get("username", ""),
            db=t.get("db", 0),
            use_tls=t.get("use_tls", False),
            verify_ssl=t.get("verify_ssl", True),
        )
        for t in raw.get("targets", [])
    )

    return AppConfig(targets=targets)
