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
    # The settings rows the test just wrote carry this actor/reason. Other
    # audit rows (for example the one-time, system-actor reference seed that
    # marks the database authoritative) are intentionally present too.
    settings_rows = [
        item
        for item in audit
        if item["key"]
        in {"sending.dry_run", "sending.enabled", "scan.poll_interval_seconds"}
    ]
    assert settings_rows
    assert all(item["actor"] == "test-admin" for item in settings_rows)
    assert all(item["reason"] == "unit-test" for item in settings_rows)
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

def test_dotenv_mail_and_credentials_are_loaded_without_global_mutation(tmp_path, monkeypatch):
    from cater.app.config import CredentialProvider
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('CATER_IMAP_PASSWORD', raising=False)
    monkeypatch.delenv('CATER_SMTP_PASSWORD', raising=False)
    (tmp_path / '.env').write_text(
        'CATER_MAIL_ACCOUNT=bot@example.com\n'
        'CATER_IMAP_HOST=imap.example.com\nCATER_IMAP_PORT=1993\n'
        'CATER_SMTP_HOST=smtp.example.com\nCATER_SMTP_SSL=false\n'
        'CATER_SMTP_STARTTLS=true\n'
        'CATER_IMAP_PASSWORD="literal # $VALUE secret"\n'
        "CATER_SMTP_PASSWORD='smtp secret'\n", encoding='utf-8')
    config = load_config(runtime_dir=tmp_path / 'runtime')
    assert config.mail.account == 'bot@example.com'
    assert config.mail.imap_port == 1993
    assert config.mail.smtp_ssl is False
    assert config.mail.smtp_starttls is True
    provider = CredentialProvider()
    assert provider.imap_password(config.mail.account) == 'literal # $VALUE secret'
    assert provider.smtp_password(config.mail.account) == 'smtp secret'
    assert 'literal # $VALUE secret' not in repr(config)
    import os
    assert 'CATER_IMAP_PASSWORD' not in os.environ
    assert config.sending.dry_run and not config.sending.enabled


def test_dotenv_precedence_and_restart_reload(tmp_path, monkeypatch):
    from cater.app.config import CredentialProvider
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'config.toml'
    path.write_text('[mail]\nimap_host="toml.example.com"\n')
    env = tmp_path / '.env'
    env.write_text('CATER_IMAP_HOST=env.example.com\nCATER_IMAP_PASSWORD=first\n')
    monkeypatch.setenv('CATER_IMAP_HOST', 'os.example.com')
    assert load_config(path).mail.imap_host == 'os.example.com'
    monkeypatch.delenv('CATER_IMAP_HOST')
    assert load_config(path).mail.imap_host == 'env.example.com'
    assert CredentialProvider().imap_password('') == 'first'
    env.write_text('CATER_IMAP_PASSWORD=second\n')
    assert CredentialProvider().imap_password('') == 'second'
    assert load_config(path).mail.imap_host == 'toml.example.com'


def test_dotenv_parse_error_does_not_disclose_secret(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('CATER_IMAP_PASSWORD="do-not-disclose\n')
    with pytest.raises(ConfigurationError) as exc:
        load_config()
    assert 'do-not-disclose' not in str(exc.value)
