"""Versioned SQLite schema migrations.

Applied migration numbers are recorded in ``schema_migrations``. Migrations are
append-only; never edit an applied migration, add a new numbered entry.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def sql_statements(sql: str):
    """Split a migration script into complete statements.

    ``sqlite3.executescript`` implicitly commits any open transaction, so
    migrations execute statement-by-statement inside an explicit transaction
    instead. ``sqlite3.complete_statement`` understands quoted strings and
    comments, unlike a naive ``split(';')``.
    """

    buffer = ""
    for line in (sql or "").splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement and statement != ";":
                yield statement
            buffer = ""
    remainder = buffer.strip()
    if remainder and remainder != ";":
        yield remainder


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS mail_messages (
    id              TEXT PRIMARY KEY,
    account         TEXT NOT NULL,
    folder          TEXT NOT NULL,
    uid_validity    INTEGER,
    uid             INTEGER NOT NULL,
    message_id      TEXT,
    raw_hash        TEXT NOT NULL,
    sender          TEXT NOT NULL,
    sender_name     TEXT,
    subject         TEXT,
    received_at     TEXT,
    direction       TEXT NOT NULL DEFAULT 'inbound',
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    raw_path        TEXT,
    task_id         TEXT,
    filter_reason   TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (account, folder, uid_validity, uid)
);
CREATE INDEX IF NOT EXISTS idx_mail_raw_hash ON mail_messages(raw_hash);
CREATE INDEX IF NOT EXISTS idx_mail_message_id ON mail_messages(message_id);
CREATE INDEX IF NOT EXISTS idx_mail_task ON mail_messages(task_id);

CREATE TABLE IF NOT EXISTS mailbox_cursors (
    account         TEXT NOT NULL,
    folder          TEXT NOT NULL,
    uid_validity    INTEGER,
    last_uid        INTEGER NOT NULL DEFAULT 0,
    first_scan_completed INTEGER NOT NULL DEFAULT 0,
    last_scan_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (account, folder)
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    owner               TEXT NOT NULL,
    owner_name          TEXT,
    kind                TEXT NOT NULL,
    business_key        TEXT,
    business_identity   TEXT,
    version             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL,
    destination_name    TEXT,
    destination_alpha3  TEXT,
    content_hash        TEXT,
    config_version      INTEGER,
    template_hash       TEXT,
    last_error          TEXT,
    reprocess_confirmed INTEGER NOT NULL DEFAULT 0,
    origin_mail_id      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    next_retry_at       TEXT,
    FOREIGN KEY (origin_mail_id) REFERENCES mail_messages(id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner_kind ON tasks(owner, kind);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_content_hash ON tasks(content_hash);
CREATE INDEX IF NOT EXISTS idx_tasks_business_identity ON tasks(business_identity);

CREATE TABLE IF NOT EXISTS task_versions (
    task_id             TEXT NOT NULL,
    version             INTEGER NOT NULL,
    status              TEXT NOT NULL,
    content_hash        TEXT,
    business_identity   TEXT,
    destination_name    TEXT,
    destination_alpha3  TEXT,
    output_dir          TEXT,
    template_hash       TEXT,
    created_at          TEXT NOT NULL,
    processed_at        TEXT,
    PRIMARY KEY (task_id, version),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_inputs (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    mail_id         TEXT,
    file_hash       TEXT NOT NULL,
    path            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    mime_type       TEXT,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE (task_id, version, file_hash, mail_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (mail_id) REFERENCES mail_messages(id)
);
CREATE INDEX IF NOT EXISTS idx_task_inputs_task ON task_inputs(task_id, version);

CREATE TABLE IF NOT EXISTS task_overrides (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    record_key          TEXT NOT NULL,
    field               TEXT NOT NULL,
    value               TEXT NOT NULL,
    source_message_id   TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (task_id, record_key, field, value, source_message_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    attempt_id      TEXT,
    stage           TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    code            TEXT,
    detail_json     TEXT,
    actor           TEXT NOT NULL DEFAULT 'system',
    created_at      TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_events_code ON task_events(code);

CREATE TABLE IF NOT EXISTS field_provenance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    record_key      TEXT,
    field           TEXT,
    value_json      TEXT,
    source_kind     TEXT,
    source_ref_json TEXT,
    mail_id         TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_provenance_task ON field_provenance(task_id, version);

CREATE TABLE IF NOT EXISTS task_issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    code            TEXT NOT NULL,
    record_key      TEXT,
    field           TEXT,
    message         TEXT NOT NULL,
    sources_json    TEXT,
    required_action TEXT,
    blocking        INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_issues_task ON task_issues(task_id, version, status);

CREATE TABLE IF NOT EXISTS task_artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    version         INTEGER NOT NULL,
    path            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    kind            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (task_id, version, path),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outbox (
    id                  TEXT PRIMARY KEY,
    dedupe_key          TEXT NOT NULL UNIQUE,
    task_id             TEXT NOT NULL,
    version             INTEGER NOT NULL,
    purpose             TEXT NOT NULL,
    recipient           TEXT NOT NULL,
    recipient_name      TEXT,
    subject             TEXT NOT NULL,
    body                TEXT NOT NULL,
    attachment_paths    TEXT NOT NULL DEFAULT '[]',
    in_reply_to         TEXT,
    references_header   TEXT,
    payload_hash        TEXT NOT NULL,
    message_id          TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    response            TEXT,
    provider_message_id TEXT,
    created_at          TEXT NOT NULL,
    sending_at          TEXT,
    sent_at             TEXT,
    checked_at          TEXT,
    next_attempt_at     TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
CREATE INDEX IF NOT EXISTS idx_outbox_task ON outbox(task_id, version);

CREATE TABLE IF NOT EXISTS countries (
    alpha3          TEXT PRIMARY KEY,
    name_zh         TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'builtin',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS country_aliases (
    alias_norm      TEXT PRIMARY KEY,
    alpha3          TEXT NOT NULL,
    alias_raw       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (alpha3) REFERENCES countries(alpha3)
);

CREATE TABLE IF NOT EXISTS machine_materials (
    model_norm      TEXT PRIMARY KEY,
    model           TEXT NOT NULL,
    english_name    TEXT,
    hs_code         TEXT,
    product_name    TEXT,
    declaration     TEXT,
    notes           TEXT,
    source          TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS machine_aliases (
    alias_norm      TEXT PRIMARY KEY,
    model_norm      TEXT NOT NULL,
    alias_raw       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (model_norm) REFERENCES machine_materials(model_norm) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS config_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    previous_json   TEXT,
    actor           TEXT NOT NULL,
    reason          TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_versions_key ON config_versions(key, id);
"""


MIGRATION_2 = """
CREATE INDEX IF NOT EXISTS idx_mail_account_folder ON mail_messages(account, folder, uid);
CREATE INDEX IF NOT EXISTS idx_outbox_message_id_status ON outbox(message_id, status);
CREATE INDEX IF NOT EXISTS idx_versions_task ON task_versions(task_id, version);
"""


MIGRATION_3 = """
ALTER TABLE task_versions ADD COLUMN record_count INTEGER NOT NULL DEFAULT 0;
"""


MIGRATION_4 = """
PRAGMA defer_foreign_keys=ON;
CREATE TABLE machine_materials_new (
    model_norm          TEXT NOT NULL,
    destination_scope   TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL,
    english_name        TEXT,
    hs_code             TEXT,
    product_name        TEXT,
    declaration         TEXT,
    notes               TEXT,
    special             TEXT,
    verification        TEXT,
    source_row          INTEGER,
    source              TEXT,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (model_norm, destination_scope)
);
INSERT INTO machine_materials_new (
    model_norm, destination_scope, model, english_name, hs_code, product_name,
    declaration, notes, source, updated_at
)
SELECT model_norm, '', model, english_name, hs_code, product_name,
       declaration, notes, source, updated_at
  FROM machine_materials;
CREATE TABLE machine_aliases_new (
    alias_norm          TEXT NOT NULL,
    destination_scope   TEXT NOT NULL DEFAULT '',
    model_norm          TEXT NOT NULL,
    alias_raw           TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (alias_norm, destination_scope),
    FOREIGN KEY (model_norm, destination_scope)
        REFERENCES machine_materials_new(model_norm, destination_scope)
        ON DELETE CASCADE
);
INSERT INTO machine_aliases_new (
    alias_norm, destination_scope, model_norm, alias_raw, created_at
)
SELECT alias_norm, '', model_norm, alias_raw, created_at FROM machine_aliases;
DROP TABLE machine_aliases;
DROP TABLE machine_materials;
ALTER TABLE machine_materials_new RENAME TO machine_materials;
ALTER TABLE machine_aliases_new RENAME TO machine_aliases;
CREATE INDEX IF NOT EXISTS idx_machine_scope
    ON machine_materials(model_norm, destination_scope);
"""


MIGRATION_5 = """
ALTER TABLE machine_materials
    ADD COLUMN special_rule_confirmed INTEGER NOT NULL DEFAULT 0;
"""


MIGRATION_6 = """
ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN failure_notified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN failure_seq INTEGER NOT NULL DEFAULT 0;
"""


MIGRATION_7 = """
PRAGMA defer_foreign_keys=ON;
CREATE TABLE outbox_v7 (
    id                  TEXT PRIMARY KEY,
    dedupe_key          TEXT NOT NULL UNIQUE,
    task_id             TEXT,
    version             INTEGER NOT NULL DEFAULT 0,
    purpose             TEXT NOT NULL,
    recipient           TEXT NOT NULL,
    recipient_name      TEXT,
    subject             TEXT NOT NULL,
    body                TEXT NOT NULL,
    attachment_paths    TEXT NOT NULL DEFAULT '[]',
    in_reply_to         TEXT,
    references_header   TEXT,
    payload_hash        TEXT NOT NULL,
    message_id          TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    response            TEXT,
    provider_message_id TEXT,
    created_at          TEXT NOT NULL,
    sending_at          TEXT,
    sent_at             TEXT,
    checked_at          TEXT,
    next_attempt_at     TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
INSERT INTO outbox_v7 (
    id, dedupe_key, task_id, version, purpose, recipient, recipient_name,
    subject, body, attachment_paths, in_reply_to, references_header,
    payload_hash, message_id, status, attempts, last_error, response,
    provider_message_id, created_at, sending_at, sent_at, checked_at,
    next_attempt_at
)
SELECT
    id, dedupe_key, task_id, version, purpose, recipient, recipient_name,
    subject, body, attachment_paths, in_reply_to, references_header,
    payload_hash, message_id, status, attempts, last_error, response,
    provider_message_id, created_at, sending_at, sent_at, checked_at,
    next_attempt_at
  FROM outbox;
DROP TABLE outbox;
ALTER TABLE outbox_v7 RENAME TO outbox;
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
CREATE INDEX IF NOT EXISTS idx_outbox_task ON outbox(task_id, version);
CREATE INDEX IF NOT EXISTS idx_outbox_message_id_status ON outbox(message_id, status);
"""


MIGRATION_8 = """
ALTER TABLE tasks ADD COLUMN archived_at TEXT;
ALTER TABLE tasks ADD COLUMN deleted_at TEXT;
ALTER TABLE tasks ADD COLUMN deletion_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_lifecycle ON tasks(archived_at, deleted_at);

ALTER TABLE countries ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE countries ADD COLUMN disabled_at TEXT;
ALTER TABLE countries ADD COLUMN disabled_reason TEXT;

ALTER TABLE country_aliases ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;

ALTER TABLE machine_materials ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE machine_materials ADD COLUMN disabled_at TEXT;
ALTER TABLE machine_materials ADD COLUMN disabled_reason TEXT;

ALTER TABLE machine_aliases ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_countries_enabled ON countries(enabled);
CREATE INDEX IF NOT EXISTS idx_machines_enabled
    ON machine_materials(model_norm, destination_scope, enabled);

CREATE TABLE IF NOT EXISTS runtime_meta (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_previews (
    token              TEXT PRIMARY KEY,
    filename           TEXT NOT NULL,
    source_path        TEXT NOT NULL,
    reference_version  INTEGER NOT NULL,
    rows_json          TEXT NOT NULL,
    summary_json       TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'open',
    actor              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_import_previews_status ON import_previews(status, expires_at);
"""


MIGRATION_9 = """
ALTER TABLE tasks ADD COLUMN auto_retry_hold INTEGER NOT NULL DEFAULT 0;
"""


MIGRATION_10 = """
CREATE TABLE IF NOT EXISTS reference_tombstones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (kind, key)
);
"""


MIGRATION_11 = """
ALTER TABLE task_versions ADD COLUMN layout_json TEXT;

-- 已确认表格布局：显式人工确认，不自动信任未知布局。
-- 基准为已确认布局，不是上封邮件或未经确认的单次修正。
CREATE TABLE IF NOT EXISTS confirmed_layouts (
    kind            TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    structure_json  TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL,
    actor           TEXT NOT NULL,
    PRIMARY KEY (kind, fingerprint)
);
"""


MIGRATION_12 = """
-- 每个版本的补充来源邮件：某版本的业务表由哪封（补件）邮件提供。
-- role 目前仅有 supplement_table；同一版本同一角色只记录一封最新邮件。
-- 与 task_versions 同生共死（CASCADE），随补件事务一起回滚。
CREATE TABLE IF NOT EXISTS task_version_mails (
    task_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    mail_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, version, role),
    FOREIGN KEY (task_id, version) REFERENCES task_versions(task_id, version) ON DELETE CASCADE,
    FOREIGN KEY (mail_id) REFERENCES mail_messages(id)
);
CREATE INDEX IF NOT EXISTS idx_task_version_mails_mail ON task_version_mails(mail_id);
"""


MIGRATION_13 = """
-- 版本来源邮件需要“累积”：连续多封表格补件各提供部分字段时，新版本必须
-- 继承之前所有版本积累的来源邮件（否则早先补件填充的字段会丢）。
-- 主键扩为 (task_id, version, role, mail_id)，允许同版本多封来源邮件。
CREATE TABLE task_version_mails_v2 (
    task_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    mail_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, version, role, mail_id),
    FOREIGN KEY (task_id, version) REFERENCES task_versions(task_id, version) ON DELETE CASCADE,
    FOREIGN KEY (mail_id) REFERENCES mail_messages(id)
);
INSERT OR IGNORE INTO task_version_mails_v2
    SELECT task_id, version, mail_id, role, created_at FROM task_version_mails;
DROP TABLE task_version_mails;
ALTER TABLE task_version_mails_v2 RENAME TO task_version_mails;
CREATE INDEX IF NOT EXISTS idx_task_version_mails_mail ON task_version_mails(mail_id);
"""


MIGRATION_14 = """
-- 多票批次（阶段 2）：本地导入 .eml -> 持久化批次/方案版本 -> 人工确认 ->
-- 幂等创建逐票任务。只新增表，不改既有表结构。
--
-- 身份分层：
-- * mail_batches 的永久邮件身份是 (account, owner, mail_hash)：同一原邮件
--   重复/并发导入只建一批；不同邮箱/发件人/原文不会只因附件相同而混批。
-- * source_fingerprint 含识别器/别名版本，只标识某次识别结果，不作为邮件
--   身份（规则升级只产生新方案版本，不产生新批次）。
-- * 批次ID / 方案版本ID / 候选内容寻址ID / 单票任务ID 是四种不同ID。

CREATE TABLE IF NOT EXISTS mail_batches (
    id                   TEXT PRIMARY KEY,
    mail_id              TEXT NOT NULL REFERENCES mail_messages(id),
    account              TEXT NOT NULL,
    owner                TEXT NOT NULL,
    owner_name           TEXT,
    mail_hash            TEXT NOT NULL,
    subject              TEXT,
    message_id           TEXT,
    status               TEXT NOT NULL DEFAULT 'open',
    current_plan_id      TEXT,
    confirmed_plan_id    TEXT,
    confirmed_fingerprint TEXT,
    confirmed_at         TEXT,
    confirmed_by         TEXT,
    materialized_at      TEXT,
    created_at           TEXT NOT NULL,
    created_by           TEXT NOT NULL,
    UNIQUE (account, owner, mail_hash)
);
CREATE INDEX IF NOT EXISTS idx_mail_batches_status ON mail_batches(status);

-- 每次识别/调整产生一个不可变新版本；旧版本永不覆写。
CREATE TABLE IF NOT EXISTS batch_plan_versions (
    id                TEXT PRIMARY KEY,
    batch_id          TEXT NOT NULL REFERENCES mail_batches(id),
    version           INTEGER NOT NULL,
    source_fingerprint TEXT NOT NULL,
    inspector_version TEXT NOT NULL,
    alias_marker      TEXT,
    preview_json      TEXT NOT NULL,
    assignments_json  TEXT NOT NULL DEFAULT '[]',
    derived_from      TEXT,
    batch_ready       INTEGER NOT NULL DEFAULT 0,
    reason            TEXT,
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    UNIQUE (batch_id, version)
);
CREATE INDEX IF NOT EXISTS idx_batch_plans_batch
    ON batch_plan_versions(batch_id, version);

-- 每个方案版本下的候选票快照及人工选入/排除决定（未选=pending，不丢票）。
CREATE TABLE IF NOT EXISTS batch_candidates (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES batch_plan_versions(id),
    batch_id        TEXT NOT NULL REFERENCES mail_batches(id),
    candidate_key   TEXT NOT NULL,
    kind            TEXT,
    preview_json    TEXT NOT NULL,
    decision        TEXT NOT NULL DEFAULT 'pending',
    decision_reason TEXT,
    decided_by      TEXT,
    decided_at      TEXT,
    UNIQUE (plan_id, candidate_key)
);
CREATE INDEX IF NOT EXISTS idx_batch_candidates_batch
    ON batch_candidates(batch_id);

-- 邮件原件与原始附件：不可变保存一次，内容哈希 + 来源定位。
CREATE TABLE IF NOT EXISTS batch_assets (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES mail_batches(id),
    role        TEXT NOT NULL,
    filename    TEXT,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (batch_id, role, sha256)
);

-- 批次与单票任务的一对多关联；legacy mail_messages.task_id 保持单票语义，
-- 多票归属只通过本表表达。execution_hold=1：本阶段任务不可执行。
CREATE TABLE IF NOT EXISTS batch_tasks (
    id              TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES mail_batches(id),
    plan_id         TEXT NOT NULL REFERENCES batch_plan_versions(id),
    candidate_key   TEXT NOT NULL,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    snapshot_path   TEXT NOT NULL,
    execution_hold  INTEGER NOT NULL DEFAULT 1,
    hold_reason     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (batch_id, candidate_key),
    UNIQUE (task_id)
);
CREATE INDEX IF NOT EXISTS idx_batch_tasks_task ON batch_tasks(task_id);

CREATE TABLE IF NOT EXISTS batch_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES mail_batches(id),
    plan_id     TEXT,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail_json TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_events_batch ON batch_events(batch_id, id);
"""


MIGRATION_15 = """
ALTER TABLE batch_tasks ADD COLUMN snapshot_hash TEXT;
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_schema", MIGRATION_1),
    Migration(2, "additional_indexes", MIGRATION_2),
    Migration(3, "task_version_record_count", MIGRATION_3),
    Migration(4, "machine_reference_destination_scope", MIGRATION_4),
    Migration(5, "machine_special_rule_confirmed", MIGRATION_5),
    Migration(6, "task_retry_budget", MIGRATION_6),
    Migration(7, "outbox_taskless_guidance", MIGRATION_7),
    Migration(8, "lifecycle_reference_authority_import_preview", MIGRATION_8),
    Migration(9, "restored_task_retry_hold", MIGRATION_9),
    Migration(10, "reference_tombstones", MIGRATION_10),
    Migration(11, "confirmed_layouts", MIGRATION_11),
    Migration(12, "task_version_source_mails", MIGRATION_12),
    Migration(13, "task_version_source_mails_accumulate", MIGRATION_13),
    Migration(14, "multi_ticket_batches", MIGRATION_14),
    Migration(15, "batch_task_snapshot_hash", MIGRATION_15),
)