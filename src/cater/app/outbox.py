"""Outbox persistence with explicit, non-blind sending states."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import Database
from .errors import StateTransitionError
from .logging import JsonlLogger
from .util import new_id, sha256_file, sha256_text, utc_now

PENDING = "pending"
SENDING = "sending"
SENT = "sent"
SEND_UNKNOWN = "send_unknown"
FAILED = "failed"
DRY_RUN = "dry_run"
CANCELLED = "cancelled"

ALL_STATUSES = frozenset({PENDING, SENDING, SENT, SEND_UNKNOWN, FAILED, DRY_RUN, CANCELLED})

TRANSITIONS = {
    PENDING: {SENDING, DRY_RUN, CANCELLED},
    DRY_RUN: {PENDING, CANCELLED},
    SENDING: {SENT, SEND_UNKNOWN, PENDING, FAILED},
    FAILED: {PENDING, CANCELLED},
    SEND_UNKNOWN: {SENT, PENDING, CANCELLED},
    SENT: set(),
    CANCELLED: set(),
}

PURPOSES = frozenset({"delivery", "question", "notice"})


class OutboxRepository:
    def __init__(self, db: Database, logger: JsonlLogger) -> None:
        self.db = db
        self.logger = logger

    # ------------------------------------------------------------------
    # enqueue / lookup
    # ------------------------------------------------------------------
    def enqueue(
        self,
        *,
        dedupe_key: str,
        task_id: str,
        version: int,
        purpose: str,
        recipient: str,
        recipient_name: str | None,
        subject: str,
        body: str,
        attachments: list[dict[str, str]],
        in_reply_to: str | None,
        references: list[str],
        message_id: str,
        dry_run: bool,
    ) -> tuple[dict[str, Any], bool]:
        if purpose not in PURPOSES:
            raise ValueError(purpose)
        existing = self.db.fetchone("SELECT * FROM outbox WHERE dedupe_key = ?", (dedupe_key,))
        if existing is not None:
            return dict(existing), False

        attachment_hashes: list[str] = []
        for attachment in attachments:
            path = Path(attachment["path"])
            if not path.is_file():
                raise FileNotFoundError(path)
            attachment_hashes.append(sha256_file(path))
        payload_hash = sha256_text(
            json.dumps(
                {
                    "recipient": recipient,
                    "subject": subject,
                    "body": body,
                    "attachments": attachment_hashes,
                    "in_reply_to": in_reply_to,
                    "references": references,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        outbox_id = new_id("out")
        now = utc_now()
        status = DRY_RUN if dry_run else PENDING
        try:
            self.db.insert(
                """
                INSERT INTO outbox (
                    id, dedupe_key, task_id, version, purpose, recipient, recipient_name,
                    subject, body, attachment_paths, in_reply_to, references_header,
                    payload_hash, message_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outbox_id,
                    dedupe_key,
                    task_id,
                    version,
                    purpose,
                    recipient,
                    recipient_name,
                    subject,
                    body,
                    json.dumps(attachments, ensure_ascii=False),
                    in_reply_to,
                    " ".join(references),
                    payload_hash,
                    message_id,
                    status,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self.db.fetchone(
                "SELECT * FROM outbox WHERE dedupe_key = ? OR message_id = ?",
                (dedupe_key, message_id),
            )
            if existing is None:
                raise
            return dict(existing), False
        self.logger.info(
            "outbox_enqueued",
            task_id=task_id,
            version=version,
            purpose=purpose,
            outbox_id=outbox_id,
            status=status,
            dedupe_key=dedupe_key,
        )
        row = self.get(outbox_id)
        assert row is not None
        return row, True

    def get(self, outbox_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM outbox WHERE id = ?", (outbox_id,))
        return _decode(row)

    def find_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        normalized = message_id.strip().strip("<>")
        row = self.db.fetchone(
            "SELECT * FROM outbox WHERE trim(message_id, '<>') = ? ORDER BY created_at DESC LIMIT 1",
            (normalized,),
        )
        return _decode(row)

    def for_task(self, task_id: str, version: int | None = None) -> list[dict[str, Any]]:
        if version is None:
            rows = self.db.fetchall(
                "SELECT * FROM outbox WHERE task_id = ? ORDER BY id DESC", (task_id,)
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM outbox WHERE task_id = ? AND version = ? ORDER BY id DESC",
                (task_id, version),
            )
        return [_decode(row) for row in rows if row is not None]

    def list_due(self, limit: int = 20) -> list[dict[str, Any]]:
        now = utc_now()
        rows = self.db.fetchall(
            """
            SELECT * FROM outbox
             WHERE status = ?
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
             ORDER BY created_at LIMIT ?
            """,
            (PENDING, now, max(1, min(int(limit), 100))),
        )
        return [_decode(row) for row in rows if row is not None]

    def unknown(self) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM outbox WHERE status = ? ORDER BY created_at", (SEND_UNKNOWN,)
        )
        return [_decode(row) for row in rows if row is not None]

    def list_recent(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if status:
            rows = self.db.fetchall(
                "SELECT * FROM outbox WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM outbox ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [_decode(row) for row in rows if row is not None]

    # ------------------------------------------------------------------
    # status changes
    # ------------------------------------------------------------------
    def _transition(
        self,
        outbox_id: str,
        target: str,
        *,
        allowed_from: set[str] | None = None,
        error: str | None = None,
        response: str | None = None,
        provider_message_id: str | None = None,
        next_attempt_at: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
            if row is None:
                raise KeyError(outbox_id)
            current = str(row["status"])
            if allowed_from is not None and current not in allowed_from:
                raise StateTransitionError(
                    f"Outbox {outbox_id} is {current}; expected one of {sorted(allowed_from)}"
                )
            if target not in TRANSITIONS.get(current, set()):
                raise StateTransitionError(f"Cannot move outbox from {current} to {target}")
            updates = ["status = ?"]
            params: list[Any] = [target]
            if target == SENDING:
                updates.extend(["attempts = attempts + 1", "sending_at = ?"])
                params.append(now)
            if target == SENT:
                updates.extend(["sent_at = ?", "checked_at = ?"])
                params.extend([now, now])
            if target == SEND_UNKNOWN:
                updates.append("checked_at = ?")
                params.append(now)
            if error is not None:
                updates.append("last_error = ?")
                params.append(error[:4000])
            if response is not None:
                updates.append("response = ?")
                params.append(response[:4000])
            if provider_message_id is not None:
                updates.append("provider_message_id = ?")
                params.append(provider_message_id)
            if next_attempt_at is not None:
                updates.append("next_attempt_at = ?")
                params.append(next_attempt_at)
            params.append(outbox_id)
            conn.execute(
                f"UPDATE outbox SET {', '.join(updates)} WHERE id = ?", tuple(params)
            )
        self.logger.info(
            "outbox_status_changed",
            outbox_id=outbox_id,
            task_id=row["task_id"],
            version=row["version"],
            purpose=row["purpose"],
            from_status=current,
            to_status=target,
        )
        result = self.get(outbox_id)
        assert result is not None
        return result

    def mark_sending(self, outbox_id: str) -> dict[str, Any]:
        return self._transition(outbox_id, SENDING, allowed_from={PENDING})

    def mark_sent(
        self, outbox_id: str, *, response: str, provider_message_id: str | None = None
    ) -> dict[str, Any]:
        # SENT may be reached directly from SENDING (normal SMTP acceptance) or
        # from SEND_UNKNOWN after a Sent-folder recheck / explicit admin
        # confirmation. Both are audited transitions.
        return self._transition(
            outbox_id,
            SENT,
            allowed_from={SENDING, SEND_UNKNOWN},
            response=response,
            provider_message_id=provider_message_id,
        )

    def mark_unknown(self, outbox_id: str, error: str) -> dict[str, Any]:
        return self._transition(
            outbox_id, SEND_UNKNOWN, allowed_from={SENDING}, error=error
        )

    def mark_pending(
        self,
        outbox_id: str,
        *,
        error: str | None = None,
        next_attempt_at: str | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            outbox_id,
            PENDING,
            allowed_from={SENDING, FAILED, DRY_RUN, SEND_UNKNOWN},
            error=error,
            next_attempt_at=next_attempt_at,
        )

    def mark_failed(self, outbox_id: str, error: str) -> dict[str, Any]:
        return self._transition(outbox_id, FAILED, allowed_from={SENDING}, error=error)

    def mark_dry_run(self, outbox_id: str) -> dict[str, Any]:
        return self._transition(outbox_id, DRY_RUN, allowed_from={PENDING})

    def mark_cancelled(self, outbox_id: str) -> dict[str, Any]:
        return self._transition(outbox_id, CANCELLED, allowed_from={PENDING, DRY_RUN, FAILED, SEND_UNKNOWN})

    def recover_sending(self) -> list[dict[str, Any]]:
        """Any row interrupted while sending becomes SEND_UNKNOWN, never resent."""

        recovered: list[dict[str, Any]] = []
        rows = self.db.fetchall("SELECT * FROM outbox WHERE status = ?", (SENDING,))
        for row in rows:
            try:
                result = self._transition(
                    str(row["id"]),
                    SEND_UNKNOWN,
                    allowed_from={SENDING},
                    error="process interrupted while sending; delivery is uncertain",
                )
                recovered.append(result)
            except StateTransitionError:
                continue
        return recovered


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item["attachments"] = json.loads(item.pop("attachment_paths") or "[]")
    except json.JSONDecodeError:
        item["attachments"] = []
    return item