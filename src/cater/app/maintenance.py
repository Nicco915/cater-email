"""Operational maintenance: consistent backups, restore, retention and diagnostics.

All secrets are excluded by construction: configuration summaries contain
booleans and counts only, and log records have already passed through the
redacting logger. Backups are SQLite consistency backups, not file copies.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .db import Database
from .errors import ValidationError
from .logging import JsonlLogger
from .util import is_within, parse_utc, utc_now

DIAGNOSTICS_LOG_LIMIT = 5000


class MaintenanceService:
    def __init__(
        self,
        *,
        db: Database,
        config: AppConfig,
        logger: JsonlLogger,
        health_provider=None,
        recovery_provider=None,
    ) -> None:
        self.db = db
        self.config = config
        self.logger = logger
        self._health_provider = health_provider
        self._recovery_provider = recovery_provider
        self._last_daily: datetime | None = None

    # ------------------------------------------------------------------
    # backups
    # ------------------------------------------------------------------
    def backup(self, reason: str = "manual") -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_reason = "".join(
            character if character.isalnum() or character in {"-", "_"} else "-"
            for character in reason
        )[:40]
        path = self.config.backups_dir / f"cater-{timestamp}-{safe_reason}.sqlite3"
        self.db.backup_to(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        verification = self.verify_backup(path)
        if not verification["ok"]:
            raise ValidationError(f"Backup verification failed: {verification['detail']}")
        pruned = self._prune_backups(keep_current=path)
        self.logger.info(
            "database_backup_created",
            path=str(path),
            reason=reason,
            schema_version=verification["schema_version"],
            task_count=verification["task_count"],
            pruned=[str(item) for item in pruned],
        )
        return path

    def _prune_backups(self, *, keep_current: Path | None = None) -> list[Path]:
        keep = max(1, int(self.config.logging.backup_retention))
        backups = sorted(
            (path for path in self.config.backups_dir.glob("cater-*.sqlite3") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removed: list[Path] = []
        for path in backups[keep:]:
            if keep_current is not None and path == keep_current:
                continue
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
        return removed

    def verify_backup(self, path: str | Path) -> dict[str, Any]:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            check_row = connection.execute("PRAGMA quick_check").fetchone()
            quick_check = str(check_row[0]) if check_row else "unknown"
            schema_version = 0
            task_count = 0
            try:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
                schema_version = int(row["version"]) if row else 0
                row = connection.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
                task_count = int(row["n"]) if row else 0
            except sqlite3.Error:
                pass
            return {
                "ok": quick_check == "ok",
                "quick_check": quick_check,
                "schema_version": schema_version,
                "task_count": task_count,
                "detail": "ok" if quick_check == "ok" else quick_check,
            }
        except sqlite3.Error as exc:
            return {
                "ok": False,
                "quick_check": "error",
                "schema_version": 0,
                "task_count": 0,
                "detail": str(exc),
            }
        finally:
            connection.close()

    def restore(self, path: str | Path, *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise ValidationError(
                "Restore requires explicit confirmation; stop the service and pass --yes"
            )
        source = Path(path)
        verification = self.verify_backup(source)
        if not verification["ok"]:
            raise ValidationError(f"Refusing to restore an invalid backup: {verification['detail']}")
        safety = self.backup("pre-restore")
        self.db.restore_from(source)
        self.logger.warning(
            "database_restored",
            source=str(source),
            safety_backup=str(safety),
            schema_version=verification["schema_version"],
        )
        return {"restored_from": str(source), "safety_backup": str(safety), **verification}

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------
    def cleanup_logs(self, now: datetime | None = None) -> list[Path]:
        removed = self.logger.cleanup(now=now)
        if removed:
            self.logger.info("logs_cleanup", removed=[str(item) for item in removed])
        return removed

    def cleanup_task_files(self, now: datetime | None = None) -> dict[str, Any]:
        """Delete old files for terminal tasks only.

        Open/waiting/retry tasks are never touched. Database rows remain as an
        audit trail; only runtime task directories are removed. The current
        service must be stopped (or the task idle) for this to be safe.
        """

        reference = now or datetime.now(timezone.utc)
        cutoff = reference - timedelta(days=self.config.logging.task_file_retention_days)
        rows = self.db.fetchall(
            "SELECT id, status, updated_at FROM tasks WHERE status IN ('COMPLETED', 'CANCELLED')"
        )
        removed: list[str] = []
        skipped: list[str] = []
        for row in rows:
            task_id = str(row["id"])
            updated = parse_utc(row["updated_at"])
            if updated is None or updated >= cutoff:
                skipped.append(task_id)
                continue
            directory = self.config.tasks_dir / task_id
            if not directory.exists():
                continue
            if not is_within(directory, self.config.tasks_dir):
                skipped.append(task_id)
                continue
            try:
                shutil.rmtree(directory)
            except OSError:
                skipped.append(task_id)
                continue
            removed.append(task_id)
            try:
                self.db.insert(
                    """
                    INSERT INTO task_events (
                        task_id, version, stage, code, detail_json, actor, created_at
                    ) VALUES (?, 0, 'cleanup', 'task_files_removed', ?, 'cleanup', ?)
                    """,
                    (
                        task_id,
                        json.dumps(
                            {"cutoff": cutoff.isoformat(), "retention_days": self.config.logging.task_file_retention_days},
                            ensure_ascii=False,
                        ),
                        utc_now(),
                    ),
                )
            except sqlite3.Error:
                pass
        if removed:
            self.logger.info(
                "task_files_cleanup",
                removed=removed,
                retention_days=self.config.logging.task_file_retention_days,
            )
        return {"removed": removed, "skipped": skipped, "cutoff": cutoff.isoformat()}

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    def export_diagnostics(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.config.diagnostics_dir / f"cater-diagnostics-{timestamp}.zip"
        health = self._health_provider() if self._health_provider else {}
        recovery = self._recovery_provider() if self._recovery_provider else {}
        tasks = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT id, owner_name, kind, status, version, destination_alpha3,
                       content_hash, last_error, created_at, updated_at
                  FROM tasks ORDER BY updated_at DESC LIMIT 500
                """
            )
        ]
        issues = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT task_id, version, code, record_key, field, message,
                       required_action, blocking, status, created_at
                  FROM task_issues ORDER BY id DESC LIMIT 2000
                """
            )
        ]
        logs = self.logger.search(limit=DIAGNOSTICS_LOG_LIMIT)
        manifest = {
            "generated_at": utc_now(),
            "schema_version": self.db.schema_version(),
            "quick_check": self.db.quick_check(),
            "health": health,
            "recovery": recovery,
            "note": "Secrets, credentials, mail bodies and attachment contents are excluded.",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
            )
            archive.writestr(
                "config-summary.json",
                json.dumps(self.config_summary(), ensure_ascii=False, indent=2, default=str),
            )
            archive.writestr(
                "tasks.json",
                json.dumps(tasks, ensure_ascii=False, indent=2, default=str),
            )
            archive.writestr(
                "issues.json",
                json.dumps(issues, ensure_ascii=False, indent=2, default=str),
            )
            archive.writestr(
                "logs.jsonl",
                "\n".join(
                    json.dumps(record, ensure_ascii=False, default=str) for record in logs
                )
                + ("\n" if logs else ""),
            )
            archive.writestr(
                "README.txt",
                "CATER local diagnostics.\n"
                "Contains operational metadata and redacted logs only.\n"
                "It does not contain credentials, raw e-mail bodies or attachment contents.\n",
            )
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._prune_diagnostics(keep_current=path)
        self.logger.info(
            "diagnostics_exported",
            path=str(path),
            task_rows=len(tasks),
            issue_rows=len(issues),
            log_rows=len(logs),
        )
        return path

    def _prune_diagnostics(self, *, keep_current: Path | None = None) -> None:
        keep = max(1, int(self.config.logging.backup_retention))
        files = sorted(
            (path for path in self.config.diagnostics_dir.glob("cater-diagnostics-*.zip") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[keep:]:
            if keep_current is not None and path == keep_current:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def config_summary(self) -> dict[str, Any]:
        # Local import keeps the maintenance module free of a settings cycle.
        from .settings import SettingsService

        settings = SettingsService(self.db, self.logger, self.config).current()
        return {
            "runtime_dir": str(self.config.paths.runtime_dir),
            "mail": {
                "account_configured": bool(self.config.mail.account),
                "folder": self.config.mail.folder,
                "sent_folder": self.config.mail.sent_folder,
                "imap_host_configured": bool(self.config.mail.imap_host),
                "smtp_host_configured": bool(self.config.mail.smtp_host),
                "imap_ssl": self.config.mail.imap_ssl,
                "smtp_ssl": self.config.mail.smtp_ssl,
                "poll_interval_seconds": settings.poll_interval_seconds,
                "max_attachment_bytes": self.config.mail.max_attachment_bytes,
                "max_total_attachment_bytes": self.config.mail.max_total_attachment_bytes,
            },
            "sending": {
                "dry_run": settings.sending.dry_run,
                "enabled": settings.sending.enabled,
            },
            "allowlist": {
                "mode": settings.allowlist.mode,
                "test_count": len(settings.allowlist.test),
                "production_count": len(settings.allowlist.production),
            },
            "web": {"host": self.config.web.host, "port": self.config.web.port},
            "logging": {
                "retention_days": self.config.logging.retention_days,
                "backup_retention": self.config.logging.backup_retention,
                "task_file_retention_days": self.config.logging.task_file_retention_days,
            },
        }

    # ------------------------------------------------------------------
    # daily hook for the poller
    # ------------------------------------------------------------------
    def maybe_daily(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if self._last_daily is not None and now - self._last_daily < timedelta(hours=24):
            return {"skipped": True}
        result: dict[str, Any] = {}
        try:
            result["backup"] = str(self.backup("daily"))
        except Exception as exc:  # maintenance must not stop polling
            self.logger.exception("daily_backup_failed", exc)
            result["backup_error"] = f"{type(exc).__name__}: {exc}"
        result["logs_removed"] = [str(path) for path in self.cleanup_logs()]
        self._last_daily = now
        return result