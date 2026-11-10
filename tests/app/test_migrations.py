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
        assert database.schema_version() == 15
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
        assert database.schema_version() == 15
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


def test_migration_11_layout_columns_and_confirmed_table(tmp_path: Path) -> None:
    """布局持久化：task_versions.layout_json 列与 confirmed_layouts 表。"""
    database = Database(tmp_path / "fresh.sqlite3")
    try:
        assert database.schema_version() == 15
        columns = {
            row[1]
            for row in database.fetchall("PRAGMA table_info(task_versions)")
        }
        assert "layout_json" in columns
        tables = {
            row[0]
            for row in database.fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "confirmed_layouts" in tables
        # 确认布局可存取（UPSERT 语义 + 主键(kind, fingerprint)）
        database.insert(
            """
            INSERT INTO confirmed_layouts (kind, fingerprint, structure_json,
                confirmed_at, actor) VALUES ('parts', 'fp1', '{}', 't', 'a')
            ON CONFLICT (kind, fingerprint) DO UPDATE SET
                structure_json = excluded.structure_json,
                confirmed_at = excluded.confirmed_at,
                actor = excluded.actor
            """
        )
        database.insert(
            """
            INSERT INTO confirmed_layouts (kind, fingerprint, structure_json,
                confirmed_at, actor) VALUES ('parts', 'fp1', '{"v":2}', 't2', 'b')
            ON CONFLICT (kind, fingerprint) DO UPDATE SET
                structure_json = excluded.structure_json,
                confirmed_at = excluded.confirmed_at,
                actor = excluded.actor
            """
        )
        assert database.scalar("SELECT COUNT(*) FROM confirmed_layouts") == 1
        row = database.fetchone(
            "SELECT structure_json, actor FROM confirmed_layouts WHERE kind = 'parts'"
        )
        assert row["structure_json"] == '{"v":2}'
        assert row["actor"] == "b"
    finally:
        database.close()

def test_migration_12_task_version_source_mails(tmp_path: Path) -> None:
    """版本来源邮件：task_version_mails 表随版本记录补件业务表邮件。

    migration 13 后主键为 (task_id, version, role, mail_id)：同一版本
    同角色可累积多封来源邮件（连续部分表格补件各自留痕）。"""
    database = Database(tmp_path / "fresh.sqlite3")
    try:
        assert database.schema_version() == 15
        tables = {
            row[0]
            for row in database.fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "task_version_mails" in tables
        database.insert(
            """
            INSERT INTO mail_messages (id, account, folder, uid, raw_hash, sender,
                direction, created_at)
            VALUES ('mail_1', 'bot@example.com', 'INBOX', 1, 'h1',
                'tester@example.com', 'inbound', 't')
            """
        )
        database.insert(
            """
            INSERT INTO mail_messages (id, account, folder, uid, raw_hash, sender,
                direction, created_at)
            VALUES ('mail_2', 'bot@example.com', 'INBOX', 2, 'h2',
                'tester@example.com', 'inbound', 't')
            """
        )
        database.insert(
            """
            INSERT INTO tasks (id, owner, kind, status, version, created_at, updated_at)
            VALUES ('task_x', 'tester@example.com', 'machine', 'WAITING_INPUT', 1,
                't', 't')
            """
        )
        database.insert(
            """
            INSERT INTO task_versions (task_id, version, status, created_at)
            VALUES ('task_x', 1, 'WAITING_INPUT', 't')
            """
        )
        # 同版本同角色多封来源邮件：累积而非覆盖
        database.insert(
            """
            INSERT INTO task_version_mails (task_id, version, mail_id, role, created_at)
            VALUES ('task_x', 1, 'mail_1', 'supplement_table', 't')
            """
        )
        database.insert(
            """
            INSERT INTO task_version_mails (task_id, version, mail_id, role, created_at)
            VALUES ('task_x', 1, 'mail_2', 'supplement_table', 't2')
            """
        )
        rows = database.fetchall(
            "SELECT * FROM task_version_mails WHERE task_id = 'task_x' ORDER BY created_at"
        )
        assert [row["mail_id"] for row in rows] == ["mail_1", "mail_2"]
        assert all(row["role"] == "supplement_table" for row in rows)
        # 外键强制：不存在的邮件/版本必须被拒绝
        with pytest.raises(sqlite3.IntegrityError):
            database.insert(
                """
                INSERT INTO task_version_mails (task_id, version, mail_id, role,
                    created_at)
                VALUES ('task_x', 1, 'no_such_mail', 'supplement_table', 't')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            database.insert(
                """
                INSERT INTO task_version_mails (task_id, version, mail_id, role,
                    created_at)
                VALUES ('task_x', 99, 'mail_1', 'supplement_table', 't')
                """
            )
    finally:
        database.close()


def test_migration_14_multi_ticket_batch_tables(tmp_path: Path) -> None:
    """多票批次表：迁移14 新建批次/方案/候选/资产/关联/事件表，约束生效。"""
    database = Database(tmp_path / "fresh.sqlite3")
    try:
        assert database.schema_version() == 15
        tables = {
            row[0]
            for row in database.fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for name in (
            "mail_batches", "batch_plan_versions", "batch_candidates",
            "batch_assets", "batch_tasks", "batch_events",
        ):
            assert name in tables
        # 批次邮件身份唯一：(account, owner, mail_hash)
        database.insert(
            """
            INSERT INTO mail_messages (id, account, folder, uid, raw_hash, sender,
                direction, created_at)
            VALUES ('mail_b', 'bot@example.com', 'OFFLINE-BATCH', 1, 'h1',
                'tester@example.com', 'inbound', 't')
            """
        )
        database.insert(
            """
            INSERT INTO mail_batches (id, mail_id, account, owner, mail_hash,
                status, created_at, created_by)
            VALUES ('batch_1', 'mail_b', 'bot@example.com', 'tester@example.com',
                'h1', 'open', 't', 'cli')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            database.insert(
                """
                INSERT INTO mail_batches (id, mail_id, account, owner, mail_hash,
                    status, created_at, created_by)
                VALUES ('batch_2', 'mail_b', 'bot@example.com',
                    'tester@example.com', 'h1', 'open', 't', 'cli')
                """
            )
        # 不同 owner / 原文不混批
        database.insert(
            """
            INSERT INTO mail_batches (id, mail_id, account, owner, mail_hash,
                status, created_at, created_by)
            VALUES ('batch_3', 'mail_b', 'bot@example.com', 'other@example.com',
                'h1', 'open', 't', 'cli')
            """
        )
        # 方案版本唯一 (batch_id, version)
        database.insert(
            """
            INSERT INTO batch_plan_versions (id, batch_id, version,
                source_fingerprint, inspector_version, preview_json,
                created_at, created_by)
            VALUES ('plan_1', 'batch_1', 1, 'fp', 'v1', '{}', 't', 'cli')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            database.insert(
                """
                INSERT INTO batch_plan_versions (id, batch_id, version,
                    source_fingerprint, inspector_version, preview_json,
                    created_at, created_by)
                VALUES ('plan_2', 'batch_1', 1, 'fp2', 'v1', '{}', 't', 'cli')
                """
            )
        # batch_tasks 关联：候选唯一 + 任务唯一 + 默认挂起
        database.insert(
            """
            INSERT INTO tasks (id, owner, kind, status, version, created_at,
                updated_at)
            VALUES ('task_b1', 'tester@example.com', 'machine', 'RECEIVED', 1,
                't', 't')
            """
        )
        database.insert(
            """
            INSERT INTO batch_tasks (id, batch_id, plan_id, candidate_key,
                task_id, snapshot_path, hold_reason, created_at)
            VALUES ('bt_1', 'batch_1', 'plan_1', 'cand-x', 'task_b1',
                '/tmp/x', 'hold', 't')
            """
        )
        row = database.fetchone("SELECT * FROM batch_tasks WHERE id = 'bt_1'")
        assert row["execution_hold"] == 1
        with pytest.raises(sqlite3.IntegrityError):
            database.insert(
                """
                INSERT INTO batch_tasks (id, batch_id, plan_id, candidate_key,
                    task_id, snapshot_path, hold_reason, created_at)
                VALUES ('bt_2', 'batch_1', 'plan_1', 'cand-y', 'task_b1',
                    '/tmp/y', 'hold', 't')
                """
            )
    finally:
        database.close()
