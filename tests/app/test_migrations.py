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
        assert database.schema_version() == 7
        assert database.quick_check() == "ok"
        rows = [dict(row) for row in database.fetchall("SELECT * FROM machine_materials")]
        assert len(rows) == 1
        assert rows[0]["model"] == "915F"
        assert rows[0]["declaration"] == "110KW"
        assert rows[0]["destination_scope"] == ""
        # Rows imported before the special-rule flag existed start
        # unconfirmed; re-importing the workbook refreshes the flag.
        assert rows[0]["special_rule_confirmed"] == 0
        aliases = [dict(row) for row in database.fetchall("SELECT * FROM machine_aliases")]
        assert aliases[0]["alias_raw"] == "Old915"
        assert aliases[0]["destination_scope"] == ""

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