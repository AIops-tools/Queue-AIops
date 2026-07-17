"""Tests for the ``queue-aiops init`` onboarding wizard.

The wizard is driven end-to-end through Typer's CliRunner with every path
(config.yaml, secrets.enc, rules.yaml) isolated under tmp_path. The master
password comes from QUEUE_AIOPS_MASTER_PASSWORD (the non-interactive path)
and the hidden secret prompt is patched at the getpass boundary.
"""

from __future__ import annotations

import getpass as getpass_mod

import pytest
import yaml
from typer.testing import CliRunner

import queue_aiops.cli.init as init_mod
import queue_aiops.config as config_mod
import queue_aiops.doctor as doctor_mod
import queue_aiops.secretstore as ss

MASTER_PW = "init-master-pw"
SECRET = "broker-password-0123"

# Wizard answers (redis default path): name, accept platform default (redis),
# host, accept default port, accept default db, accept TLS default (False),
# no second target, decline the trailing doctor run. The secret itself comes
# via getpass.
WIZARD_INPUT_REDIS = "cache1\n\ncache.example.com\n\n\n\nn\nn\n"


@pytest.fixture
def init_home(tmp_path, monkeypatch):
    """Isolate config + secret store + governance home under tmp_path."""
    config_file = tmp_path / "config.yaml"
    secrets_file = tmp_path / "secrets.enc"
    monkeypatch.setenv("QUEUE_AIOPS_HOME", str(tmp_path))
    monkeypatch.setenv("QUEUE_AIOPS_MASTER_PASSWORD", MASTER_PW)
    monkeypatch.setattr(init_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(init_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(ss, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ss, "SECRETS_FILE", secrets_file)
    monkeypatch.setattr(ss, "LEGACY_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(ss, "_cached", None)
    # The hidden secret prompt bypasses CliRunner stdin.
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": SECRET)
    return tmp_path


def _run_init(input_text: str = WIZARD_INPUT_REDIS):
    from queue_aiops.cli import app

    return CliRunner().invoke(app, ["init"], input=input_text)


@pytest.mark.unit
def test_init_writes_redis_config_with_defaults(init_home):
    result = _run_init()
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((init_home / "config.yaml").read_text("utf-8"))
    assert raw["targets"] == [
        {
            "name": "cache1",
            "platform": "redis",
            "host": "cache.example.com",
            "port": 6379,
            "username": "",
            "db": 0,
            "use_tls": False,  # accepted TLS confirm default=False must land
            "verify_ssl": True,
        }
    ]


@pytest.mark.unit
def test_init_tls_verify_defaults_true(init_home):
    """Choosing TLS and accepting the verify default must land verify_ssl=True."""
    result = _run_init("cache1\n\ncache.example.com\n\n\ny\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((init_home / "config.yaml").read_text("utf-8"))
    assert raw["targets"][0]["use_tls"] is True
    assert raw["targets"][0]["verify_ssl"] is True
    assert "self-signed lab" in result.output


@pytest.mark.unit
def test_init_tls_verify_can_be_declined_for_lab_certs(init_home):
    result = _run_init("cache1\n\ncache.example.com\n\n\ny\nn\nn\nn\n")
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((init_home / "config.yaml").read_text("utf-8"))
    assert raw["targets"][0]["verify_ssl"] is False


@pytest.mark.unit
def test_init_rabbitmq_branch_prompts_username(init_home):
    result = _run_init("broker1\nrabbitmq\nmq.example.com\n\n\nadmin\nn\nn\n")
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((init_home / "config.yaml").read_text("utf-8"))
    target = raw["targets"][0]
    assert target["platform"] == "rabbitmq"
    assert target["port"] == 15672
    assert target["username"] == "admin"
    assert ss.SecretStore.unlock(MASTER_PW).get("broker1") == SECRET


@pytest.mark.unit
def test_init_redis_password_is_optional(init_home, monkeypatch):
    """An empty redis password is accepted (auth-less lab) and stores nothing."""
    monkeypatch.setattr(getpass_mod, "getpass", lambda prompt="": "")
    result = _run_init()
    assert result.exit_code == 0, result.output
    assert "no AUTH" in result.output
    raw = yaml.safe_load((init_home / "config.yaml").read_text("utf-8"))
    assert raw["targets"][0]["name"] == "cache1"
    if (init_home / "secrets.enc").exists():
        with pytest.raises(ss.SecretStoreError):
            ss.SecretStore.unlock(MASTER_PW).get("cache1")


@pytest.mark.unit
def test_init_rejects_unknown_platform_then_reprompts(init_home):
    result = _run_init("cache1\nkafka\ncache1\n\ncache.example.com\n\n\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert "Platform must be 'redis' or 'rabbitmq'." in result.output
    raw = yaml.safe_load((init_home / "config.yaml").read_text("utf-8"))
    assert [t["name"] for t in raw["targets"]] == ["cache1"]


@pytest.mark.unit
def test_init_stores_secret_encrypted_not_in_config(init_home):
    result = _run_init()
    assert result.exit_code == 0, result.output
    # The secret is readable back through the secret store API...
    assert ss.SecretStore.unlock(MASTER_PW).get("cache1") == SECRET
    # ...and never lands in plaintext in config.yaml or secrets.enc.
    assert SECRET not in (init_home / "config.yaml").read_text("utf-8")
    assert SECRET not in (init_home / "secrets.enc").read_text("utf-8")


@pytest.mark.unit
def test_init_seeds_default_rules_with_dual_control_tier(init_home):
    result = _run_init()
    assert result.exit_code == 0, result.output
    rules = yaml.safe_load((init_home / "rules.yaml").read_text("utf-8"))
    tiers = {r["name"]: r for r in rules["risk_tiers"]}
    assert "high-risk-requires-approver" in tiers
    assert tiers["high-risk-requires-approver"]["tier"] == "dual"
    assert tiers["high-risk-requires-approver"]["min_risk_level"] == "high"


@pytest.mark.unit
def test_init_rerun_does_not_clobber_existing_rules(init_home):
    sentinel = "# operator-authored rules — must survive re-init\nrisk_tiers: []\n"
    (init_home / "rules.yaml").write_text(sentinel, "utf-8")
    result = _run_init()
    assert result.exit_code == 0, result.output
    assert (init_home / "rules.yaml").read_text("utf-8") == sentinel


@pytest.mark.unit
def test_init_accepting_doctor_confirm_runs_doctor(init_home, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(doctor_mod, "run_doctor", lambda: calls.append(True) or 0)
    # Empty last answer accepts the confirm's default=True.
    result = _run_init("cache1\n\ncache.example.com\n\n\n\nn\n\n")
    assert result.exit_code == 0, result.output
    assert calls == [True]


@pytest.mark.unit
def test_init_overwrite_existing_target(init_home):
    result = _run_init()
    assert result.exit_code == 0, result.output
    # Same name again: confirm overwrite, new host, accept defaults.
    result = _run_init("cache1\ny\n\ncache-new.example.com\n\n\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((init_home / "config.yaml").read_text("utf-8"))
    assert [t["host"] for t in raw["targets"]] == ["cache-new.example.com"]
