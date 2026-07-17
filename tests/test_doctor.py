"""Tests for ``run_doctor`` — environment and connectivity diagnostics.

Everything is redirected to a tmp dir (config, secret store) and the
connection layer is faked at the ``ConnectionManager`` boundary, so no test
ever touches a real redis/rabbitmq or ``~/.queue-aiops``.
"""

from __future__ import annotations

import pytest
import yaml
from rich.console import Console

import queue_aiops.config as config_mod
import queue_aiops.connection as connection_mod
import queue_aiops.doctor as doctor_mod
import queue_aiops.secretstore as ss
from queue_aiops.doctor import run_doctor
from queue_aiops.platform import REDIS, get_platform

MASTER_PW = "test-master-pw"


@pytest.fixture
def doctor_home(tmp_path, monkeypatch):
    """Isolate config + secret store paths under tmp_path."""
    config_file = tmp_path / "config.yaml"
    env_file = tmp_path / ".env"
    secrets_file = tmp_path / "secrets.enc"
    monkeypatch.setenv("QUEUE_AIOPS_HOME", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "ENV_FILE", env_file)
    monkeypatch.setattr(doctor_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(doctor_mod, "ENV_FILE", env_file)
    monkeypatch.setattr(doctor_mod, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(ss, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ss, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(ss, "LEGACY_ENV_FILE", env_file)
    monkeypatch.setattr(ss, "_cached", None)
    # Wide console so long messages don't wrap mid-assertion.
    monkeypatch.setattr(doctor_mod, "_console", Console(width=500))
    monkeypatch.delenv("QUEUE_CACHE1_SECRET", raising=False)
    monkeypatch.delenv("QUEUE_BROKER1_SECRET", raising=False)
    return tmp_path


def _write_config(tmp_path, targets: list[dict]) -> None:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"targets": targets}), "utf-8")


def _seed_secret(monkeypatch, name: str, value: str = "s3cret-1") -> None:
    monkeypatch.setenv("QUEUE_AIOPS_MASTER_PASSWORD", MASTER_PW)
    ss.SecretStore.unlock(MASTER_PW).set(name, value)


_REDIS_TARGET = {"name": "cache1", "platform": "redis", "host": "cache.example.com"}
_RABBIT_TARGET = {
    "name": "broker1", "platform": "rabbitmq", "host": "mq.example.com",
    "username": "admin",
}


class _FakeConn:
    def __init__(self, target) -> None:
        self.target = target
        self.platform = get_platform(target.platform)

    def redis_ping(self):
        assert self.target.platform == REDIS
        return True

    def get(self, path):
        assert path == self.platform.path("overview")
        return {"rabbitmq_version": "3.13.2"}


class _HealthyManager:
    """Stands in for ConnectionManager: every connect() succeeds."""

    def __init__(self, config) -> None:
        self._config = config

    def connect(self, name):
        return _FakeConn(self._config.get_target(name))


class _UnreachableManager:
    """Stands in for ConnectionManager: every connect() fails."""

    def __init__(self, config) -> None:
        self._config = config

    def connect(self, name):
        raise ConnectionError("connection to 'cache.example.com' refused")


@pytest.mark.unit
def test_doctor_missing_config_fails_with_init_hint(doctor_home, capsys):
    assert run_doctor() == 1
    out = capsys.readouterr().out
    assert "Config file missing" in out
    assert "queue-aiops init" in out


@pytest.mark.unit
def test_doctor_config_load_failure_reported_not_raised(doctor_home, capsys):
    (doctor_home / "config.yaml").write_text("targets: [unclosed", "utf-8")
    assert run_doctor() == 1
    assert "Config load failed" in capsys.readouterr().out


@pytest.mark.unit
def test_doctor_no_targets_configured(doctor_home, capsys):
    _write_config(doctor_home, [])
    assert run_doctor() == 1
    assert "No targets configured" in capsys.readouterr().out


@pytest.mark.unit
def test_doctor_all_healthy_both_platforms_exit_zero(doctor_home, monkeypatch, capsys):
    _write_config(doctor_home, [_REDIS_TARGET, _RABBIT_TARGET])
    _seed_secret(monkeypatch, "cache1")
    _seed_secret(monkeypatch, "broker1")
    monkeypatch.setattr(connection_mod, "ConnectionManager", _HealthyManager)
    assert run_doctor() == 0
    out = capsys.readouterr().out
    assert "2 target(s) configured" in out
    assert "Encrypted secret store present" in out
    assert "Secret present for 'cache1' (redis)" in out
    assert "Connected to 'cache1' (redis cache.example.com) — PING OK" in out
    assert "Connected to 'broker1' (rabbitmq mq.example.com) — management /api/overview OK" in out


@pytest.mark.unit
def test_doctor_redis_without_secret_warns_but_passes(doctor_home, monkeypatch, capsys):
    """An auth-less redis lab target is a warning, not a failure."""
    _write_config(doctor_home, [_REDIS_TARGET])
    _seed_secret(monkeypatch, "unrelated")  # store exists, but no cache1 entry
    monkeypatch.setattr(connection_mod, "ConnectionManager", _HealthyManager)
    assert run_doctor() == 0
    out = capsys.readouterr().out
    assert "No secret for 'cache1' (redis)" in out
    assert "without AUTH" in out


@pytest.mark.unit
def test_doctor_rabbitmq_without_secret_fails(doctor_home, monkeypatch, capsys):
    _write_config(doctor_home, [_RABBIT_TARGET])
    _seed_secret(monkeypatch, "unrelated")
    monkeypatch.setattr(connection_mod, "ConnectionManager", _HealthyManager)
    assert run_doctor(skip_auth=True) == 1
    assert "No secret for target 'broker1'" in capsys.readouterr().out


@pytest.mark.unit
def test_doctor_skip_auth_skips_connectivity(doctor_home, monkeypatch, capsys):
    _write_config(doctor_home, [_REDIS_TARGET])
    _seed_secret(monkeypatch, "cache1")

    def _boom(config):  # doctor must not even construct a manager
        raise AssertionError("ConnectionManager should not be used with --skip-auth")

    monkeypatch.setattr(connection_mod, "ConnectionManager", _boom)
    assert run_doctor(skip_auth=True) == 0
    out = capsys.readouterr().out
    assert "Skipping connectivity check" in out
    assert "Connected" not in out


@pytest.mark.unit
def test_doctor_unreachable_target_exit_one(doctor_home, monkeypatch, capsys):
    _write_config(doctor_home, [_REDIS_TARGET])
    _seed_secret(monkeypatch, "cache1")
    monkeypatch.setattr(connection_mod, "ConnectionManager", _UnreachableManager)
    assert run_doctor() == 1
    out = capsys.readouterr().out
    assert "Connect to 'cache1' failed" in out
    assert "refused" in out


@pytest.mark.unit
def test_doctor_legacy_env_file_warns_migrate(doctor_home, monkeypatch, capsys):
    _write_config(doctor_home, [_RABBIT_TARGET])
    (doctor_home / ".env").write_text("QUEUE_BROKER1_SECRET=legacy\n", "utf-8")
    monkeypatch.setenv("QUEUE_BROKER1_SECRET", "legacy")
    assert run_doctor(skip_auth=True) == 0
    out = capsys.readouterr().out
    assert "legacy plaintext .env" in out
    assert "secret migrate" in out


@pytest.mark.unit
def test_doctor_warns_on_loose_secret_permissions(doctor_home, monkeypatch, capsys):
    _write_config(doctor_home, [_REDIS_TARGET])
    _seed_secret(monkeypatch, "cache1")
    (doctor_home / "secrets.enc").chmod(0o644)
    assert run_doctor(skip_auth=True) == 0
    assert "should be 600" in capsys.readouterr().out


@pytest.mark.unit
def test_cli_doctor_command_exits_with_doctor_code(doctor_home, monkeypatch):
    from typer.testing import CliRunner

    from queue_aiops.cli import app

    _write_config(doctor_home, [_REDIS_TARGET])
    _seed_secret(monkeypatch, "cache1")
    result = CliRunner().invoke(app, ["doctor", "--skip-auth"])
    assert result.exit_code == 0
    assert "Skipping connectivity check" in result.output
