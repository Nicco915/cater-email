"""Structured JSONL application logs with daily/size rotation and redaction.

Logs intentionally contain identifiers rather than full message bodies or
credentials. ``task_events`` in SQLite remains the durable business history.
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .util import utc_now

_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "credential",
    "api_key",
    "apikey",
    "oauth",
)
_OMIT_KEY_PARTS = (
    "raw_email",
    "raw_body",
    "mail_body",
    "html_body",
    "text_body",
    "message_body",
)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|authorization|api[_-]?key)\b(\s*[=:]\s*)([^\s,;]+)"
)
_MAX_STRING = 4000


def _redact(value: Any, key: str = "") -> Any:
    lower = key.casefold()
    if any(part in lower for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if lower in {"body", "html", "text", "raw"} or any(part in lower for part in _OMIT_KEY_PARTS):
        return "[OMITTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key) for item in value]
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            value = value[:_MAX_STRING] + "…[truncated]"
        return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    if isinstance(value, Path):
        return str(value)
    return value


class JsonlLogger:
    def __init__(
        self,
        logs_dir: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        retention_days: int = 90,
        also_stderr: bool = False,
    ) -> None:
        self.logs_dir = Path(logs_dir)
        self.max_bytes = max(1024, int(max_bytes))
        self.retention_days = max(1, int(retention_days))
        self.also_stderr = also_stderr
        self._lock = threading.RLock()

    def _current_path(self) -> Path:
        return self.logs_dir / f"app-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    def _rotate_if_needed(self, path: Path) -> None:
        try:
            if path.exists() and path.stat().st_size >= self.max_bytes:
                for index in range(20, 0, -1):
                    older = path.with_name(f"{path.name}.{index}")
                    newer = path.with_name(f"{path.name}.{index + 1}")
                    if older.exists():
                        if index >= 20:
                            older.unlink(missing_ok=True)
                        else:
                            os.replace(older, newer)
                os.replace(path, path.with_name(f"{path.name}.1"))
        except OSError:
            # Logging must never break processing; the next record retries.
            pass

    def log(self, level: str, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": utc_now(),
            "level": level.upper(),
            "event": event,
        }
        for key, value in fields.items():
            if value is not None:
                record[key] = _redact(value, key)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            path = self._current_path()
            self._rotate_if_needed(path)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
            if self.also_stderr:
                try:
                    print(line, flush=True)
                except OSError:
                    pass
        return record

    def debug(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("DEBUG", event, **fields)

    def info(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("INFO", event, **fields)

    def warning(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("WARNING", event, **fields)

    def error(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("ERROR", event, **fields)

    def exception(self, event: str, exc: BaseException, **fields: Any) -> dict[str, Any]:
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return self.log(
            "ERROR",
            event,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=_SECRET_PATTERN.sub(
                lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", stack
            ),
            **fields,
        )

    def rotate(self) -> None:
        with self._lock:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(self._current_path())

    def cleanup(self, now: datetime | None = None) -> list[Path]:
        """Delete rotated/current log files older than retention."""

        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=self.retention_days)
        removed: list[Path] = []
        if not self.logs_dir.exists():
            return removed
        for path in self.logs_dir.glob("app-*.jsonl*"):
            if not path.is_file():
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if modified < cutoff:
                try:
                    path.unlink()
                    removed.append(path)
                except OSError:
                    pass
        return removed

    def search(
        self,
        *,
        task_id: str | None = None,
        mail_id: str | None = None,
        level: str | None = None,
        event: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Search known log files only.

        Callers cannot provide a path. Results are newest-first and bounded.
        """

        limit = max(1, min(int(limit), 1000))
        needle = (query or "").casefold()
        results: list[dict[str, Any]] = []
        if not self.logs_dir.exists():
            return results
        paths = sorted(
            (p for p in self.logs_dir.glob("app-*.jsonl*") if p.is_file()),
            key=lambda p: (p.name, p.stat().st_mtime),
            reverse=True,
        )
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if task_id and record.get("task_id") != task_id:
                    continue
                if mail_id and record.get("mail_id") != mail_id:
                    continue
                if level and str(record.get("level", "")).upper() != level.upper():
                    continue
                if event and event.casefold() not in str(record.get("event", "")).casefold():
                    continue
                if needle and needle not in json.dumps(record, ensure_ascii=False).casefold():
                    continue
                results.append(record)
                if len(results) >= limit:
                    return results
        return results

    def files(self) -> list[Path]:
        if not self.logs_dir.exists():
            return []
        return sorted(
            (p for p in self.logs_dir.glob("app-*.jsonl*") if p.is_file()),
            key=lambda p: p.name,
            reverse=True,
        )


def redact_for_export(value: Any) -> Any:
    """Public helper used by diagnostics export and configuration views."""

    return _redact(value)


def iter_log_records(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue