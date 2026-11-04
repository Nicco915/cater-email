from __future__ import annotations

from pathlib import Path

import pytest

from cater.app import state
from cater.app.errors import SendNotAllowedError
from cater.app.mail.simulator import SimulatorMailGateway

from helpers import FakeEngine, make_config, make_mail, raw_message


def _submit(service, source: Path) -> dict:
    return service.submit_offline_sources(
        sender="tester@example.com",
        destination="法国",
        source_paths=[source],
    )


def _delivery_row(service) -> dict:
    rows = [row for row in service.outbox.list_recent() if row["purpose"] == "delivery"]
    assert len(rows) == 1
    return rows[0]


def test_dry_run_delivery_never_calls_smtp(service, gateway, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    assert task["status"] == state.READY_TO_SEND
    row = _delivery_row(service)
    assert row["status"] == "dry_run"
    assert row["recipient"] == "tester@example.com"
    assert gateway.sent == []
    assert gateway.send_attempts == 0
    assert row["message_id"].startswith(f"cater.{task['id']}.delivery.")
    assert row["attachments"] and Path(row["attachments"][0]["path"]).is_file()


def test_release_dry_run_requires_live_switches(service, offline_source: Path) -> None:
    _submit(service, offline_source)
    row = _delivery_row(service)
    with pytest.raises(SendNotAllowedError):
        service.release_dry_run(row["id"], actor="test")
    assert service.outbox.get(row["id"])["status"] == "dry_run"


def test_release_after_audited_enable_sends(
    service, gateway, offline_source: Path
) -> None:
    _submit(service, offline_source)
    row = _delivery_row(service)
    service.update_settings(
        {"sending.dry_run": False, "sending.enabled": True},
        actor="test-admin",
        reason="live-send-test",
    )
    sent = service.release_dry_run(row["id"], actor="test-admin")
    assert sent["status"] == "sent"
    assert gateway.send_attempts == 1
    assert len(gateway.sent) == 1
    assert service.tasks.require(row["task_id"])["status"] == state.COMPLETED


def test_live_send_success(live_config, service_factory, offline_source: Path) -> None:
    gateway = SimulatorMailGateway()
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    task = _submit(service, offline_source)
    assert task["status"] == state.COMPLETED
    row = _delivery_row(service)
    assert row["status"] == "sent"
    assert gateway.send_attempts == 1
    message = gateway.sent[0]
    assert message.recipient == "tester@example.com"
    assert message.message_id == row["message_id"]
    assert message.attachments and message.attachments[0].filename == "result.xlsx"
    assert f"[CATER {task['id']} v1]" in message.subject


def test_live_rejection_keeps_task_ready_and_retry_sends(
    live_config, service_factory, offline_source: Path
) -> None:
    gateway = SimulatorMailGateway(fail_mode="reject")
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    task = _submit(service, offline_source)
    row = _delivery_row(service)
    assert row["status"] == "failed"
    assert service.tasks.require(task["id"])["status"] == state.READY_TO_SEND
    assert gateway.sent == []

    gateway.fail_mode = "accept"
    sent = service.delivery.retry_failed(row["id"], actor="test-admin")
    assert sent["status"] == "sent"
    assert len(gateway.sent) == 1
    assert service.tasks.require(task["id"])["status"] == state.COMPLETED


def test_unknown_before_accept_requires_manual_confirmation(
    live_config, service_factory, offline_source: Path
) -> None:
    gateway = SimulatorMailGateway(fail_mode="unknown_before_accept")
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    task = _submit(service, offline_source)
    row = _delivery_row(service)
    assert row["status"] == "send_unknown"
    assert service.tasks.require(task["id"])["status"] == state.SEND_UNKNOWN
    assert gateway.sent == []
    assert gateway.send_attempts == 1

    assert service.recheck_unknown(row["id"], actor="test-admin") is False
    assert service.outbox.get(row["id"])["status"] == "send_unknown"

    pending = service.confirm_unknown_not_sent(row["id"], actor="test-admin")
    assert pending["status"] == "pending"
    assert service.tasks.require(task["id"])["status"] == state.READY_TO_SEND

    # The task was only returned to READY_TO_SEND; a resend needs an explicit
    # dispatch, and this time the simulator accepts it.
    gateway.fail_mode = "accept"
    sent = service.delivery.dispatch(row["id"], actor="test-admin")
    assert sent["status"] == "sent"
    assert gateway.send_attempts == 2
    assert service.tasks.require(task["id"])["status"] == state.COMPLETED


def test_unknown_after_accept_is_resolved_by_sent_folder(
    live_config, service_factory, offline_source: Path
) -> None:
    gateway = SimulatorMailGateway(fail_mode="unknown_after_accept")
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    task = _submit(service, offline_source)
    row = _delivery_row(service)
    # Dispatch performs a best-effort Sent-folder recheck and must not resend.
    assert row["status"] == "sent"
    assert gateway.send_attempts == 1
    assert len(gateway.sent) == 1
    assert service.tasks.require(task["id"])["status"] == state.COMPLETED


def test_admin_may_confirm_unknown_as_sent(
    live_config, service_factory, offline_source: Path
) -> None:
    gateway = SimulatorMailGateway(fail_mode="unknown_before_accept")
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    task = _submit(service, offline_source)
    row = _delivery_row(service)
    confirmed = service.confirm_unknown_sent(row["id"], actor="test-admin")
    assert confirmed["status"] == "sent"
    assert service.tasks.require(task["id"])["status"] == state.COMPLETED


def test_interrupted_sending_row_recovers_as_unknown(
    service_factory, config, gateway, offline_source: Path
) -> None:
    service = service_factory(config, engine=FakeEngine(), gateway=gateway)
    task = _submit(service, offline_source)
    row = _delivery_row(service)
    service.outbox.mark_pending(row["id"])
    service.outbox.mark_sending(row["id"])
    service.close()

    restarted = service_factory(config, engine=FakeEngine(), gateway=gateway)
    restarted.recovery_report["uncertain_outbox"]
    recovered = restarted.outbox.get(row["id"])
    assert recovered["status"] == "send_unknown"
    assert restarted.tasks.require(task["id"])["status"] == state.SEND_UNKNOWN
    assert gateway.send_attempts == 0


def test_interrupted_processing_becomes_retry_pending(
    service_factory, config, gateway, offline_source: Path
) -> None:
    service = service_factory(config, engine=FakeEngine(), gateway=gateway)
    task = _submit(service, offline_source)
    service.db.insert(
        "UPDATE tasks SET status = ? WHERE id = ?",
        (state.PROCESSING, task["id"]),
    )
    service.close()

    restarted = service_factory(config, engine=FakeEngine(), gateway=gateway)
    assert task["id"] in restarted.recovery_report["interrupted_tasks"]
    recovered = restarted.tasks.require(task["id"])
    assert recovered["status"] == state.RETRY_PENDING
    assert gateway.send_attempts == 0
    assert restarted.outbox.for_task(task["id"])


def test_duplicate_question_is_deduplicated(service, gateway) -> None:
    first = service.ingest(
        raw_message(
            make_mail(message_id="<q1@example.com>", text="附件"),
            uid=1,
        )
    )
    # No destination and no attachment -> waiting; a second processing run
    # produces the same issue, but question dedupe keeps one row.
    service.process_task(first.task_id, actor="test")
    questions = [row for row in service.outbox.list_recent() if row["purpose"] == "question"]
    assert len(questions) == 1
    assert gateway.sent == []


def test_same_version_delivery_is_deduplicated(service, offline_source: Path) -> None:
    task = _submit(service, offline_source)
    first = _delivery_row(service)
    artifacts = service.tasks.artifacts(task["id"], int(task["version"]))
    again, created = service.delivery.enqueue_delivery(
        task,
        record_count=1,
        artifacts=artifacts,
        content_hash=task.get("content_hash"),
    )
    assert created is False
    assert again["id"] == first["id"]
    deliveries = [
        row for row in service.outbox.list_recent() if row["purpose"] == "delivery"
    ]
    assert len(deliveries) == 1


def test_explicit_same_content_reprocess_creates_new_delivery(
    live_config, service_factory, offline_source: Path
) -> None:
    gateway = SimulatorMailGateway()
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    task = _submit(service, offline_source)
    assert task["status"] == state.COMPLETED
    first = _delivery_row(service)
    assert first["version"] == 1
    assert len(gateway.sent) == 1

    reprocessed = service.reprocess_task(task["id"], actor="test-admin")
    assert reprocessed["version"] == 2
    assert reprocessed["status"] == state.COMPLETED

    deliveries = [
        row for row in service.outbox.list_recent() if row["purpose"] == "delivery"
    ]
    assert len(deliveries) == 2
    assert {row["version"] for row in deliveries} == {1, 2}
    assert len({row["dedupe_key"] for row in deliveries}) == 2
    assert len(gateway.sent) == 2
    assert all(message.attachments for message in gateway.sent)