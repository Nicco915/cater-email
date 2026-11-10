"""Persistence for inbound mail protocol coordinates, raw files and cursors."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .config import AppConfig
from .db import Database
from .mail.message import ParsedMail
from .util import new_id, utc_now


class MailStore:
    def __init__(self, db: Database, config: AppConfig) -> None:
        self.db = db
        self.config = config

    # ------------------------------------------------------------------
    # raw files
    # ------------------------------------------------------------------
    def save_raw(self, raw: bytes, raw_hash: str, received_hint: str | None = None) -> Path:
        directory = self.config.mail_dir / (received_hint or utc_now())[:7].replace("-", "/")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{raw_hash}.eml"
        if not path.exists():
            # 独占唯一暂存：并发 save_raw 同一 raw_hash 时，固定 .tmp 名
            # 会互相抢占 replace 源（FileNotFoundError）或交叉写。
            import secrets
            temporary = path.with_name(
                f"{path.name}.{secrets.token_hex(8)}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.replace(temporary, path)
                except FileNotFoundError:
                    # 并发线程已发布同一内容：以已发布文件为准。
                    pass
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    # ------------------------------------------------------------------
    # message records
    # ------------------------------------------------------------------
    def record(
        self,
        *,
        account: str,
        folder: str,
        uid_validity: int | None,
        uid: int,
        raw_hash: str,
        parsed: ParsedMail,
        raw_path: str,
        size_bytes: int,
    ) -> tuple[dict[str, Any], bool]:
        """Insert one protocol coordinate.

        Returns ``(row, created)``. A repeated UID is never treated as a new
        message even if the server re-delivers it.
        """

        # SQLite UNIQUE treats NULLs as distinct, so a server that omits
# UIDVALIDITY would otherwise allow duplicate coordinates. Normalize the
# absence to 0 for both lookup and storage.
        validity = 0 if uid_validity is None else int(uid_validity)
        existing = self.db.fetchone(
            """
            SELECT * FROM mail_messages
             WHERE account = ? AND folder = ? AND uid_validity = ? AND uid = ?
            """,
            (account, folder, validity, uid),
        )
        if existing is not None:
            return dict(existing), False
        mail_id = new_id("mail")
        now = utc_now()
        try:
            self.db.insert(
                """
                INSERT INTO mail_messages (
                    id, account, folder, uid_validity, uid, message_id, raw_hash,
                    sender, sender_name, subject, received_at, direction, size_bytes,
                    raw_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'inbound', ?, ?, ?)
                """,
                (
                    mail_id,
                    account,
                    folder,
                    validity,
                    uid,
                    parsed.message_id,
                    raw_hash,
                    parsed.sender,
                    parsed.sender_name,
                    parsed.subject,
                    parsed.date,
                    size_bytes,
                    raw_path,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self.db.fetchone(
                """
                SELECT * FROM mail_messages
                 WHERE account = ? AND folder = ? AND uid_validity = ? AND uid = ?
                """,
                (account, folder, validity, uid),
            )
            if existing is None:
                raise
            return dict(existing), False
        row = self.db.fetchone("SELECT * FROM mail_messages WHERE id = ?", (mail_id,))
        assert row is not None
        return dict(row), True

    def get(self, mail_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM mail_messages WHERE id = ?", (mail_id,))
        return dict(row) if row else None

    def set_task(self, mail_id: str, task_id: str | None, filter_reason: str | None = None) -> None:
        self.db.insert(
            "UPDATE mail_messages SET task_id = ?, filter_reason = ? WHERE id = ?",
            (task_id, filter_reason, mail_id),
        )

    def find_by_raw_hash(self, raw_hash: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM mail_messages WHERE raw_hash = ? ORDER BY created_at",
                (raw_hash,),
            )
        ]

    def find_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        normalized = message_id.strip().strip("<>")
        row = self.db.fetchone(
            "SELECT * FROM mail_messages WHERE trim(message_id, '<>') = ? ORDER BY created_at DESC LIMIT 1",
            (normalized,),
        )
        return dict(row) if row else None

    def for_task(self, task_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM mail_messages WHERE task_id = ? ORDER BY created_at", (task_id,)
            )
        ]

    # ------------------------------------------------------------------
    # cursors
    # ------------------------------------------------------------------
    def get_cursor(self, account: str, folder: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM mailbox_cursors WHERE account = ? AND folder = ?", (account, folder)
        )
        return dict(row) if row else None

    def save_cursor(
        self,
        account: str,
        folder: str,
        *,
        uid_validity: int | None,
        last_uid: int,
        first_scan_completed: bool = True,
    ) -> None:
        now = utc_now()
        self.db.insert(
            """
            INSERT INTO mailbox_cursors (
                account, folder, uid_validity, last_uid, first_scan_completed,
                last_scan_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, folder) DO UPDATE SET
                uid_validity = excluded.uid_validity,
                last_uid = excluded.last_uid,
                first_scan_completed = excluded.first_scan_completed,
                last_scan_at = excluded.last_scan_at,
                updated_at = excluded.updated_at
            """,
            (
                account,
                folder,
                uid_validity,
                last_uid,
                1 if first_scan_completed else 0,
                now,
                now,
                now,
            ),
        )