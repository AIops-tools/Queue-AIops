"""CLI tests for ``queue-aiops secret`` — the encrypted credential store.

Drive set / list / rm / migrate / rotate-password through the real Typer app
against a throwaway store dir (real Fernet crypto, no ``~/.queue-aiops``). The
master password comes from the env var so nothing prompts. Assertions prove:
a value is stored under real encryption (never plaintext on disk) and reloads,
list never shows values, rm removes, migrate imports ``QUEUE_<T>_SECRET`` env
lines and renames the legacy file, and rotate re-encrypts (and rejects a
mismatched confirmation). No secret value is ever printed.
"""

from __future__ import annotations

import getpass

import pytest
from typer.testing import CliRunner

import queue_aiops.secretstore as ss
from queue_aiops.cli import app

runner = CliRunner()


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    """Point the secret store at a throwaway dir + set the master password."""
    monkeypatch.setattr(ss, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ss, "SECRETS_FILE", tmp_path / "secrets.enc")
    monkeypatch.setattr(ss, "LEGACY_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(ss, "_cached", None)
    monkeypatch.setenv(ss.MASTER_PASSWORD_ENV, "unit-master-pw")
    return tmp_path


@pytest.mark.unit
def test_secret_set_stores_encrypted_and_reloads(store_dir):
    result = runner.invoke(app, ["secret", "set", "cache1", "--value", "s3cr3t-token"])
    assert result.exit_code == 0, result.output
    assert "Stored encrypted API key" in result.output
    blob = (store_dir / "secrets.enc").read_text()
    assert "s3cr3t-token" not in blob  # encrypted at rest
    assert "ciphertext" in blob
    assert ss.SecretStore.unlock("unit-master-pw").get("cache1") == "s3cr3t-token"


@pytest.mark.unit
def test_secret_list_shows_names_never_values(store_dir):
    ss.SecretStore.unlock("unit-master-pw").set("cache1", "hidden-value")
    result = runner.invoke(app, ["secret", "list"])
    assert result.exit_code == 0, result.output
    assert "cache1" in result.output
    assert "hidden-value" not in result.output


@pytest.mark.unit
def test_secret_list_empty_teaches_how_to_add(store_dir):
    result = runner.invoke(app, ["secret", "list"])
    assert result.exit_code == 0, result.output
    assert "No secrets stored" in result.output


@pytest.mark.unit
def test_secret_rm_deletes_the_named_key(store_dir):
    ss.SecretStore.unlock("unit-master-pw").set("cache1", "v").set("broker1", "w")
    result = runner.invoke(app, ["secret", "rm", "cache1"])
    assert result.exit_code == 0, result.output
    assert ss.SecretStore.unlock("unit-master-pw").names() == ("broker1",)


@pytest.mark.unit
def test_secret_migrate_imports_legacy_env_lines(store_dir):
    (store_dir / ".env").write_text(
        "# legacy\nQUEUE_CACHE1_SECRET=plain-old-pw\nQUEUE_BROKER1_SECRET='quoted-pw'\n"
    )
    result = runner.invoke(app, ["secret", "migrate"])
    assert result.exit_code == 0, result.output
    assert "Imported 2 secret(s)" in result.output
    store = ss.SecretStore.unlock("unit-master-pw")
    assert store.get("cache1") == "plain-old-pw"
    assert store.get("broker1") == "quoted-pw"
    assert (store_dir / ".env.migrated").exists()  # legacy file renamed, not lost


@pytest.mark.unit
def test_secret_migrate_reports_nothing_when_no_legacy_file(store_dir):
    result = runner.invoke(app, ["secret", "migrate"])
    assert result.exit_code == 0, result.output
    assert "Nothing to migrate" in result.output


@pytest.mark.unit
def test_secret_rotate_password_reencrypts_under_new_password(store_dir, monkeypatch):
    ss.SecretStore.unlock("unit-master-pw").set("cache1", "keep-me")
    monkeypatch.setattr(getpass, "getpass", lambda *_a, **_k: "new-master-pw")
    result = runner.invoke(app, ["secret", "rotate-password"])
    assert result.exit_code == 0, result.output
    assert "Master password rotated" in result.output
    assert ss.SecretStore.unlock("new-master-pw").get("cache1") == "keep-me"


@pytest.mark.unit
def test_secret_rotate_password_aborts_on_mismatch(store_dir, monkeypatch):
    ss.SecretStore.unlock("unit-master-pw").set("cache1", "keep-me")
    answers = iter(["new-pw", "different-pw"])
    monkeypatch.setattr(getpass, "getpass", lambda *_a, **_k: next(answers))
    result = runner.invoke(app, ["secret", "rotate-password"])
    assert result.exit_code == 1
    assert "did not match" in result.output
    # original password still unlocks — nothing was rotated
    assert ss.SecretStore.unlock("unit-master-pw").get("cache1") == "keep-me"
