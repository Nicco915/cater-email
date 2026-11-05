from __future__ import annotations

from pathlib import Path

import pytest

from cater.app import state
from cater.app.errors import (
    BatchDisallowedError,
    StateTransitionError,
    ValidationError,
)
from cater.app.tasks import TaskRepository
from cater.app.db import Database

from helpers import FakeEngine, make_config, raw_message, make_mail


def _completed_service(service_factory, live_config, gateway, offline_source):
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    task = service.submit_offline_sources(
        sender="tester@example.com",
        destination="法国",
        source_paths=[offline_source],
    )
    assert task["status"] == state.COMPLETED
    return service, task


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------
def test_archive_hides_from_active_list_but_keeps_processing(service, offline_source: Path) -> None:
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    service.archive_task(task["id"], actor="test")
    active = [t["id"] for t in service.tasks.list(lifecycle="active")]
    archived = [t["id"] for t in service.tasks.list(lifecycle="archived")]
    assert task["id"] not in active
    assert task["id"] in archived
    # Archive is display-only: reprocessing still works.
    reprocessed = service.reprocess_task(task["id"], actor="test", regenerate_only=True)
    assert reprocessed["version"] == 2


def test_unarchive_returns_to_active(service, offline_source: Path) -> None:
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    service.archive_task(task["id"], actor="test")
    service.unarchive_task(task["id"], actor="test")
    assert task["id"] in [t["id"] for t in service.tasks.list(lifecycle="active")]


# ---------------------------------------------------------------------------
# delete to recycle bin
# ---------------------------------------------------------------------------
def test_delete_blocks_processing_retry_reprocess_and_delivery(
    service, offline_source: Path
) -> None:
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    assert task["status"] == state.READY_TO_SEND
    # An unsent outbox row exists (dry_run in the default fixture); trashing
    # must cancel it but keep the row as audit.
    unsent = service.db.fetchall(
        "SELECT * FROM outbox WHERE task_id = ? AND status IN ('pending','dry_run')",
        (task["id"],),
    )
    assert unsent
    service.delete_task(task["id"], actor="test", reason="cleanup")
    assert service.tasks.is_trashed(task["id"])
    statuses = {
        row["status"]
        for row in service.db.fetchall("SELECT status FROM outbox WHERE task_id = ?", (task["id"],))
    }
    assert "pending" not in statuses
    assert "dry_run" not in statuses
    assert "cancelled" in statuses

    with pytest.raises(StateTransitionError):
        service.process_task(task["id"], actor="test")
    with pytest.raises(StateTransitionError):
        service.retry_task(task["id"], actor="test")
    with pytest.raises(StateTransitionError):
        service.reprocess_task(task["id"], actor="test")
    with pytest.raises(StateTransitionError):
        service.deliver_task(task["id"], actor="test")


def test_restore_does_not_resend_or_reprocess(service, offline_source: Path) -> None:
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    outbox_id = service.db.scalar(
        "SELECT id FROM outbox WHERE task_id = ? AND status IN ('pending','dry_run') LIMIT 1",
        (task["id"],),
    )
    service.delete_task(task["id"], actor="test")
    service.restore_task(task["id"], actor="test")
    assert not service.tasks.is_trashed(task["id"])
    # The previously cancelled outbox row stays cancelled; restoring does not
    # reactivate it or create a new one.
    row = service.outbox.get(outbox_id)
    assert row["status"] == "cancelled"
    new_unsent = service.db.fetchall(
        "SELECT * FROM outbox WHERE task_id = ? AND status IN ('pending','dry_run')",
        (task["id"],),
    )
    assert not new_unsent


def test_processing_and_send_unknown_tasks_cannot_be_deleted(
    service_factory, config, gateway, offline_source: Path
) -> None:
    service = service_factory(config, engine=FakeEngine(), gateway=gateway)
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    # Move to SEND_UNKNOWN to simulate an uncertain delivery.
    service.tasks.transition(task["id"], state.SEND_UNKNOWN, stage="test", actor="test")
    with pytest.raises(Exception):
        service.delete_task(task["id"], actor="test")
    assert not service.tasks.is_trashed(task["id"])


def test_completed_dedupe_still_recognises_trashed_task(
    service_factory, live_config, gateway, offline_source: Path
) -> None:
    service, task = _completed_service(service_factory, live_config, gateway, offline_source)
    content_hash = task["content_hash"]
    service.delete_task(task["id"], actor="test")
    # A new task with the same content must still be recognised as processed,
    # even though the prior task is in the recycle bin.
    existing = service.tasks.find_by_content_hash(
        "tester@example.com", "parts", content_hash, exclude_task_id="other"
    )
    assert existing is not None
    assert existing["id"] == task["id"]
    assert existing["status"] == state.COMPLETED


# ---------------------------------------------------------------------------
# batch operations are all-or-nothing
# ---------------------------------------------------------------------------
def test_batch_delete_aborts_when_any_disallowed(
    service_factory, config, gateway, offline_source: Path
) -> None:
    service = service_factory(config, engine=FakeEngine(), gateway=gateway)
    blocked = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    # Put the first task into an in-flight status that blocks deletion.
    service.tasks.transition(blocked["id"], state.PROCESSING, stage="test", actor="test")
    # A second submit is not deduped against a PROCESSING task, so it becomes a
    # normal READY_TO_SEND task eligible for the recycle bin.
    ok = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    with pytest.raises(BatchDisallowedError) as exc:
        service.batch_delete([ok["id"], blocked["id"]], actor="test")
    reasons = {item["task_id"]: item["reason"] for item in exc.value.items}
    assert blocked["id"] in reasons
    # Nothing was applied: the allowed task is still active.
    assert not service.tasks.is_trashed(ok["id"])
    assert not service.tasks.is_trashed(blocked["id"])


def test_batch_archive_then_restore_round_trip(service, offline_source: Path) -> None:
    ids = []
    for _ in range(2):
        task = service.submit_offline_sources(
            sender="tester@example.com", destination="法国", source_paths=[offline_source]
        )
        ids.append(task["id"])
    service.batch_archive(ids, actor="test")
    assert all(
        t not in [x["id"] for x in service.tasks.list(lifecycle="active")] for t in ids
    )
    service.batch_unarchive(ids, actor="test")
    assert all(
        t in [x["id"] for x in service.tasks.list(lifecycle="active")] for t in ids
    )


# ---------------------------------------------------------------------------
# file purge + test clear
# ---------------------------------------------------------------------------
def test_purge_files_only_for_trashed_terminal_task(service, offline_source: Path) -> None:
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    # Not trashed yet: refuse.
    with pytest.raises(Exception):
        service.purge_task_files_preview(task["id"])
    service.delete_task(task["id"], actor="test")
    preview = service.purge_task_files_preview(task["id"])
    assert preview["file_count"] >= 1
    result = service.purge_task_files(task["id"], actor="test")
    assert result["files_removed"] >= 1
    # Task row + dedupe fingerprint remain.
    assert service.tasks.get(task["id"]) is not None


def test_clear_test_data_requires_test_identity_and_refuses_production(
    service_factory, tmp_path: Path
) -> None:
    prod_config = make_config(tmp_path, mode="production", production_addresses=("op@example.com",))
    service = service_factory(prod_config)
    assert service.test_identity() == "production"
    preview = service.clear_test_data_preview()
    assert preview["eligible"] is False
    with pytest.raises(Exception):
        service.clear_test_data(actor="test", confirm=True)


def test_clear_test_data_clears_tasks_keeps_references(
    service, offline_source: Path
) -> None:
    service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    assert service.test_identity() == "test"
    # Reference data exists before clear.
    assert service.references.list_countries()
    result = service.clear_test_data(actor="test", confirm=True)
    assert result["safety_backup"]
    assert service.db.scalar("SELECT COUNT(*) FROM tasks") == 0
    assert service.db.scalar("SELECT COUNT(*) FROM outbox") == 0
    # References and config audit are preserved.
    assert service.references.list_countries()
    assert service.references.reference_version() > 0


def test_clear_test_data_requires_confirmation(service) -> None:
    with pytest.raises(Exception):
        service.clear_test_data(actor="test", confirm=False)


# ---------------------------------------------------------------------------
# mail commands respect the recycle bin
# ---------------------------------------------------------------------------
def test_status_query_on_trashed_task_answers_deleted_without_leak(
    service, engine
) -> None:
    from helpers import make_mail, raw_message

    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-trash@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    question = next(
        row for row in service.outbox.for_task(task_id) if row["purpose"] == "question"
    )
    service.delete_task(task_id, actor="test")

    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="查询状态",
        in_reply_to=question["message_id"],
        message_id="<query-trash@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.status_query is True
    # The answer says the task was deleted and how to recover; it must not
    # reveal other tasks or the prior business state.
    guidance = [
        row
        for row in service.outbox.list_recent(limit=50)
        if row["purpose"] == "guidance" and "回收站" in (row["body"] or "")
    ]
    assert guidance
    body = guidance[0]["body"]
    assert "联系维护人员" in body
    assert "缺少数量" not in body  # prior business state is not leaked


def test_reprocess_command_on_trashed_task_is_refused(service, engine) -> None:
    from helpers import make_mail, raw_message

    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-rep@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    question = next(
        row for row in service.outbox.for_task(task_id) if row["purpose"] == "question"
    )
    service.delete_task(task_id, actor="test")
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="确认重新处理",
        in_reply_to=question["message_id"],
        message_id="<rep-trash@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    # No reprocess was triggered; the task stays trashed and frozen.
    assert not result.reprocess_requested
    assert service.tasks.is_trashed(task_id)


def test_due_for_retry_excludes_trashed(service, engine, offline_source: Path) -> None:
    engine.mode = "core_unavailable"
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    assert task["status"] == state.RETRY_PENDING
    engine.mode = "ready"
    # Make the retry immediately due (the backoff would otherwise defer it).
    service.db.insert(
        "UPDATE tasks SET next_retry_at = NULL WHERE id = ?", (task["id"],)
    )
    assert task["id"] in service.tasks.due_for_retry(limit=10)
    service.delete_task(task["id"], actor="test")
    assert task["id"] not in service.tasks.due_for_retry(limit=10)


# ---------------------------------------------------------------------------
# batch atomicity under a mid-batch failure (fault injection)
# ---------------------------------------------------------------------------
def test_batch_archive_rolls_back_all_on_midbatch_failure(
    service, offline_source: Path, monkeypatch
) -> None:
    ids = []
    for _ in range(2):
        task = service.submit_offline_sources(
            sender="tester@example.com", destination="法国", source_paths=[offline_source]
        )
        ids.append(task["id"])

    original = TaskRepository._apply_archive
    calls = {"n": 0}

    def flaky(self, conn, row, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected second-item failure")
        return original(self, conn, row, **kwargs)

    monkeypatch.setattr(TaskRepository, "_apply_archive", flaky)
    with pytest.raises(RuntimeError):
        service.batch_archive(ids, actor="test")
    # The whole batch rolled back: neither task is archived.
    for task_id in ids:
        assert service.tasks.get(task_id)["archived_at"] is None


def test_batch_delete_rolls_back_all_on_midbatch_failure(
    service, offline_source: Path, monkeypatch
) -> None:
    ids = []
    for _ in range(2):
        task = service.submit_offline_sources(
            sender="tester@example.com", destination="法国", source_paths=[offline_source]
        )
        ids.append(task["id"])
    original = TaskRepository._apply_delete
    calls = {"n": 0}

    def flaky(self, conn, row, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected second-item failure")
        return original(self, conn, row, **kwargs)

    monkeypatch.setattr(TaskRepository, "_apply_delete", flaky)
    with pytest.raises(RuntimeError):
        service.batch_delete(ids, actor="test")
    for task_id in ids:
        assert not service.tasks.is_trashed(task_id)


# ---------------------------------------------------------------------------
# outbox send_unknown blocks delete; sending claim refuses deleted task
# ---------------------------------------------------------------------------
def test_any_send_unknown_outbox_row_blocks_delete(service, offline_source: Path) -> None:
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    # Inject a send-unknown outbox row directly while the task itself stays
    # READY_TO_SEND; deletion must still be refused.
    service.db.insert(
        """
        INSERT INTO outbox (id, dedupe_key, task_id, version, purpose, recipient,
            subject, body, payload_hash, message_id, status, created_at)
        VALUES ('out-unknown', 'dk-unknown', ?, 1, 'delivery', 'r@x', 's', 'b',
            'ph', 'mid-unknown', 'send_unknown', 't')
        """,
        (task["id"],),
    )
    with pytest.raises(ValidationError):
        service.delete_task(task["id"], actor="test")
    assert not service.tasks.is_trashed(task["id"])


def test_mark_sending_refuses_deleted_task(service, offline_source: Path) -> None:
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    outbox_id = service.db.scalar(
        "SELECT id FROM outbox WHERE task_id = ? AND status = 'dry_run' LIMIT 1",
        (task["id"],),
    )
    # Force the row to pending and mark the task deleted directly (bypassing the
    # trash cancel path) to simulate the list_due/claim race.
    service.db.insert("UPDATE outbox SET status = 'pending' WHERE id = ?", (outbox_id,))
    service.db.insert("UPDATE tasks SET deleted_at = 'now' WHERE id = ?", (task["id"],))
    with pytest.raises(StateTransitionError):
        service.outbox.mark_sending(outbox_id)


# ---------------------------------------------------------------------------
# restore of RETRY_PENDING does not auto-process
# ---------------------------------------------------------------------------
def test_restore_retry_pending_holds_until_explicit_retry(
    service, engine, offline_source: Path
) -> None:
    engine.mode = "core_unavailable"
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    assert task["status"] == state.RETRY_PENDING
    engine.mode = "ready"
    service.db.insert("UPDATE tasks SET next_retry_at = NULL WHERE id = ?", (task["id"],))
    service.delete_task(task["id"], actor="test")
    service.restore_task(task["id"], actor="test")
    # Restored RETRY_PENDING task is on hold: not auto-selected.
    assert service.tasks.get(task["id"])["auto_retry_hold"] == 1
    assert task["id"] not in service.tasks.due_for_retry(limit=10)
    # Explicit retry clears the hold and resumes.
    resumed = service.retry_task(task["id"], actor="test")
    assert resumed["status"] in {state.READY_TO_SEND, state.COMPLETED}
    assert service.tasks.get(task["id"])["auto_retry_hold"] == 0


# ---------------------------------------------------------------------------
# environment identity: ambiguous/production runtimes are never clearable
# ---------------------------------------------------------------------------
def test_old_db_with_data_becomes_unknown_and_not_clearable(
    service_factory, config, gateway, offline_source: Path, tmp_path: Path
) -> None:
    service = service_factory(config, engine=FakeEngine(), gateway=gateway)
    service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    # Simulate a pre-marker (old) runtime: no environment identity yet, but it
    # already holds business data.
    service.db.insert("DELETE FROM runtime_meta WHERE key = 'environment'")
    restarted = service_factory(config, engine=FakeEngine(), gateway=gateway)
    assert restarted.test_identity() == "unknown"
    preview = restarted.clear_test_data_preview()
    assert preview["eligible"] is False
    with pytest.raises(Exception):
        restarted.clear_test_data(actor="test", confirm=True)


def test_seen_production_forever_blocks_clear(
    service_factory, config, gateway
) -> None:
    # Start once in production mode, then switch to test mode.
    from dataclasses import replace

    from helpers import make_config

    prod = make_config(
        config.paths.runtime_dir.parent.parent / "prod_runtime_tmp",
        mode="production",
        production_addresses=("op@example.com",),
    )
    service = service_factory(prod, engine=FakeEngine(), gateway=gateway)
    assert service.test_identity() == "production"
    service.settings.update({"allowlist.mode": "test"}, actor="test", reason="switch")
    restarted = service_factory(prod, engine=FakeEngine(), gateway=gateway)
    preview = restarted.clear_test_data_preview()
    assert preview["seen_production"] is True
    assert preview["eligible"] is False


def test_clear_test_blocked_when_outbox_send_unknown(service, offline_source: Path) -> None:
    task = service.submit_offline_sources(
        sender="tester@example.com", destination="法国", source_paths=[offline_source]
    )
    service.db.insert(
        """
        INSERT INTO outbox (id, dedupe_key, task_id, version, purpose, recipient,
            subject, body, payload_hash, message_id, status, created_at)
        VALUES ('out-u2', 'dk-u2', ?, 1, 'delivery', 'r@x', 's', 'b', 'ph', 'mid-u2',
            'send_unknown', 't')
        """,
        (task["id"],),
    )
    preview = service.clear_test_data_preview()
    assert preview["eligible"] is True  # identity is fine
    assert preview["blockers"]
    with pytest.raises(Exception):
        service.clear_test_data(actor="test", confirm=True)


def test_clear_test_requires_poller_stopped(service) -> None:
    with pytest.raises(Exception):
        service.clear_test_data(actor="test", confirm=True, poller_running=True)


def test_no_restart_production_switch_persists_seen_marker(service) -> None:
    # Switch test -> production -> test without restarting the process.
    service.settings.update(
        {
            "allowlist.mode": "production",
            "allowlist.production": ["op@example.com"],
        },
        actor="test",
        reason="switch-prod",
        production_ack=True,
    )
    assert service.settings.current().allowlist.mode == "production"
    service.settings.update(
        {"allowlist.mode": "test"}, actor="test", reason="switch-back"
    )
    assert service.settings.current().allowlist.mode == "test"
    # The permanent production marker was written in the settings transaction,
    # so clear-test stays blocked even though the live mode is test again.
    preview = service.clear_test_data_preview()
    assert preview["seen_production"] is True
    assert preview["eligible"] is False
    with pytest.raises(Exception):
        service.clear_test_data(actor="test", confirm=True)


# ---------------------------------------------------------------------------
# restart stability: deleted/disabled countries do not resurrect
# ---------------------------------------------------------------------------
def test_disabled_country_does_not_resurrect_on_restart(
    service_factory, config, gateway, tmp_path: Path
) -> None:
    service = service_factory(config, engine=FakeEngine(), gateway=gateway)
    service.references.set_country_enabled("FRA", False, actor="test", reason="test")
    assert service.references.get_country("FRA")["enabled"] is False
    # A fresh service on the same runtime must not re-seed FRA.
    restarted = service_factory(config, engine=FakeEngine(), gateway=gateway)
    assert restarted.references.get_country("FRA")["enabled"] is False
    assert restarted.references.resolve("法国") is None
    assert restarted.references.resolve("France") is None
