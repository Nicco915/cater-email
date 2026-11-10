"""SQLite access: serialized writes, WAL, foreign keys and migrations."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .migrations import MIGRATIONS, sql_statements
from .util import utc_now


class Database:
    """A small single-process SQLite wrapper.

    All writes are serialized by an in-process lock, transactions are short,
    WAL is enabled and foreign keys are enforced. Network operations must
    never happen inside :meth:`transaction`.
    """

    def __init__(
        self,
        path: str | Path,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if connection is not None:
            self._conn = connection
            self._external = True
        else:
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False,
                isolation_level=None, timeout=10,
            )
            self._external = False
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
        self.migrate()

    # ------------------------------------------------------------------
    # migrations
    # ------------------------------------------------------------------
    def migrate(self) -> list[int]:
        applied: list[int] = []
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            existing = {
                int(row["version"])
                for row in self._conn.execute("SELECT version FROM schema_migrations")
            }
            for migration in MIGRATIONS:
                if migration.version in existing:
                    continue
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    for statement in sql_statements(migration.sql):
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, utc_now()),
                    )
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
                applied.append(migration.version)
        return applied

    def schema_version(self) -> int:
        row = self.fetchone("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations")
        return int(row["version"]) if row else 0

    # ------------------------------------------------------------------
    # read/write helpers
    # ------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return cursor

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)

    def insert(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.fetchone(sql, params)
        return row[0] if row is not None else None

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    def backup_to(self, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(destination))
            try:
                self._conn.backup(target)
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                target.close()
        return destination

    def restore_from(self, source: str | Path) -> None:
        """Restore a verified backup into the live database."""

        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            try:
                src.backup(self._conn)
            finally:
                src.close()

    def quick_check(self) -> str:
        row = self.fetchone("PRAGMA quick_check")
        return str(row[0]) if row else "unknown"

    def close(self) -> None:
        with self._lock:
            if self._external:
                return  # 外部连接的所有权归调用方（测试/并发注入）
            try:
                self._conn.close()
            except sqlite3.Error:
                pass