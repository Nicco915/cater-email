from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cater.app import db as db_module
from cater.app.db import Database
from cater.app.migrations import MIGRATION_1, Migration


def test_existing_database_is_upgraded_without_losing_reference_rows(
    tmp_path: Path
) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(MIGRATION_1)
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (1, 'initial_schema', 'old')"
    )
    connection.execute(
        """
        INSERT INTO machine_materials (model_norm, model, declaration, updated_at)
        VALUES ('915F', '915F', '110KW', 'old')
        """
    )
    connection.execute(
        """
        INSERT INTO machine_aliases(alias_norm, model_norm, alias_raw, created_at)
        VALUES ('OLD915', '915F', 'Old915', 'old')
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        assert database.schema_version() == 10
        assert database.quick_check() == "ok"
        rows = [dict(row) for row in database.fetchall("SELECT * FROM machine_materials")]
        assert len(rows) == 1
        assert rows[0]["model"] == "915F"
        assert rows[0]["declaration"] == "110KW"
        assert rows[0]["destination_scope"] == ""
        # Rows imported before the special-rule flag existed start
        # unconfirmed; re-importing the workbook refreshes the flag.
        assert rows[0]["special_rule_confirmed"] == 0
        # Migration 8 adds the authority/enabled columns; existing rows default
        # to enabled so historical reference data keeps matching after upgrade.
        assert rows[0]["enabled"] == 1
        aliases = [dict(row) for row in database.fetchall("SELECT * FROM machine_aliases")]
        assert aliases[0]["alias_raw"] == "Old915"
        assert aliases[0]["destination_scope"] == ""
        assert aliases[0]["enabled"] == 1

        # The upgraded composite key permits the Brazil variant alongside the
        # default variant.
        database.insert(
            """
            INSERT INTO machine_materials (
                model_norm, destination_scope, model, declaration, special, updated_at
            ) VALUES ('915F', 'BRA', '915F', '123KW', '巴西', 'new')
            """
        )
        assert database.scalar("SELECT COUNT(*) FROM machine_materials") == 2
    finally:
        database.close()


def test_nonempty_old_db_upgrades_keeping_tasks_and_outbox(tmp_path: Path) -> None:
    """A real pre-existing runtime must keep its tasks and outbox after the
    lifecycle/reference-authority migration, with the new columns present."""

    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(MIGRATION_1)
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO schema_migrations VALUES (1, 'initial_schema', 'old')")
    connection.execute(
        """
        INSERT INTO tasks (id, owner, kind, version, status, created_at, updated_at)
        VALUES ('task_old', 'owner@example.com', 'parts', 1, 'COMPLETED', 't', 't')
        """
    )
    connection.execute(
        """
        INSERT INTO outbox (id, dedupe_key, task_id, version, purpose, recipient,
            subject, body, payload_hash, message_id, status, created_at)
        VALUES ('out_old', 'dk', 'task_old', 1, 'delivery', 'r@x', 's', 'b', 'ph',
            'mid', 'sent', 't')
        """
    )
    connection.execute(
        """
        INSERT INTO countries (alpha3, name_zh, name_en, source, created_at)
        VALUES ('FRA', '法国', 'France', 'builtin', 't')
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        assert database.schema_version() == 10
        assert database.quick_check() == "ok"
        # Task and outbox preserved.
        assert database.scalar("SELECT COUNT(*) FROM tasks") == 1
        assert database.scalar("SELECT COUNT(*) FROM outbox") == 1
        # Migration 8 lifecycle columns exist and default to NULL/1.
        task = database.fetchone("SELECT * FROM tasks WHERE id = 'task_old'")
        assert task["archived_at"] is None
        assert task["deleted_at"] is None
        # The enabled column added by migration 8 defaults to 1 for prior rows.
        country = database.fetchone("SELECT * FROM countries WHERE alpha3 = 'FRA'")
        assert country["enabled"] == 1
        # New authority/preview tables exist.
        assert database.fetchone("SELECT * FROM runtime_meta WHERE key = 'nope'") is None
    finally:
        database.close()


def test_failed_migration_rolls_back_all_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "broken.sqlite3"
    good = Migration(1, "good", "CREATE TABLE alpha (id INTEGER);")
    bad = Migration(
        2,
        "bad",
        "CREATE TABLE beta (id INTEGER);\nINSERT INTO missing_table VALUES (1);\n",
    )
    monkeypatch.setattr(db_module, "MIGRATIONS", (good, bad))
    with pytest.raises(sqlite3.OperationalError):
        Database(path)

    connection = sqlite3.connect(path)
    try:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "alpha" in names
        assert "beta" not in names
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] == 1
    finally:
        connection.close()