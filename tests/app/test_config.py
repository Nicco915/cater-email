from __future__ import annotations

import json
from pathlib import Path

import pytest

from cater.app.config import AppConfig, ConfigurationError, load_config
from cater.app.errors import ValidationError


def test_defaults_are_offline_and_loopback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(runtime_dir=tmp_path / "runtime")
    assert config.sending.dry_run is True
    assert config.sending.enabled is False
    assert config.allowlist.test == ()
    assert config.allowlist.production == ()
    assert config.web.host == "127.0.0.1"
    assert config.mail.first_run_new_only is True


def test_explicit_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(path=tmp_path / "missing.toml", runtime_dir=tmp_path / "runtime")


def test_toml_values_and_string_booleans(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[runtime]
dir = "{tmp_path / 'data'}"

[mail]
account = "BOT@Example.com"
imap_host = "imap.example.com"
smtp_host = "smtp.example.com"
imap_ssl = true
imap_starttls = false
poll_interval_seconds = 15

[allowlist]
mode = "test"
test = ["  Tester@Example.com ", "tester@example.com"]

[sending]
dry_run = "false"
enabled = "false"

[web]
host = "localhost"
port = 9000
""",
        encoding="utf-8",
    )
    config = load_config(path=config_path)
    assert config.paths.runtime_dir == tmp_path / "data"
    assert config.mail.account == "BOT@Example.com"
    assert config.allowlist.test == ("tester@example.com",)
    assert config.sending.dry_run is False
    assert config.sending.enabled is False
    assert config.web.port == 9000


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '[allowlist]\nmode = "production"\nproduction = []\n',
            "production allowlist",
        ),
        (
            '[web]\nhost = "0.0.0.0"\n',
            "loopback",
        ),
        (
            "[mail]\nimap_ssl = true\nimap_starttls = true\n",
            "mutually exclusive",
        ),
        (
            '[allowlist]\nmode = "staging"\n',
            "mode",
        ),
    ],
)
def test_invalid_configuration_rejected(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_config(path=path, runtime_dir=tmp_path / "runtime")


def test_runtime_settings_are_audited_and_cache_invalidated(service) -> None:
    effective = service.settings.update(
        {
            "sending.dry_run": "false",
            "sending.enabled": "true",
            "scan.poll_interval_seconds": 45,
            "allowlist.test": ["tester@example.com"],
        },
        actor="test-admin",
        reason="unit-test",
    )
    assert effective.sending.dry_run is False
    assert effective.sending.enabled is True
    assert effective.poll_interval_seconds == 45

    audit = service.settings.audit(limit=20)
    keys = {item["key"] for item in audit}
    assert {"sending.dry_run", "sending.enabled", "scan.poll_interval_seconds"} <= keys
    assert all(item["actor"] == "test-admin" for item in audit)
    assert all(item["reason"] == "unit-test" for item in audit)
    # Audit payloads contain configuration values, never credentials.
    serialized = json.dumps(audit, ensure_ascii=False)
    assert "password" not in serialized.lower()

    # Simulate another process writing a new audited version.
    service.db.insert(
        "INSERT INTO config_versions(key, value_json, previous_json, actor, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            "sending.dry_run",
            json.dumps(True),
            json.dumps(False),
            "other-process",
            "unit-test",
            "2026-01-01T00:00:00Z",
        ),
    )
    assert service.settings.current().sending.dry_run is True


def test_enabling_send_with_empty_active_allowlist_is_rejected(service) -> None:
    with pytest.raises(ValidationError, match="empty active allowlist"):
        service.settings.update(
            {"sending.enabled": True, "allowlist.test": []},
            actor="test-admin",
        )


def test_production_switch_requires_explicit_ack(service) -> None:
    with pytest.raises(ValidationError, match="explicit confirmation"):
        service.settings.update(
            {
                "allowlist.mode": "production",
                "allowlist.production": ["prod@example.com"],
            },
            actor="test-admin",
        )
    updated = service.settings.update(
        {
            "allowlist.mode": "production",
            "allowlist.production": ["prod@example.com"],
        },
        actor="test-admin",
        production_ack=True,
    )
    assert updated.allowlist.mode == "production"
    assert updated.allowlist.active() == {"prod@example.com"}


def test_web_secret_is_created_restrictively(config: AppConfig, service) -> None:
    from cater.app.web.security import ensure_web_secret

    secret = ensure_web_secret(config.web_secret_path)
    path = Path(config.web_secret_path)
    assert path.is_file()
    assert len(secret) >= 32
    assert path.read_text(encoding="ascii").strip() == secret
    assert path.stat().st_mode & 0o077 == 0