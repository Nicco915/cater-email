"""Task persistence and status transitions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import state
from .db import Database
from .logging import JsonlLogger
from .util import new_id, normalize_key, utc_now

# Automatic retry policy for recoverable technical failures. ``retry_count``
# counts consecutive technical failures without a successful business outcome;
# while it stays below MAX_AUTO_RETRIES the poller re-attempts with backoff,
# afterwards the task becomes FAILED and the owner is notified exactly once
# per failure episode (``failure_notified``). Both counters persist in the
# database, so a process restart neither resets nor re-notifies.
MAX_AUTO_RETRIES = 3
RETRY_BACKOFF_SECONDS = (60, 300, 900)


def retry_backoff_seconds(failure_number: int) -> int:
    """Delay before retrying after ``failure_number`` consecutive failures."""

    index = max(1, min(int(failure_number), len(RETRY_BACKOFF_SECONDS))) - 1
    return RETRY_BACKOFF_SECONDS[index]


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class TaskRepository:
    def __init__(self, db: Database, logger: JsonlLogger) -> None:
        self.db = db
        self.logger = logger

    # ------------------------------------------------------------------
    # creation and lookup
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        owner: str,
        owner_name: str | None,
        kind: str,
        origin_mail_id: str,
        destination_name: str | None = None,
        destination_alpha3: str | None = None,
        status: str = state.RECEIVED,
    ) -> dict[str, Any]:
        task_id = new_id("task")
        now = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, owner, owner_name, kind, version, status, destination_name,
                    destination_alpha3, origin_mail_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    owner,
                    owner_name,
                    kind,
                    status,
                    destination_name,
                    destination_alpha3,
                    origin_mail_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_versions (
                    task_id, version, status, destination_name, destination_alpha3, created_at
                ) VALUES (?, 1, ?, ?, ?, ?)
                """,
                (task_id, status, destination_name, destination_alpha3, now),
            )
            conn.execute(
                """
                INSERT INTO task_events (
                    task_id, version, attempt_id, stage, from_status, to_status,
                    code, detail_json, actor, created_at
                ) VALUES (?, 1, NULL, 'intake', NULL, ?, 'task_created', ?, 'system', ?)
                """,
                (
                    task_id,
                    status,
                    json.dumps({"origin_mail_id": origin_mail_id, "kind": kind}, ensure_ascii=False),
                    now,
                ),
            )
        self.logger.info("task_created", task_id=task_id, mail_id=origin_mail_id, kind=kind)
        task = self.get(task_id)
        assert task is not None
        return task

    def get(self, task_id: str) -> dict[str, Any] | None:
        return _row(self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,)))

    def require(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def get_version(self, task_id: str, version: int) -> dict[str, Any] | None:
        return _row(
            self.db.fetchone(
                "SELECT * FROM task_versions WHERE task_id = ? AND version = ?", (task_id, version)
            )
        )

    def list(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        owner: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("t.status = ?")
            params.append(status)
        if kind:
            clauses.append("t.kind = ?")
            params.append(kind)
        if owner:
            clauses.append("t.owner = ?")
            params.append(owner)
        if search:
            clauses.append("(t.id LIKE ? OR m.subject LIKE ? OR t.business_key LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        rows = self.db.fetchall(
            f"""
            SELECT t.*, m.subject AS mail_subject, m.message_id AS origin_message_id,
                   (SELECT COUNT(*) FROM task_issues i
                     WHERE i.task_id = t.id AND i.version = t.version AND i.status = 'open') AS open_issues,
                   (SELECT COUNT(*) FROM outbox o
                     WHERE o.task_id = t.id AND o.version = t.version) AS outbox_count
              FROM tasks t
              LEFT JOIN mail_messages m ON m.id = t.origin_mail_id
              {where}
             ORDER BY t.updated_at DESC
             LIMIT ? OFFSET ?
            """,
            params,
        )
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.db.fetchall("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        return {str(row["status"]): int(row["n"]) for row in rows}

    def get_origin_mail(self, task_id: str) -> dict[str, Any] | None:
        return _row(
            self.db.fetchone(
                """
                SELECT m.* FROM mail_messages m
                  JOIN tasks t ON t.origin_mail_id = m.id
                 WHERE t.id = ?
                """,
                (task_id,),
            )
        )

    def find_by_content_hash(
        self, owner: str, kind: str, content_hash: str, *, exclude_task_id: str
    ) -> dict[str, Any] | None:
        return _row(
            self.db.fetchone(
                """
                SELECT * FROM tasks
                 WHERE owner = ? AND kind = ? AND content_hash = ? AND id <> ?
                 ORDER BY created_at DESC LIMIT 1
                """,
                (owner, kind, content_hash, exclude_task_id),
            )
        )

    def find_by_business_identity(
        self, owner: str, kind: str, identity: str, *, exclude_task_id: str
    ) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM tasks
             WHERE owner = ? AND kind = ? AND business_identity = ? AND id <> ?
             ORDER BY created_at DESC
            """,
            (owner, kind, identity, exclude_task_id),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    def find_previous_content_hash(
        self, task_id: str, version: int, content_hash: str
    ) -> dict[str, Any] | None:
        return _row(
            self.db.fetchone(
                """
                SELECT * FROM task_versions
                 WHERE task_id = ? AND version <> ? AND content_hash = ?
                 ORDER BY version DESC LIMIT 1
                """,
                (task_id, version, content_hash),
            )
        )

    def set_kind(self, task_id: str, kind: str, *, actor: str = "system") -> None:
        task = self.require(task_id)
        if task["kind"] != "auto" or kind not in {"parts", "machine"}:
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET kind = ?, updated_at = ? WHERE id = ?",
                (kind, utc_now(), task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events (
                    task_id, version, stage, code, detail_json, actor, created_at
                ) VALUES (?, ?, 'processing', 'kind_resolved', ?, ?, ?)
                """,
                (
                    task_id,
                    int(task["version"]),
                    json.dumps({"from_kind": "auto", "to_kind": kind}, ensure_ascii=False),
                    actor,
                    utc_now(),
                ),
            )

    def update_config_version(self, task_id: str, version_id: int) -> None:
        self.db.insert(
            "UPDATE tasks SET config_version = ?, updated_at = ? WHERE id = ?",
            (version_id, utc_now(), task_id),
        )

    def clear_artifacts(self, task_id: str, version: int) -> None:
        self.db.insert(
            "DELETE FROM task_artifacts WHERE task_id = ? AND version = ?", (task_id, version)
        )

    # status and events
    # ------------------------------------------------------------------
    def transition(
        self,
        task_id: str,
        target: str,
        *,
        stage: str,
        actor: str = "system",
        attempt_id: str | None = None,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if target not in state.ALL_STATUSES:
            raise ValueError(target)
        now = utc_now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = str(row["status"])
            version = int(row["version"])
            state.require_transition(current, target)
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (target, now, task_id),
            )
            conn.execute(
                "UPDATE task_versions SET status = ? WHERE task_id = ? AND version = ?",
                (target, task_id, version),
            )
            conn.execute(
                """
                INSERT INTO task_events (
                    task_id, version, attempt_id, stage, from_status, to_status,
                    code, detail_json, actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    version,
                    attempt_id,
                    stage,
                    current,
                    target,
                    code,
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                    actor,
                    now,
                ),
            )
        self.logger.info(
            "task_status_changed",
            task_id=task_id,
            version=version,
            from_status=current,
            to_status=target,
            code=code,
        )
        return self.require(task_id)

    def add_event(
        self,
        task_id: str,
        *,
        stage: str,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        actor: str = "system",
        attempt_id: str | None = None,
        version: int | None = None,
    ) -> None:
        task = self.require(task_id)
        version = int(version or task["version"])
        self.db.insert(
            """
            INSERT INTO task_events (
                task_id, version, attempt_id, stage, from_status, to_status,
                code, detail_json, actor, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                task_id,
                version,
                attempt_id,
                stage,
                code,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                actor,
                utc_now(),
            ),
        )

    def set_retry_state(
        self,
        task_id: str,
        *,
        status: str,
        error: str,
        stage: str,
        actor: str = "system",
        attempt_id: str | None = None,
        code: str = "processing_error",
        detail: dict[str, Any] | None = None,
        next_retry_at: str | None = None,
        count_attempt: bool = True,
    ) -> dict[str, Any]:
        """Record a processing failure and schedule or exhaust automatic retry.

        ``RETRY_PENDING`` consumes retry budget: the consecutive-failure
        counter is incremented and, once it exceeds ``MAX_AUTO_RETRIES``, the
        task moves to ``FAILED`` instead (no further automatic attempts).
        ``FAILED`` records the terminal failure without scheduling. Both
        transitions are audited; the caller is responsible for notifying the
        owner when the returned task is ``FAILED`` and not yet
        ``failure_notified``.
        """

        if status not in {state.RETRY_PENDING, state.FAILED, state.WAITING_INPUT}:
            raise ValueError(status)
        now = utc_now()
        failures = 0
        if count_attempt:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT retry_count FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                failures = int(row["retry_count"]) + 1
            if status == state.RETRY_PENDING and failures > MAX_AUTO_RETRIES:
                status = state.FAILED
        result = self.transition(
            task_id,
            status,
            stage=stage,
            actor=actor,
            attempt_id=attempt_id,
            code=code,
            detail=detail,
        )
        if status == state.RETRY_PENDING:
            if count_attempt:
                if next_retry_at is None:
                    from datetime import timedelta

                    next_retry_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=retry_backoff_seconds(failures))
                    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                self.db.insert(
                    "UPDATE tasks SET retry_count = ?, last_error = ?, next_retry_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (failures, error[:4000], next_retry_at, utc_now(), task_id),
                )
            else:
                self.db.insert(
                    "UPDATE tasks SET last_error = ?, next_retry_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (error[:4000], next_retry_at, utc_now(), task_id),
                )
        else:
            self.db.insert(
                "UPDATE tasks SET retry_count = ?, last_error = ?, next_retry_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (
                    failures if status == state.FAILED else 0,
                    error[:4000],
                    utc_now(),
                    task_id,
                ),
            )
        return self.require(task_id)

    def reset_retry_budget(self, task_id: str) -> None:
        """Start a new failure episode after an explicit user/admin retry."""

        self.db.insert(
            "UPDATE tasks SET retry_count = 0, failure_notified = 0, updated_at = ? "
            "WHERE id = ?",
            (utc_now(), task_id),
        )

    def mark_failure_notified(self, task_id: str) -> None:
        self.db.insert(
            "UPDATE tasks SET failure_notified = 1, failure_seq = failure_seq + 1, "
            "updated_at = ? WHERE id = ?",
            (utc_now(), task_id),
        )

    def due_for_retry(self, limit: int = 10) -> list[str]:
        now = utc_now()
        rows = self.db.fetchall(
            """
            SELECT id FROM tasks
             WHERE status = ?
               AND (next_retry_at IS NULL OR next_retry_at <= ?)
             ORDER BY updated_at LIMIT ?
            """,
            (state.RETRY_PENDING, now, max(1, min(int(limit), 50))),
        )
        return [str(row["id"]) for row in rows]

    def clear_retry_state(self, task_id: str) -> None:
        self.db.insert(
            "UPDATE tasks SET last_error = NULL, next_retry_at = NULL, updated_at = ? WHERE id = ?",
            (utc_now(), task_id),
        )

    # ------------------------------------------------------------------
    # versions / inputs
    # ------------------------------------------------------------------
    def start_next_version(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        copy_inputs: bool = True,
        exclude_filenames: Iterable[str] = (),
    ) -> dict[str, Any]:
        now = utc_now()
        excluded = {normalize_key(name) for name in exclude_filenames}
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            old_version = int(row["version"])
            new_version = old_version + 1
            conn.execute(
                """
                INSERT INTO task_versions (task_id, version, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, new_version, row["status"], now),
            )
            conn.execute(
                """
                UPDATE tasks SET version = ?, updated_at = ?,
                                 reprocess_confirmed = 0, content_hash = NULL,
                                 business_identity = NULL, last_error = NULL,
                                 retry_count = 0, failure_notified = 0
                 WHERE id = ?
                """,
                (new_version, now, task_id),
            )
            if copy_inputs:
                rows = conn.execute(
                    "SELECT * FROM task_inputs WHERE task_id = ? AND version = ?",
                    (task_id, old_version),
                ).fetchall()
                for source in rows:
                    if normalize_key(str(source["filename"])) in excluded:
                        # A same-name supplement replaces this input for the
                        # new version only; the old version row and file stay.
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO task_inputs (
                            id, task_id, version, mail_id, file_hash, path, filename,
                            source_type, mime_type, size_bytes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("input"),
                            task_id,
                            new_version,
                            source["mail_id"],
                            source["file_hash"],
                            source["path"],
                            source["filename"],
                            source["source_type"],
                            source["mime_type"],
                            source["size_bytes"],
                            now,
                        ),
                    )
            conn.execute(
                """
                UPDATE task_issues SET status = 'superseded', resolved_at = ?
                 WHERE task_id = ? AND version < ? AND status = 'open'
                """,
                (now, task_id, new_version),
            )
            conn.execute(
                """
                INSERT INTO task_events (
                    task_id, version, stage, from_status, to_status, code,
                    detail_json, actor, created_at
                ) VALUES (?, ?, 'versioning', ?, ?, 'new_version_started', ?, ?, ?)
                """,
                (
                    task_id,
                    new_version,
                    row["status"],
                    row["status"],
                    json.dumps({"from_version": old_version, "reason": reason}, ensure_ascii=False),
                    actor,
                    now,
                ),
            )
        self.logger.info(
            "task_new_version",
            task_id=task_id,
            version=new_version,
            from_version=old_version,
            actor=actor,
        )
        return self.require(task_id)

    def confirm_reprocess(self, task_id: str, *, actor: str, reason: str | None = None) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT version FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            conn.execute(
                "UPDATE tasks SET reprocess_confirmed = 1, updated_at = ? WHERE id = ?",
                (utc_now(), task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events (
                    task_id, version, stage, code, detail_json, actor, created_at
                ) VALUES (?, ?, 'admin', 'reprocess_confirmed', ?, ?, ?)
                """,
                (
                    task_id,
                    int(row["version"]),
                    json.dumps({"reason": reason}, ensure_ascii=False) if reason else None,
                    actor,
                    utc_now(),
                ),
            )

    def apply_supplement(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        exclude_filenames: Iterable[str] = (),
        aliases: Iterable[tuple[str, str, str]] = (),
        destination: tuple[str, str] | None = None,
        overrides: Iterable[dict[str, Any]] = (),
        inputs: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Apply a full owner supplement in ONE database transaction.

        Writes country aliases (with audit rows), starts the next task
        version (copying inputs except replaced file names), updates the
        destination, stores overrides and registers the new input rows. If
        anything fails, the transaction rolls back: the previously active
        version, its inputs and the reference configuration remain exactly
        as they were. ``aliases`` items are pre-validated
        ``(alias_norm, alias_raw, alpha3)`` tuples.
        """

        excluded = {normalize_key(name) for name in exclude_filenames}
        now = utc_now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            old_version = int(row["version"])
            new_version = old_version + 1

            for alias_norm, alias_raw, alpha3 in aliases:
                existing = conn.execute(
                    "SELECT 1 FROM country_aliases WHERE alias_norm = ?", (alias_norm,)
                ).fetchone()
                if existing is not None:
                    continue
                conn.execute(
                    """
                    INSERT INTO country_aliases(alias_norm, alpha3, alias_raw, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (alias_norm, alpha3, alias_raw, now),
                )
                conn.execute(
                    """
                    INSERT INTO config_versions(key, value_json, previous_json, actor, reason, created_at)
                    VALUES (?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        f"country_alias:{alias_norm}",
                        json.dumps(
                            {"alias": alias_raw, "alpha3": alpha3}, ensure_ascii=False
                        ),
                        actor,
                        reason,
                        now,
                    ),
                )

            conn.execute(
                """
                INSERT INTO task_versions (task_id, version, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, new_version, row["status"], now),
            )
            conn.execute(
                """
                UPDATE tasks SET version = ?, updated_at = ?,
                                 reprocess_confirmed = 0, content_hash = NULL,
                                 business_identity = NULL, last_error = NULL,
                                 retry_count = 0, failure_notified = 0
                 WHERE id = ?
                """,
                (new_version, now, task_id),
            )
            source_rows = conn.execute(
                "SELECT * FROM task_inputs WHERE task_id = ? AND version = ?",
                (task_id, old_version),
            ).fetchall()
            for source in source_rows:
                if normalize_key(str(source["filename"])) in excluded:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO task_inputs (
                        id, task_id, version, mail_id, file_hash, path, filename,
                        source_type, mime_type, size_bytes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("input"),
                        task_id,
                        new_version,
                        source["mail_id"],
                        source["file_hash"],
                        source["path"],
                        source["filename"],
                        source["source_type"],
                        source["mime_type"],
                        source["size_bytes"],
                        now,
                    ),
                )
            if destination is not None:
                conn.execute(
                    """
                    UPDATE tasks SET destination_name = ?, destination_alpha3 = ?,
                                     updated_at = ? WHERE id = ?
                    """,
                    (destination[0], destination[1], now, task_id),
                )
                conn.execute(
                    """
                    UPDATE task_versions SET destination_name = ?, destination_alpha3 = ?
                     WHERE task_id = ? AND version = ?
                    """,
                    (destination[0], destination[1], task_id, new_version),
                )
            for item in overrides:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO task_overrides (
                        task_id, record_key, field, value, source_message_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        item["record_key"],
                        item["field"],
                        item["value"],
                        item.get("source_message_id"),
                        now,
                    ),
                )
            for item in inputs:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO task_inputs (
                        id, task_id, version, mail_id, file_hash, path, filename,
                        source_type, mime_type, size_bytes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("input"),
                        task_id,
                        new_version,
                        item.get("mail_id"),
                        item["file_hash"],
                        item["path"],
                        item["filename"],
                        item.get("source_type", "attachment"),
                        item.get("mime_type"),
                        int(item.get("size_bytes") or 0),
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE task_issues SET status = 'superseded', resolved_at = ?
                 WHERE task_id = ? AND version < ? AND status = 'open'
                """,
                (now, task_id, new_version),
            )
            conn.execute(
                """
                INSERT INTO task_events (
                    task_id, version, stage, from_status, to_status, code,
                    detail_json, actor, created_at
                ) VALUES (?, ?, 'versioning', ?, ?, 'new_version_started', ?, ?, ?)
                """,
                (
                    task_id,
                    new_version,
                    row["status"],
                    row["status"],
                    json.dumps(
                        {"from_version": old_version, "reason": reason},
                        ensure_ascii=False,
                    ),
                    actor,
                    now,
                ),
            )
        self.logger.info(
            "task_new_version",
            task_id=task_id,
            version=new_version,
            from_version=old_version,
            actor=actor,
        )
        return self.require(task_id)

    def add_input(
        self,
        *,
        task_id: str,
        version: int,
        mail_id: str | None,
        file_hash: str,
        path: str,
        filename: str,
        source_type: str,
        mime_type: str | None,
        size_bytes: int,
    ) -> None:
        self.db.insert(
            """
            INSERT OR IGNORE INTO task_inputs (
                id, task_id, version, mail_id, file_hash, path, filename,
                source_type, mime_type, size_bytes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("input"),
                task_id,
                version,
                mail_id,
                file_hash,
                path,
                filename,
                source_type,
                mime_type,
                size_bytes,
                utc_now(),
            ),
        )

    def inputs(self, task_id: str, version: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM task_inputs WHERE task_id = ? AND version = ? ORDER BY id",
                (task_id, version),
            )
        ]

    def update_destination(
        self, task_id: str, name: str | None, alpha3: str | None
    ) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT version FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            conn.execute(
                """
                UPDATE tasks SET destination_name = ?, destination_alpha3 = ?, updated_at = ?
                 WHERE id = ?
                """,
                (name, alpha3, utc_now(), task_id),
            )
            conn.execute(
                """
                UPDATE task_versions SET destination_name = ?, destination_alpha3 = ?
                 WHERE task_id = ? AND version = ?
                """,
                (name, alpha3, task_id, int(row["version"])),
            )

    def add_override(
        self,
        task_id: str,
        *,
        record_key: str,
        field: str,
        value: str,
        source_message_id: str | None,
    ) -> None:
        self.db.insert(
            """
            INSERT OR IGNORE INTO task_overrides (
                task_id, record_key, field, value, source_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, record_key, field, value, source_message_id, utc_now()),
        )

    def overrides(self, task_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM task_overrides WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ]

    # ------------------------------------------------------------------
    # results
    # ------------------------------------------------------------------
    def update_result(
        self,
        task_id: str,
        version: int,
        *,
        content_hash: str | None,
        business_identity: str | None,
        template_hash: str | None,
        output_dir: str | None,
        status: str,
        record_count: int = 0,
    ) -> None:
        now = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE task_versions
                   SET content_hash = ?, business_identity = ?, template_hash = ?,
                       output_dir = ?, status = ?, processed_at = ?, record_count = ?
                 WHERE task_id = ? AND version = ?
                """,
                (
                    content_hash,
                    business_identity,
                    template_hash,
                    output_dir,
                    status,
                    now,
                    max(0, int(record_count)),
                    task_id,
                    version,
                ),
            )
            conn.execute(
                """
                UPDATE tasks SET content_hash = COALESCE(?, content_hash),
                                 business_identity = COALESCE(?, business_identity),
                                 template_hash = COALESCE(?, template_hash), updated_at = ?
                 WHERE id = ?
                """,
                (content_hash, business_identity, template_hash, now, task_id),
            )

    def replace_issues(self, task_id: str, version: int, issues: Iterable[dict[str, Any]]) -> int:
        now = utc_now()
        count = 0
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE task_issues SET status = 'superseded', resolved_at = ?
                 WHERE task_id = ? AND version = ? AND status = 'open'
                """,
                (now, task_id, version),
            )
            for issue in issues:
                conn.execute(
                    """
                    INSERT INTO task_issues (
                        task_id, version, code, record_key, field, message,
                        sources_json, required_action, blocking, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        task_id,
                        version,
                        str(issue.get("code") or "UNKNOWN_ISSUE"),
                        issue.get("record_key"),
                        issue.get("field"),
                        str(issue.get("message") or ""),
                        json.dumps(issue.get("sources") or [], ensure_ascii=False),
                        issue.get("required_action"),
                        1 if issue.get("blocking", True) else 0,
                        now,
                    ),
                )
                count += 1
        return count

    def add_issue(
        self,
        task_id: str,
        version: int,
        issue: dict[str, Any],
        *,
        actor: str = "system",
        stage: str = "supplement",
    ) -> None:
        """Append an issue without discarding existing core issues."""

        now = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO task_issues (
                    task_id, version, code, record_key, field, message,
                    sources_json, required_action, blocking, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    task_id,
                    version,
                    str(issue.get("code") or "UNKNOWN_ISSUE"),
                    issue.get("record_key"),
                    issue.get("field"),
                    str(issue.get("message") or ""),
                    json.dumps(issue.get("sources") or [], ensure_ascii=False),
                    issue.get("required_action"),
                    1 if issue.get("blocking", True) else 0,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_events (
                    task_id, version, stage, code, detail_json, actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    version,
                    stage,
                    f"issue:{issue.get('code') or 'UNKNOWN_ISSUE'}",
                    json.dumps(
                        {"field": issue.get("field"), "record_key": issue.get("record_key")},
                        ensure_ascii=False,
                    ),
                    actor,
                    now,
                ),
            )

    def replace_provenance(
        self, task_id: str, version: int, provenance: Iterable[dict[str, Any]]
    ) -> int:
        now = utc_now()
        count = 0
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM field_provenance WHERE task_id = ? AND version = ?",
                (task_id, version),
            )
            for item in provenance:
                conn.execute(
                    """
                    INSERT INTO field_provenance (
                        task_id, version, record_key, field, value_json,
                        source_kind, source_ref_json, mail_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        version,
                        item.get("record_key"),
                        item.get("field"),
                        json.dumps(item.get("value"), ensure_ascii=False, default=str),
                        item.get("source_kind"),
                        json.dumps(item.get("source_ref"), ensure_ascii=False, default=str),
                        item.get("mail_id"),
                        now,
                    ),
                )
                count += 1
        return count

    def add_artifact(
        self,
        task_id: str,
        version: int,
        *,
        path: str,
        filename: str,
        sha256: str,
        size_bytes: int,
        kind: str | None = None,
    ) -> None:
        self.db.insert(
            """
            INSERT INTO task_artifacts (
                task_id, version, path, filename, sha256, size_bytes, kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, version, path) DO UPDATE SET
                filename = excluded.filename,
                sha256 = excluded.sha256,
                size_bytes = excluded.size_bytes,
                kind = excluded.kind
            """,
            (task_id, version, path, filename, sha256, size_bytes, kind, utc_now()),
        )

    def issues(self, task_id: str, version: int | None = None) -> list[dict[str, Any]]:
        task = self.require(task_id)
        version = int(version or task["version"])
        rows = self.db.fetchall(
            """
            SELECT * FROM task_issues
             WHERE task_id = ? AND version = ? AND status IN ('open', 'resolved')
             ORDER BY id
            """,
            (task_id, version),
        )
        result = []
        for row in rows:
            item = dict(row)
            item["sources"] = json.loads(item.pop("sources_json") or "[]")
            item["blocking"] = bool(item["blocking"])
            result.append(item)
        return result

    def provenance(self, task_id: str, version: int | None = None) -> list[dict[str, Any]]:
        task = self.require(task_id)
        version = int(version or task["version"])
        rows = self.db.fetchall(
            """
            SELECT * FROM field_provenance WHERE task_id = ? AND version = ? ORDER BY id
            """,
            (task_id, version),
        )
        result = []
        for row in rows:
            item = dict(row)
            for key in ("value_json", "source_ref_json"):
                try:
                    item[key.removesuffix("_json")] = json.loads(item.pop(key) or "null")
                except json.JSONDecodeError:
                    item[key.removesuffix("_json")] = item.pop(key)
            result.append(item)
        return result

    def artifacts(self, task_id: str, version: int | None = None) -> list[dict[str, Any]]:
        task = self.require(task_id)
        version = int(version or task["version"])
        return [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM task_artifacts WHERE task_id = ? AND version = ? ORDER BY id",
                (task_id, version),
            )
        ]

    def events(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM task_events WHERE task_id = ?
             ORDER BY id DESC LIMIT ?
            """,
            (task_id, max(1, min(int(limit), 1000))),
        )
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "null")
            except json.JSONDecodeError:
                item["detail"] = item.pop("detail_json")
            result.append(item)
        return result

    def recover_incomplete(self, actor: str = "startup-recovery") -> list[str]:
        """Move tasks interrupted by a crash to RETRY_PENDING.

        This never re-sends anything. Outbox ``sending`` rows are handled by the
        OutboxRepository and become SEND_UNKNOWN.
        """

        recovered: list[str] = []
        rows = self.db.fetchall(
            "SELECT * FROM tasks WHERE status IN (?, ?, ?)",
            (state.RECEIVED, state.VALIDATING, state.PROCESSING),
        )
        for row in rows:
            try:
                self.transition(
                    str(row["id"]),
                    state.RETRY_PENDING,
                    stage="recovery",
                    actor=actor,
                    code="interrupted_processing",
                    detail={"previous_status": row["status"]},
                )
                recovered.append(str(row["id"]))
            except Exception:
                self.logger.exception(
                    "task_recovery_failed", Exception("recovery transition failed"), task_id=row["id"]
                )
        return recovered