from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cater.app import state


def _submit(service, source: Path) -> dict:
    return service.submit_offline_sources(
        sender="tester@example.com",
        destination="法国",
        source_paths=[source],
    )


def test_backup_is_verified_and_reopens(service) -> None:
    path = service.maintenance.backup("test")
    assert path.is_file()
    verification = service.maintenance.verify_backup(path)
    assert verification["ok"] is True
    assert verification["quick_check"] == "ok"
    assert verification["schema_version"] == service.db.schema_version()

    import sqlite3

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        connection.close()


def test_diagnostics_excludes_secret_values(service) -> None:
    service.logger.info("redaction_check", password="super-secret-value", note="visible")
    path = service.maintenance.export_diagnostics()
    assert path.is_file()
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "config-summary.json", "tasks.json", "issues.json"} <= names
        logs = archive.read("logs.jsonl").decode("utf-8")
        manifest = archive.read("manifest.json").decode("utf-8")
    assert "super-secret-value" not in logs
    assert "[REDACTED]" in logs
    assert "password" not in manifest
    assert "super-secret-value" not in manifest


def test_task_file_cleanup_only_removes_old_terminal_tasks(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    old = task["id"]
    task_dir = service.config.tasks_dir / old
    assert task_dir.exists()

    service.db.insert(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (state.COMPLETED, "2020-01-01T00:00:00Z", old),
    )
    result = service.maintenance.cleanup_task_files()
    assert old in result["removed"]
    assert not task_dir.exists()
    # The audit row remains even though the file tree is gone.
    assert service.tasks.require(old)["id"] == old

    other = _submit(service, offline_source)
    result = service.maintenance.cleanup_task_files()
    assert other["id"] not in result["removed"]
    assert (service.config.tasks_dir / other["id"]).exists()


def test_log_cleanup_removes_only_expired_files(service) -> None:
    service.logger.info("cleanup_check")
    log_files = service.logger.files()
    assert log_files
    current = log_files[0]
    old = current.with_name("app-2000-01-01.jsonl.1")
    old.write_text("{}\n", encoding="utf-8")
    stale = (datetime.now(timezone.utc) - timedelta(days=365)).timestamp()
    os.utime(old, (stale, stale))

    removed = service.maintenance.cleanup_logs()
    assert old in removed
    assert current.exists()