"""Persistent runtime metadata (key/value) stored in ``runtime_meta``.

Used for two safety-critical, restart-stable markers:

* ``reference_seeded`` — set once after the built-in country table is seeded.
  :meth:`ReferenceService.ensure_seeded` never re-seeds afterwards, so a
  country that was deleted or disabled by an operator is never resurrected by
  a process restart.
* ``environment`` — the allowlist identity recorded at first initialization
  (``test``/``production``). The "clear test data" operation requires this to
  be ``test``; a runtime initialized as production can never be cleared even if
  the operator later switches ``allowlist.mode`` to ``test``.
"""

from __future__ import annotations

from .db import Database
from .util import utc_now


def get_meta(db: Database, key: str) -> str | None:
    row = db.fetchone("SELECT value FROM runtime_meta WHERE key = ?", (key,))
    return None if row is None else str(row["value"])


def set_meta(db: Database, key: str, value: str) -> None:
    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO runtime_meta(key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now, now),
        )


def set_meta_if_absent(db: Database, key: str, value: str) -> bool:
    """Record ``key`` only the first time; never overwrite a prior identity."""

    now = utc_now()
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT value FROM runtime_meta WHERE key = ?", (key,)
        ).fetchone()
        if existing is not None:
            return False
        conn.execute(
            """
            INSERT INTO runtime_meta(key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (key, value, now, now),
        )
    return True
