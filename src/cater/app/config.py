"""Configuration loading, validation and credential lookup.

Secrets are never stored in ``config.toml``. They are read from a local .env,
OS environment variables or the Windows Credential Manager. Configuration
values may be overridden at runtime (with audit history) through the admin
service; those overrides live in SQLite, not in this file.
"""

from __future__ import annotations

import ctypes
import os
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, CredentialError
from .util import EMAIL_RE, normalize_email

DEFAULT_CONFIG_PATH = Path("config/config.toml")
DEFAULT_RUNTIME_DIR = Path("runtime")

IMAP_PASSWORD_ENV = "CATER_IMAP_PASSWORD"
SMTP_PASSWORD_ENV = "CATER_SMTP_PASSWORD"
IMAP_CREDENTIAL_TARGET = "Cater/IMAP"
SMTP_CREDENTIAL_TARGET = "Cater/SMTP"


def _mail_environment() -> dict[str, str]:
    """Read the working-directory .env as literal values; OS environment wins.

    No shell evaluation or variable interpolation. Passwords never enter AppConfig.
    """
    values: dict[str, str] = {}
    env_path = Path(os.environ.get("CATER_ENV_FILE", ".env"))
    if env_path.is_file():
        try:
            lines = env_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise ConfigurationError("Cannot read mail environment file") from exc
        for number, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not key.startswith("CATER_"):
                raise ConfigurationError(f"Invalid environment file entry at line {number}")
            if value.startswith((chr(34), chr(39))):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ConfigurationError(f"Invalid environment file quoting at line {number}")
                value = value[1:-1]
            values[key] = value
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class MailConfig:
    account: str = ""
    folder: str = "INBOX"
    sent_folder: str = "Sent"
    imap_host: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    imap_starttls: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_ssl: bool = True
    smtp_starttls: bool = False
    smtp_timeout_seconds: int = 30
    poll_interval_seconds: int = 60
    first_run_new_only: bool = True
    max_attachment_bytes: int = 25 * 1024 * 1024
    max_total_attachment_bytes: int = 50 * 1024 * 1024
    max_message_bytes: int = 60 * 1024 * 1024

    def addresses(self) -> set[str]:
        result: set[str] = set()
        if self.account:
            result.add(normalize_email(self.account))
        return result


@dataclass(frozen=True)
class AllowlistConfig:
    # "test" only honours test addresses; "production" only honours production.
    mode: str = "test"
    test: tuple[str, ...] = ()
    production: tuple[str, ...] = ()

    def active(self) -> frozenset[str]:
        source = self.production if self.mode == "production" else self.test
        return frozenset(normalize_email(item) for item in source if item)

    def contains(self, sender: str) -> bool:
        return normalize_email(sender) in self.active()


@dataclass(frozen=True)
class SendingConfig:
    # Factory defaults: offline. Two independent switches plus allowlist mode
    # must all permit live sending.
    dry_run: bool = True
    enabled: bool = False

    def live_allowed(self, allowlist: AllowlistConfig) -> bool:
        return (not self.dry_run) and self.enabled and allowlist.mode in {"test", "production"}


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    # CSRF/session signing secret lives in runtime/, not in this config.
    session_max_age_seconds: int = 8 * 60 * 60


@dataclass(frozen=True)
class LoggingConfig:
    max_bytes: int = 10 * 1024 * 1024
    retention_days: int = 90
    backup_retention: int = 30
    task_file_retention_days: int = 365


@dataclass(frozen=True)
class PathsConfig:
    runtime_dir: Path
    config_path: Path | None = None


@dataclass(frozen=True)
class AppConfig:
    paths: PathsConfig
    mail: MailConfig = field(default_factory=MailConfig)
    allowlist: AllowlistConfig = field(default_factory=AllowlistConfig)
    sending: SendingConfig = field(default_factory=SendingConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    # ----- derived paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.paths.runtime_dir / "cater.sqlite3"

    @property
    def logs_dir(self) -> Path:
        return self.paths.runtime_dir / "logs"

    @property
    def mail_dir(self) -> Path:
        return self.paths.runtime_dir / "mail"

    @property
    def tasks_dir(self) -> Path:
        return self.paths.runtime_dir / "tasks"

    @property
    def backups_dir(self) -> Path:
        return self.paths.runtime_dir / "backups"

    @property
    def diagnostics_dir(self) -> Path:
        return self.paths.runtime_dir / "diagnostics"

    @property
    def web_secret_path(self) -> Path:
        return self.paths.runtime_dir / "web-secret"

    def with_settings(
        self,
        *,
        allowlist: AllowlistConfig | None = None,
        sending: SendingConfig | None = None,
        poll_interval_seconds: int | None = None,
    ) -> "AppConfig":
        mail = self.mail
        if poll_interval_seconds is not None:
            mail = replace(mail, poll_interval_seconds=poll_interval_seconds)
        return replace(
            self,
            mail=mail,
            allowlist=allowlist or self.allowlist,
            sending=sending or self.sending,
        )


def _coerce_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{key} must be a boolean")


def _coerce_int(value: Any, key: str, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be an integer") from exc
    if number < minimum:
        raise ConfigurationError(f"{key} must be >= {minimum}")
    return number


def _coerce_emails(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigurationError(f"{key} must be a list of e-mail addresses")
    result: list[str] = []
    for raw in value:
        address = normalize_email(str(raw))
        if not address or not EMAIL_RE.match(address):
            raise ConfigurationError(f"{key} contains an invalid e-mail address")
        if address not in result:
            result.append(address)
    return tuple(result)


def load_config(
    path: str | Path | None = None,
    runtime_dir: str | Path | None = None,
) -> AppConfig:
    """Load TOML plus environment path overrides.

    A missing configuration file is valid: the application starts offline with
    empty allowlists and live sending disabled.
    """

    environment = _mail_environment()
    env_config = environment.get("CATER_CONFIG")
    chosen_path = Path(path or env_config or DEFAULT_CONFIG_PATH)
    runtime_env = environment.get("CATER_RUNTIME_DIR")
    chosen_runtime = Path(runtime_dir or runtime_env or DEFAULT_RUNTIME_DIR)

    raw: dict[str, Any] = {}
    if chosen_path.exists():
        try:
            with chosen_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"Cannot read configuration: {exc}") from exc
    elif path is not None or env_config:
        # Explicitly requested config must exist; the default may be absent.
        raise ConfigurationError(f"Configuration file not found: {chosen_path}")

    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a table")

    mail_raw = raw.get("mail", {})
    allow_raw = raw.get("allowlist", {})
    sending_raw = raw.get("sending", {})
    web_raw = raw.get("web", {})
    logging_raw = raw.get("logging", {})
    runtime_raw = raw.get("runtime", {})
    for name, value in (
        ("mail", mail_raw),
        ("allowlist", allow_raw),
        ("sending", sending_raw),
        ("web", web_raw),
        ("logging", logging_raw),
        ("runtime", runtime_raw),
    ):
        if not isinstance(value, dict):
            raise ConfigurationError(f"{name} must be a table")

    if runtime_raw.get("dir") and runtime_dir is None and not runtime_env:
        chosen_runtime = Path(str(runtime_raw["dir"]))

    # Mail server settings may be kept together with credentials in .env.
    mail_raw = dict(mail_raw)
    for field_name in MailConfig.__dataclass_fields__:
        env_key = "CATER_MAIL_ACCOUNT" if field_name == "account" else "CATER_" + field_name.upper()
        if env_key in environment:
            mail_raw[field_name] = environment[env_key]

    mail = MailConfig(
        account=str(mail_raw.get("account", "")).strip(),
        folder=str(mail_raw.get("folder", "INBOX")),
        sent_folder=str(mail_raw.get("sent_folder", "Sent")),
        imap_host=str(mail_raw.get("imap_host", "")),
        imap_port=_coerce_int(mail_raw.get("imap_port", 993), "mail.imap_port"),
        imap_ssl=_coerce_bool(mail_raw.get("imap_ssl", True), "mail.imap_ssl"),
        imap_starttls=_coerce_bool(mail_raw.get("imap_starttls", False), "mail.imap_starttls"),
        smtp_host=str(mail_raw.get("smtp_host", "")),
        smtp_port=_coerce_int(mail_raw.get("smtp_port", 465), "mail.smtp_port"),
        smtp_ssl=_coerce_bool(mail_raw.get("smtp_ssl", True), "mail.smtp_ssl"),
        smtp_starttls=_coerce_bool(mail_raw.get("smtp_starttls", False), "mail.smtp_starttls"),
        smtp_timeout_seconds=_coerce_int(
            mail_raw.get("smtp_timeout_seconds", 30), "mail.smtp_timeout_seconds"
        ),
        poll_interval_seconds=_coerce_int(
            mail_raw.get("poll_interval_seconds", 60), "mail.poll_interval_seconds", minimum=5
        ),
        first_run_new_only=_coerce_bool(
            mail_raw.get("first_run_new_only", True), "mail.first_run_new_only"
        ),
        max_attachment_bytes=_coerce_int(
            mail_raw.get("max_attachment_bytes", 25 * 1024 * 1024),
            "mail.max_attachment_bytes",
        ),
        max_total_attachment_bytes=_coerce_int(
            mail_raw.get("max_total_attachment_bytes", 50 * 1024 * 1024),
            "mail.max_total_attachment_bytes",
        ),
        max_message_bytes=_coerce_int(
            mail_raw.get("max_message_bytes", 60 * 1024 * 1024), "mail.max_message_bytes"
        ),
    )
    if mail.account and not EMAIL_RE.match(normalize_email(mail.account)):
        raise ConfigurationError("mail.account is not a valid e-mail address")
    if mail.imap_ssl and mail.imap_starttls:
        raise ConfigurationError("mail.imap_ssl and mail.imap_starttls are mutually exclusive")
    if mail.smtp_ssl and mail.smtp_starttls:
        raise ConfigurationError("mail.smtp_ssl and mail.smtp_starttls are mutually exclusive")

    mode = str(allow_raw.get("mode", "test")).strip().lower()
    if mode not in {"test", "production"}:
        raise ConfigurationError("allowlist.mode must be 'test' or 'production'")
    allowlist = AllowlistConfig(
        mode=mode,
        test=_coerce_emails(allow_raw.get("test", []), "allowlist.test"),
        production=_coerce_emails(allow_raw.get("production", []), "allowlist.production"),
    )

    sending = SendingConfig(
        dry_run=_coerce_bool(sending_raw.get("dry_run", True), "sending.dry_run"),
        enabled=_coerce_bool(sending_raw.get("enabled", False), "sending.enabled"),
    )

    web = WebConfig(
        host=str(web_raw.get("host", "127.0.0.1")).strip(),
        port=_coerce_int(web_raw.get("port", 8765), "web.port"),
        session_max_age_seconds=_coerce_int(
            web_raw.get("session_max_age_seconds", 8 * 60 * 60),
            "web.session_max_age_seconds",
        ),
    )
    logging_cfg = LoggingConfig(
        max_bytes=_coerce_int(logging_raw.get("max_bytes", 10 * 1024 * 1024), "logging.max_bytes"),
        retention_days=_coerce_int(logging_raw.get("retention_days", 90), "logging.retention_days"),
        backup_retention=_coerce_int(
            logging_raw.get("backup_retention", 30), "logging.backup_retention"
        ),
        task_file_retention_days=_coerce_int(
            logging_raw.get("task_file_retention_days", 365),
            "logging.task_file_retention_days",
        ),
    )

    config = AppConfig(
        paths=PathsConfig(runtime_dir=chosen_runtime, config_path=chosen_path if chosen_path.exists() else None),
        mail=mail,
        allowlist=allowlist,
        sending=sending,
        web=web,
        logging=logging_cfg,
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    loopback = {"127.0.0.1", "::1", "localhost"}
    if config.web.host not in loopback:
        raise ConfigurationError(
            "web.host must be a loopback address (127.0.0.1 or ::1); LAN binding is out of scope"
        )
    if config.mail.imap_ssl and config.mail.imap_starttls:
        raise ConfigurationError("IMAP SSL and STARTTLS cannot both be enabled")
    if config.mail.smtp_ssl and config.mail.smtp_starttls:
        raise ConfigurationError("SMTP SSL and STARTTLS cannot both be enabled")
    if config.sending.enabled and config.sending.dry_run:
        # Not fatal: the user may pre-enable while still in dry-run. It is
        # simply still offline, but all other validation still applies.
        pass
    if config.allowlist.mode == "production" and not config.allowlist.production:
        raise ConfigurationError(
            "production allowlist mode requires at least one production address"
        )


def ensure_runtime_dirs(config: AppConfig) -> None:
    for directory in (
        config.paths.runtime_dir,
        config.logs_dir,
        config.mail_dir,
        config.tasks_dir,
        config.backups_dir,
        config.diagnostics_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    # Best-effort restrictive permissions for local secrets and data.
    try:
        os.chmod(config.paths.runtime_dir, 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Windows Credential Manager (generic credentials) and environment fallback
# ---------------------------------------------------------------------------


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_ulonglong),
        ("CredentialBlobSize", ctypes.c_ulong),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_ulong),
        ("AttributeCount", ctypes.c_ulong),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _wincred_read(target: str) -> str | None:
    if sys.platform != "win32":
        return None
    advapi32 = ctypes.windll.advapi32
    pointer = ctypes.POINTER(_CredentialW)()
    CRED_TYPE_GENERIC = 1
    if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        return None
    try:
        credential = pointer.contents
        size = int(credential.CredentialBlobSize)
        if size <= 0 or not credential.CredentialBlob:
            return None
        data = ctypes.string_at(credential.CredentialBlob, size)
        return data.decode("utf-16-le", errors="strict")
    finally:
        advapi32.CredFree(pointer)


def _wincred_write(target: str, username: str, secret: str) -> None:
    if sys.platform != "win32":
        raise CredentialError("Windows Credential Manager is only available on Windows")
    encoded = secret.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = _CredentialW()
    credential.Flags = 0
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.Comment = "CATER mail credential"
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    credential.AttributeCount = 0
    credential.Attributes = None
    credential.TargetAlias = None
    credential.UserName = username or target
    if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialError("Windows Credential Manager rejected the credential")


class CredentialProvider:
    """Resolve local .env/OS secrets or Windows credentials without logging them."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else _mail_environment()

    def imap_password(self, account: str) -> str | None:
        return self._read(IMAP_PASSWORD_ENV, IMAP_CREDENTIAL_TARGET, account)

    def smtp_password(self, account: str) -> str | None:
        return self._read(SMTP_PASSWORD_ENV, SMTP_CREDENTIAL_TARGET, account)

    def describe(self, account: str) -> dict[str, bool]:
        return {
            "imap_env": bool(self._environ.get(IMAP_PASSWORD_ENV)),
            "smtp_env": bool(self._environ.get(SMTP_PASSWORD_ENV)),
            "imap_windows_store": self.imap_password(account) is not None,
            "smtp_windows_store": self.smtp_password(account) is not None,
        }

    def _read(self, env_name: str, target: str, account: str) -> str | None:
        value = self._environ.get(env_name)
        if value:
            return value
        return _wincred_read(target)

    @staticmethod
    def store_windows(service: str, username: str, secret: str) -> None:
        target = {"imap": IMAP_CREDENTIAL_TARGET, "smtp": SMTP_CREDENTIAL_TARGET}.get(service)
        if not target:
            raise CredentialError("service must be 'imap' or 'smtp'")
        if not secret:
            raise CredentialError("secret must not be empty")
        _wincred_write(target, username, secret)