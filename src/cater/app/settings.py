"""Runtime settings, allowlist validation and configuration audit history."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from typing import Any

from .config import (
    AppConfig,
    AllowlistConfig,
    SendingConfig,
    _coerce_bool,
    _coerce_emails,
    _coerce_int,
    validate_config,
)
from .db import Database
from .errors import ValidationError
from .logging import JsonlLogger
from .util import utc_now

MODE_KEY = "allowlist.mode"
TEST_KEY = "allowlist.test"
PRODUCTION_KEY = "allowlist.production"
DRY_RUN_KEY = "sending.dry_run"
ENABLED_KEY = "sending.enabled"
POLL_KEY = "scan.poll_interval_seconds"

SETTING_KEYS = (MODE_KEY, TEST_KEY, PRODUCTION_KEY, DRY_RUN_KEY, ENABLED_KEY, POLL_KEY)


@dataclass(frozen=True)
class EffectiveSettings:
    allowlist: AllowlistConfig
    sending: SendingConfig
    poll_interval_seconds: int
    config: AppConfig

    def as_dict(self) -> dict[str, Any]:
        return {
            MODE_KEY: self.allowlist.mode,
            TEST_KEY: list(self.allowlist.test),
            PRODUCTION_KEY: list(self.allowlist.production),
            DRY_RUN_KEY: self.sending.dry_run,
            ENABLED_KEY: self.sending.enabled,
            POLL_KEY: self.poll_interval_seconds,
        }


class SettingsService:
    def __init__(self, db: Database, logger: JsonlLogger, base_config: AppConfig) -> None:
        self.db = db
        self.logger = logger
        self.base_config = base_config
        self._cached: EffectiveSettings | None = None
        self._cached_audit_id: int | None = None

    def _audit_version(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(MAX(id), 0) FROM config_versions") or 0)

    def _latest_rows(self) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in SETTING_KEYS)
        rows = self.db.fetchall(
            f"""
            SELECT key, value_json FROM config_versions
             WHERE id IN (
                 SELECT MAX(id) FROM config_versions
                  WHERE key IN ({placeholders}) GROUP BY key
             )
            """,
            SETTING_KEYS,
        )
        current: dict[str, Any] = {}
        for row in rows:
            try:
                current[str(row["key"])] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue
        return current

    def current(self) -> EffectiveSettings:
        audit_id = self._audit_version()
        if self._cached is not None and audit_id == self._cached_audit_id:
            return self._cached
        raw = self._latest_rows()
        allowlist = AllowlistConfig(
            mode=str(raw.get(MODE_KEY, self.base_config.allowlist.mode)),
            test=tuple(raw.get(TEST_KEY, list(self.base_config.allowlist.test))),
            production=tuple(
                raw.get(PRODUCTION_KEY, list(self.base_config.allowlist.production))
            ),
        )
        sending = SendingConfig(
            dry_run=_coerce_bool(
                raw.get(DRY_RUN_KEY, self.base_config.sending.dry_run), DRY_RUN_KEY
            ),
            enabled=_coerce_bool(
                raw.get(ENABLED_KEY, self.base_config.sending.enabled), ENABLED_KEY
            ),
        )
        try:
            poll = int(raw.get(POLL_KEY, self.base_config.mail.poll_interval_seconds))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{POLL_KEY} is not an integer") from exc
        config = replace(
            self.base_config,
            allowlist=allowlist,
            sending=sending,
            mail=replace(self.base_config.mail, poll_interval_seconds=poll),
        )
        self._cached = EffectiveSettings(
            allowlist=allowlist,
            sending=sending,
            poll_interval_seconds=poll,
            config=config,
        )
        self._cached_audit_id = audit_id
        return self._cached

    def refresh(self) -> EffectiveSettings:
        self._cached = None
        self._cached_audit_id = None
        return self.current()

    def update(
        self,
        changes: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
        production_ack: bool = False,
    ) -> EffectiveSettings:
        """Validate and persist audited settings changes transactionally."""

        current = self.current()
        values = current.as_dict()
        unknown = set(changes) - set(SETTING_KEYS)
        if unknown:
            raise ValidationError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")

        for key, value in changes.items():
            if key in {TEST_KEY, PRODUCTION_KEY}:
                values[key] = list(_coerce_emails(value, key))
            elif key == MODE_KEY:
                mode = str(value).strip().lower()
                if mode not in {"test", "production"}:
                    raise ValidationError("allowlist.mode must be 'test' or 'production'")
                values[key] = mode
            elif key in {DRY_RUN_KEY, ENABLED_KEY}:
                values[key] = _coerce_bool(value, key)
            elif key == POLL_KEY:
                values[key] = _coerce_int(value, key, minimum=5)

        if values[MODE_KEY] == "production" and not values[PRODUCTION_KEY]:
            raise ValidationError(
                "Cannot use production allowlist mode without production addresses"
            )
        if (
            values[MODE_KEY] == "production"
            and current.allowlist.mode != "production"
            and not production_ack
        ):
            raise ValidationError(
                "Switching to production mode requires an explicit confirmation"
            )
        if values[ENABLED_KEY]:
            active = values[PRODUCTION_KEY] if values[MODE_KEY] == "production" else values[TEST_KEY]
            if not active:
                raise ValidationError("Cannot enable sending with an empty active allowlist")

        new_allowlist = AllowlistConfig(
            mode=values[MODE_KEY],
            test=tuple(values[TEST_KEY]),
            production=tuple(values[PRODUCTION_KEY]),
        )
        new_sending = SendingConfig(dry_run=values[DRY_RUN_KEY], enabled=values[ENABLED_KEY])
        candidate = replace(
            self.base_config,
            allowlist=new_allowlist,
            sending=new_sending,
            mail=replace(
                self.base_config.mail, poll_interval_seconds=int(values[POLL_KEY])
            ),
        )
        validate_config(candidate)

        previous_values = current.as_dict()
        changed = {
            key: value
            for key, value in values.items()
            if value != previous_values.get(key)
        }
        if not changed:
            return current
        now = utc_now()
        with self.db.transaction() as conn:
            for key, value in changed.items():
                conn.execute(
                    """
                    INSERT INTO config_versions(
                        key, value_json, previous_json, actor, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        json.dumps(value, ensure_ascii=False),
                        json.dumps(previous_values.get(key), ensure_ascii=False),
                        actor,
                        reason,
                        now,
                    ),
                )
        # Audit log records changed key names, never credentials.
        self.logger.info(
            "configuration_updated",
            actor=actor,
            keys=sorted(changed),
            reason=reason,
            dry_run=values[DRY_RUN_KEY],
            allowlist_mode=values[MODE_KEY],
        )
        return self.refresh()

    def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM config_versions ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )
        result = []
        for row in rows:
            item = dict(row)
            for key in ("value_json", "previous_json"):
                raw = item.pop(key, None)
                try:
                    item[key.removesuffix("_json")] = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    item[key.removesuffix("_json")] = raw
            result.append(item)
        return result

    def active_senders(self) -> frozenset[str]:
        return self.current().allowlist.active()