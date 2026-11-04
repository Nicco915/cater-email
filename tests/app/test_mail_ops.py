"""Mail self-service closed loop: 查询状态 / 重试 / 确认重新处理, bounded
technical retries with owner notification, and executable same-name
attachment replacement. All outbound mail goes to the simulator outbox only.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cater.app import state
from cater.app.errors import MailAuthError, MailTransportError, SendRejectedError
from cater.app.mail.simulator import SimulatorMailGateway
from cater.app.util import parse_utc

from helpers import FakeEngine, make_config, make_mail, raw_message


def _status_rows(service, task_id: str) -> list[dict]:
    return [row for row in service.outbox.for_task(task_id) if row["purpose"] == "status"]


def _notice_rows(service, task_id: str) -> list[dict]:
    return [row for row in service.outbox.for_task(task_id) if row["purpose"] == "notice"]


def _waiting_task(service, engine, *, uid: int = 1, data: bytes = b"version-one") -> str:
    engine.mode = "waiting"
    result = service.ingest(
        raw_message(
            make_mail(
                message_id=f"<waiting-{uid}@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", data),),
            ),
            uid=uid,
        )
    )
    assert service.tasks.require(result.task_id)["status"] == state.WAITING_INPUT
    engine.mode = "ready"
    return str(result.task_id)


def _exhaust_retries(service, task_id: str) -> None:
    while service.tasks.require(task_id)["status"] != state.FAILED:
        service.db.insert(
            "UPDATE tasks SET next_retry_at = NULL WHERE id = ?", (task_id,)
        )
        service.retry_pending_tasks(limit=5, actor="test")


# ----------------------------------------------------------------------
# 查询状态
# ----------------------------------------------------------------------
def test_status_query_by_replying_to_notification(service, engine) -> None:
    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-q@example.com>",
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
    calls_before = len(engine.calls)
    task_before = service.tasks.require(task_id)

    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="查询状态",
        in_reply_to=question["message_id"],
        message_id="<query-1@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.status_query is True

    # Read-only: no recompute, no version bump, no state change.
    task_after = service.tasks.require(task_id)
    assert task_after["version"] == task_before["version"]
    assert task_after["status"] == state.WAITING_INPUT
    assert task_after["updated_at"] == task_before["updated_at"]
    assert len(engine.calls) == calls_before

    reports = _status_rows(service, task_id)
    assert len(reports) == 1
    body = reports[0]["body"]
    assert task_id in body
    assert "等待您补充资料" in body
    assert "缺少数量" in body  # concrete missing item, business wording
    assert "MISSING_QTY" not in body  # internal codes stay hidden


def test_status_query_new_mail_with_task_id(service, engine) -> None:
    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-id@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    query = make_mail(
        text=f"任务编号：{task_id}\n查询状态",
        message_id="<query-new@example.com>",
    )
    result = service.ingest(raw_message(query, uid=2))
    assert result.status_query is True
    assert len(_status_rows(service, task_id)) == 1


def test_status_query_without_reference_gets_generic_guidance(service) -> None:
    result = service.ingest(
        raw_message(make_mail(text="查询状态", message_id="<query-noid@example.com>"), uid=1)
    )
    assert result.filter_reason == "command_without_task_reference"
    assert service.tasks.list() == []
    rows = service.outbox.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["purpose"] == "guidance"
    assert row["task_id"] is None  # no task information is attached or leaked
    assert row["recipient"] == "tester@example.com"
    assert "任务编号" in row["body"] and "查询状态" in row["body"]
    # Guidance may contain the placeholder example "task_xxx", but no real task id.
    assert re.search(r"task_[0-9a-f]{8,}", row["body"]) is None
    # Redelivery of the same mail never produces a second guidance message.
    replay = service.ingest(
        raw_message(make_mail(text="查询状态", message_id="<query-noid@example.com>"), uid=2)
    )
    assert replay.filter_reason == "command_without_task_reference"
    assert len(
        [r for r in service.outbox.list_recent() if r["purpose"] == "guidance"]
    ) == 1  # the same guidance is never sent twice for the replayed mail


def test_taskless_guidance_respects_send_protection(live_config, service_factory) -> None:
    gateway = SimulatorMailGateway()
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    result = service.ingest(
        raw_message(make_mail(text="重试", message_id="<live-guidance@example.com>"), uid=1)
    )
    assert result.filter_reason == "command_without_task_reference"
    rows = service.outbox.list_recent()
    assert len(rows) == 1 and rows[0]["purpose"] == "guidance"
    assert rows[0]["status"] == "sent"  # allowlisted recipient, live enabled
    assert gateway.sent[-1].recipient == "tester@example.com"
    assert "重试" in gateway.sent[-1].body


def test_status_query_with_ambiguous_reference_is_filtered(service, engine) -> None:
    engine.mode = "waiting"
    first = service.ingest(
        raw_message(make_mail(text="目的国：法国", attachments=(("a.xlsx", b"a"),)), uid=1)
    )
    second = service.ingest(
        raw_message(make_mail(text="目的国：法国", attachments=(("b.xlsx", b"b"),)), uid=2)
    )
    mail = make_mail(
        text=f"任务编号：{first.task_id}\n任务编号：{second.task_id}\n查询状态",
        message_id="<query-amb@example.com>",
    )
    result = service.ingest(raw_message(mail, uid=3))
    assert result.filter_reason == "ambiguous_task_reference"
    assert _status_rows(service, str(first.task_id)) == []
    assert _status_rows(service, str(second.task_id)) == []
    # The sender still gets task-free guidance on how to associate a command.
    rows = [r for r in service.outbox.list_recent() if r["purpose"] == "guidance"]
    assert len(rows) == 1 and rows[0]["task_id"] is None
    assert "任务编号" in rows[0]["body"]


def test_status_query_from_non_owner_leaks_nothing(tmp_path, service_factory, engine) -> None:
    config = make_config(
        tmp_path, test_addresses=("tester@example.com", "stranger@example.com")
    )
    service = service_factory(config, engine=engine)
    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(text="目的国：法国", attachments=(("明细.xlsx", b"data"),)), uid=1
        )
    )
    task_id = str(created.task_id)
    mail = make_mail(
        sender="stranger@example.com",
        text=f"任务编号：{task_id}\n查询状态",
        message_id="<query-stranger@example.com>",
    )
    result = service.ingest(raw_message(mail, uid=2))
    assert result.filter_reason == "supplement_from_non_owner"
    assert _status_rows(service, task_id) == []
    # Generic no-permission guidance, with no task details attached.
    rows = [r for r in service.outbox.list_recent() if r["purpose"] == "guidance"]
    assert len(rows) == 1 and rows[0]["task_id"] is None
    assert "原发件人" in rows[0]["body"]
    assert task_id not in rows[0]["body"]


def test_quoted_status_command_is_not_executed(service, engine) -> None:
    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-quoted@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    mail = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="好的\n-----Original Message-----\n查询状态",
        message_id="<query-quoted@example.com>",
    )
    result = service.ingest(raw_message(mail, uid=2))
    assert result.status_query is False
    assert _status_rows(service, task_id) == []


def test_repeated_query_mail_is_answered_once(service, engine) -> None:
    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-replay@example.com>",
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
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="查询状态",
        in_reply_to=question["message_id"],
        message_id="<query-replay@example.com>",
    )
    first = service.ingest(raw_message(reply, uid=2))
    assert first.status_query is True
    assert len(_status_rows(service, task_id)) == 1
    # The same mail redelivered under a new mailbox coordinate is a replay.
    replay = service.ingest(raw_message(reply, uid=3))
    assert replay.filter_reason == "duplicate_status_query"
    assert len(_status_rows(service, task_id)) == 1


def test_new_query_mail_gets_fresh_status(service, engine) -> None:
    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-fresh@example.com>",
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
    first_reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="查询状态",
        in_reply_to=question["message_id"],
        message_id="<query-fresh-1@example.com>",
    )
    service.ingest(raw_message(first_reply, uid=2))
    assert len(_status_rows(service, task_id)) == 1

    # The state changes (destination supplement resolves the missing qty
    # scenario by switching the engine to ready).
    engine.mode = "ready"
    fix = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text=f"任务编号：{task_id}\n修正：P1.qty=4",
        message_id="<fix-fresh@example.com>",
    )
    fixed = service.ingest(raw_message(fix, uid=3))
    assert service.tasks.require(task_id)["status"] == state.READY_TO_SEND

    new_query = make_mail(
        subject=f"Re: [CATER {task_id} v2] 配件发票",
        text="查询状态",
        message_id="<query-fresh-2@example.com>",
    )
    service.ingest(raw_message(new_query, uid=4))
    reports = _status_rows(service, task_id)
    assert len(reports) == 2
    # Outbox rows sort by text id, not insertion order; pick by content.
    assert any("等待您补充资料" in row["body"] for row in reports)
    assert any("等待发送" in row["body"] for row in reports)


# ----------------------------------------------------------------------
# command hygiene
# ----------------------------------------------------------------------
def test_mixed_commands_are_rejected_with_guidance(service, engine) -> None:
    task_id = _waiting_task(service, engine)
    mail = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text=f"任务编号：{task_id}\n查询状态\n重试",
        message_id="<mixed@example.com>",
    )
    result = service.ingest(raw_message(mail, uid=2))
    assert result.filter_reason == "command_mixed"
    assert service.tasks.require(task_id)["version"] == 1
    notices = _notice_rows(service, task_id)
    assert len(notices) == 1
    assert "查询状态" in notices[0]["body"] and "重试" in notices[0]["body"]


def test_command_with_attachment_is_rejected_with_guidance(service, engine) -> None:
    task_id = _waiting_task(service, engine)
    calls_before = len(engine.calls)
    mail = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重试",
        attachments=(("明细.xlsx", b"other"),),
        message_id="<cmd-att@example.com>",
    )
    result = service.ingest(raw_message(mail, uid=2))
    assert result.filter_reason == "command_with_payload"
    assert service.tasks.require(task_id)["version"] == 1
    notices = _notice_rows(service, task_id)
    assert len(notices) == 1
    assert "单独发送" in notices[0]["body"]
    assert len(engine.calls) == calls_before


# ----------------------------------------------------------------------
# 重试
# ----------------------------------------------------------------------
def test_mail_retry_after_technical_failure_reuses_current_version(service, engine) -> None:
    engine.mode = "core_unavailable"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<retry-origin@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    task = service.tasks.require(task_id)
    assert task["status"] == state.RETRY_PENDING
    assert task["retry_count"] == 1
    inputs_before = [item["id"] for item in service.tasks.inputs(task_id, 1)]
    assert len(engine.calls) == 1

    engine.mode = "ready"
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重试",
        message_id="<retry-1@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.retry_command is True
    task = service.tasks.require(task_id)
    assert task["status"] == state.READY_TO_SEND
    assert task["version"] == 1  # retry never creates a version
    assert task["retry_count"] == 0  # success closes the failure episode
    assert [item["id"] for item in service.tasks.inputs(task_id, 1)] == inputs_before
    assert len(engine.calls) == 2

    replay = service.ingest(raw_message(reply, uid=3))
    assert replay.filter_reason == "duplicate_retry_command"
    assert len(engine.calls) == 2


def test_mail_retry_on_completed_is_rejected(live_config, service_factory) -> None:
    gateway = SimulatorMailGateway()
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<done@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    assert service.tasks.require(task_id)["status"] == state.COMPLETED
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重试",
        message_id="<retry-done@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.filter_reason == "retry_completed"
    task = service.tasks.require(task_id)
    assert task["status"] == state.COMPLETED and task["version"] == 1
    notices = _notice_rows(service, task_id)
    assert len(notices) == 1 and "确认重新处理" in notices[0]["body"]


def test_mail_retry_on_waiting_input_gets_per_mail_answer(service, engine) -> None:
    task_id = _waiting_task(service, engine)
    questions_before = len(
        [row for row in service.outbox.for_task(task_id) if row["purpose"] == "question"]
    )
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重试",
        message_id="<retry-wait@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.retry_waiting is True
    assert result.note == "retry_waiting_input"
    assert service.tasks.require(task_id)["status"] == state.WAITING_INPUT
    answers = [
        row
        for row in service.outbox.for_task(task_id)
        if row["purpose"] == "notice" and "暂时不能重试" in row["body"]
    ]
    assert len(answers) == 1
    assert "缺少数量" in answers[0]["body"]  # concrete missing items
    questions_after = len(
        [row for row in service.outbox.for_task(task_id) if row["purpose"] == "question"]
    )
    assert questions_after == questions_before  # old question rows untouched

    # A new active command mail receives its own answer...
    second = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重试",
        message_id="<retry-wait-2@example.com>",
    )
    service.ingest(raw_message(second, uid=3))
    answers = [
        row
        for row in service.outbox.for_task(task_id)
        if row["purpose"] == "notice" and "暂时不能重试" in row["body"]
    ]
    assert len(answers) == 2
    # ...while a redelivered copy of the same mail is a replay and adds none.
    replay = service.ingest(raw_message(second, uid=4))
    assert replay.filter_reason == "duplicate_retry_command"
    answers = [
        row
        for row in service.outbox.for_task(task_id)
        if row["purpose"] == "notice" and "暂时不能重试" in row["body"]
    ]
    assert len(answers) == 2


def test_mail_retry_never_bypasses_send_unknown(service, engine) -> None:
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<unknown-origin@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    assert service.tasks.require(task_id)["status"] == state.READY_TO_SEND
    service.db.insert(
        "UPDATE tasks SET status = ? WHERE id = ?", (state.SEND_UNKNOWN, task_id)
    )
    calls_before = len(engine.calls)
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重试",
        message_id="<retry-unknown@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.filter_reason == "retry_send_unknown"
    assert service.tasks.require(task_id)["status"] == state.SEND_UNKNOWN
    notices = _notice_rows(service, task_id)
    assert len(notices) == 1 and "人工确认" in notices[0]["body"]
    assert len(engine.calls) == calls_before


@pytest.mark.parametrize(
    ("forced_status", "reason"),
    [
        (state.PROCESSING, "retry_in_progress"),
        (state.READY_TO_SEND, "retry_already_ready"),
        (state.CANCELLED, "retry_cancelled"),
    ],
)
def test_mail_retry_rejected_by_status(service, engine, forced_status, reason) -> None:
    task_id = _waiting_task(service, engine)
    service.db.insert(
        "UPDATE tasks SET status = ? WHERE id = ?", (forced_status, task_id)
    )
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重试",
        message_id=f"<retry-{reason}@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.filter_reason == reason
    assert service.tasks.require(task_id)["status"] == forced_status
    assert len(_notice_rows(service, task_id)) == 1


# ----------------------------------------------------------------------
# bounded automatic retry with persisted notification
# ----------------------------------------------------------------------
def test_retry_budget_exhaustion_notifies_once_and_survives_restart(
    config, service_factory
) -> None:
    engine = FakeEngine()
    engine.mode = "core_unavailable"
    service = service_factory(config, engine=engine, gateway=SimulatorMailGateway())
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<budget@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    assert service.tasks.require(task_id)["retry_count"] == 1

    _exhaust_retries(service, task_id)
    task = service.tasks.require(task_id)
    assert task["status"] == state.FAILED
    assert task["retry_count"] == 4  # initial failure + 3 automatic retries
    assert task["failure_notified"] == 1

    notices = _notice_rows(service, task_id)
    assert len(notices) == 1  # one continuous failure, one notification
    body = notices[0]["body"]
    assert "自动重试已用完" in body  # honest about the retry count
    assert "资料已保存" in body
    assert "重试" in body
    assert "core not installed" not in body
    assert "CoreUnavailable" not in body

    # Restart: counters persist, nothing is re-notified or reset.
    service.close()
    broken = FakeEngine()
    broken.mode = "core_unavailable"
    restarted = service_factory(config, engine=broken, gateway=SimulatorMailGateway())
    task = restarted.tasks.require(task_id)
    assert task["status"] == state.FAILED
    assert task["retry_count"] == 4
    assert task["failure_notified"] == 1
    assert len(_notice_rows(restarted, task_id)) == 1


def test_retry_backoff_grows(config, service_factory) -> None:
    engine = FakeEngine()
    engine.mode = "core_unavailable"
    service = service_factory(config, engine=engine, gateway=SimulatorMailGateway())
    created = service.ingest(
        raw_message(make_mail(text="目的国：法国", attachments=(("明细.xlsx", b"data"),)), uid=1)
    )
    task_id = str(created.task_id)

    def next_retry_in_seconds() -> float:
        value = parse_utc(service.tasks.require(task_id)["next_retry_at"])
        assert value is not None
        return (value - datetime.now(timezone.utc)).total_seconds()

    first = next_retry_in_seconds()
    assert 55 <= first <= 120
    service.db.insert("UPDATE tasks SET next_retry_at = NULL WHERE id = ?", (task_id,))
    service.retry_pending_tasks(limit=5, actor="test")
    second = next_retry_in_seconds()
    assert 290 <= second <= 360
    assert second > first


def test_recovered_task_succeeds_and_resets_budget(config, service_factory) -> None:
    engine = FakeEngine()
    engine.mode = "core_unavailable"
    service = service_factory(config, engine=engine, gateway=SimulatorMailGateway())
    created = service.ingest(
        raw_message(make_mail(text="目的国：法国", attachments=(("明细.xlsx", b"data"),)), uid=1)
    )
    task_id = str(created.task_id)
    assert service.tasks.require(task_id)["status"] == state.RETRY_PENDING

    engine.mode = "ready"
    service.db.insert("UPDATE tasks SET next_retry_at = NULL WHERE id = ?", (task_id,))
    service.retry_pending_tasks(limit=5, actor="test")
    task = service.tasks.require(task_id)
    assert task["status"] == state.READY_TO_SEND
    assert task["retry_count"] == 0
    assert task["next_retry_at"] is None


def test_owner_retry_after_failure_earns_one_new_feedback(config, service_factory) -> None:
    engine = FakeEngine()
    engine.mode = "core_unavailable"
    service = service_factory(config, engine=engine, gateway=SimulatorMailGateway())
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<episode@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    _exhaust_retries(service, task_id)
    assert service.tasks.require(task_id)["status"] == state.FAILED
    assert len(_notice_rows(service, task_id)) == 1

    # Explicit owner retry: new failure episode, one new feedback allowed.
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重试",
        message_id="<episode-retry@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.retry_command is True
    task = service.tasks.require(task_id)
    assert task["status"] == state.RETRY_PENDING
    assert task["retry_count"] == 1
    assert task["failure_notified"] == 0

    _exhaust_retries(service, task_id)
    task = service.tasks.require(task_id)
    assert task["status"] == state.FAILED
    notices = _notice_rows(service, task_id)
    assert len(notices) == 2
    assert len({row["dedupe_key"] for row in notices}) == 2


# ----------------------------------------------------------------------
# attachment replacement
# ----------------------------------------------------------------------
def test_same_name_attachment_reply_without_directives_replaces_input(service, engine) -> None:
    task_id = _waiting_task(service, engine, data=b"version-one")
    v1_inputs = service.tasks.inputs(task_id, 1)
    assert len(v1_inputs) == 1
    old_path = Path(v1_inputs[0]["path"])
    assert old_path.read_bytes() == b"version-one"

    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="请查收更正后的附件",  # free-form text only, no extra fields required
        attachments=(("明细.xlsx", b"version-two"),),
        message_id="<replace-1@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.supplement is True and result.should_process is True

    task = service.tasks.require(task_id)
    assert task["version"] == 2
    assert task["status"] == state.READY_TO_SEND

    v2_inputs = service.tasks.inputs(task_id, 2)
    assert len(v2_inputs) == 1
    assert v2_inputs[0]["filename"] == "明细.xlsx"
    new_path = Path(v2_inputs[0]["path"])
    assert new_path.read_bytes() == b"version-two"

    # Old version row and old file stay untouched (audit retention).
    assert [item["id"] for item in service.tasks.inputs(task_id, 1)] == [v1_inputs[0]["id"]]
    assert old_path.read_bytes() == b"version-one"

    # The core request sees exactly the new file, never old+new together, and
    # the business mail payload still comes from the origin mail.
    request = engine.calls[-1]
    assert len(request["sources"]) == 1
    assert Path(request["sources"][0]["path"]).read_bytes() == b"version-two"
    assert request["mail"]["message_id"] == "waiting-1@example.com"


def test_different_name_attachment_pauses_with_guidance(service, engine) -> None:
    task_id = _waiting_task(service, engine)
    calls_before = len(engine.calls)
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="请查收更正后的附件",
        attachments=(("新名字.xlsx", b"other"),),
        message_id="<rename@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.note == "attachment_replacement_blocked"
    task = service.tasks.require(task_id)
    assert task["version"] == 1
    assert task["status"] == state.WAITING_INPUT
    codes = {issue["code"] for issue in service.tasks.issues(task_id)}
    assert "ATTACHMENT_NAME_MISMATCH" in codes
    questions = [row for row in service.outbox.for_task(task_id) if row["purpose"] == "question"]
    guidance = [row for row in questions if "沿用原附件名" in row["body"]]
    assert len(guidance) == 1
    assert "明细.xlsx" in guidance[0]["body"]
    assert len(engine.calls) == calls_before


def test_conflicting_same_name_attachments_are_rejected(service, engine) -> None:
    task_id = _waiting_task(service, engine)
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="请查收更正后的附件",
        attachments=(("明细.xlsx", b"one"), ("明细.xlsx", b"two")),
        message_id="<conflict@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.note == "attachment_replacement_blocked"
    assert service.tasks.require(task_id)["version"] == 1
    codes = {issue["code"] for issue in service.tasks.issues(task_id)}
    assert "ATTACHMENT_NAME_CONFLICT" in codes


def test_staging_write_failure_leaves_no_partial_update(
    service, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _waiting_task(service, engine)
    calls_before = len(engine.calls)
    real_write = Path.write_bytes

    def flaky(self: Path, data: bytes) -> int:
        if "incoming" in str(self):
            raise OSError("simulated disk failure")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="请查收更正后的附件",
        attachments=(("明细.xlsx", b"version-two"),),
        message_id="<diskfail@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    monkeypatch.undo()
    assert result.note == "attachment_replacement_blocked"
    task = service.tasks.require(task_id)
    assert task["version"] == 1  # no half-updated version
    assert [item["filename"] for item in service.tasks.inputs(task_id, 1)] == ["明细.xlsx"]
    assert not (service.config.tasks_dir / task_id / "v2").exists()
    incoming = service.config.tasks_dir / task_id / "incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []
    codes = {issue["code"] for issue in service.tasks.issues(task_id)}
    assert "ATTACHMENT_WRITE_FAILED" in codes
    assert len(engine.calls) == calls_before


def test_owner_resupplies_attachment_after_unrecoverable_failure(service, engine) -> None:
    task_id = _waiting_task(service, engine, data=b"version-one")
    # Simulate saved input data becoming unreadable on disk.
    saved = Path(service.tasks.inputs(task_id, 1)[0]["path"])
    saved.unlink()
    failed = service.process_task(task_id, actor="test")
    assert failed["status"] == state.FAILED  # non-recoverable: no auto retry
    task = service.tasks.require(task_id)
    assert task["retry_count"] == 0
    assert len(_notice_rows(service, task_id)) == 1

    # The owner simply replies with the correct Excel under the same name.
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="重新提供附件",
        attachments=(("明细.xlsx", b"version-two"),),
        message_id="<resupply@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.should_process is True
    task = service.tasks.require(task_id)
    assert task["version"] == 2
    assert task["status"] == state.READY_TO_SEND
    request = engine.calls[-1]
    assert len(request["sources"]) == 1
    assert Path(request["sources"][0]["path"]).read_bytes() == b"version-two"


# ----------------------------------------------------------------------
# first supply for tasks without any saved workbook
# ----------------------------------------------------------------------
def test_first_supply_attachment_accepted_when_task_has_no_inputs(service, engine) -> None:
    # Original mail carried only an unsupported file: the task waits with no
    # business workbook saved at all.
    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-noxlsx@example.com>",
                text="目的国：法国",
                attachments=(("说明.txt", b"notes"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    task = service.tasks.require(task_id)
    assert task["status"] == state.WAITING_INPUT
    assert service.tasks.inputs(task_id, 1) == []

    # The owner replies with the single required Excel: accepted as the
    # first supply instead of being rejected for an unmatched name.
    engine.mode = "ready"
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="请查收所需 Excel 附件",
        attachments=(("明细.xlsx", b"first-supply"),),
        message_id="<first-supply@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.supplement is True and result.should_process is True
    task = service.tasks.require(task_id)
    assert task["version"] == 2
    assert task["status"] == state.READY_TO_SEND
    v2_inputs = service.tasks.inputs(task_id, 2)
    assert len(v2_inputs) == 1
    assert Path(v2_inputs[0]["path"]).read_bytes() == b"first-supply"
    request = engine.calls[-1]
    assert len(request["sources"]) == 1


def test_multiple_attachments_without_inputs_pause_with_role_guidance(
    service, engine
) -> None:
    engine.mode = "waiting"
    created = service.ingest(
        raw_message(
            make_mail(
                message_id="<origin-multi@example.com>",
                text="目的国：法国",
                attachments=(("说明.txt", b"notes"),),
            ),
            uid=1,
        )
    )
    task_id = str(created.task_id)
    assert service.tasks.inputs(task_id, 1) == []
    calls_before = len(engine.calls)

    # Several different-name attachments with no saved workbook to match
    # against: roles are undecidable, so nothing is adopted silently.
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text="两批资料见附件",
        attachments=(("发票.xlsx", b"one"), ("装箱.xlsx", b"two")),
        message_id="<multi-supply@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.note == "attachment_replacement_blocked"
    task = service.tasks.require(task_id)
    assert task["version"] == 1 and task["status"] == state.WAITING_INPUT
    codes = {issue["code"] for issue in service.tasks.issues(task_id)}
    assert "ATTACHMENT_ROLE_UNKNOWN" in codes
    questions = [
        row for row in service.outbox.for_task(task_id) if row["purpose"] == "question"
    ]
    guidance = [row for row in questions if "无法判别" in row["body"]]
    assert len(guidance) == 1
    assert not (service.config.tasks_dir / task_id / "v2").exists()
    assert len(engine.calls) == calls_before


# ----------------------------------------------------------------------
# atomic supplement commit (fault injection)
# ----------------------------------------------------------------------
def _two_input_waiting_task(service, engine) -> str:
    engine.mode = "waiting"
    result = service.ingest(
        raw_message(
            make_mail(
                message_id="<waiting-two@example.com>",
                text="目的国：法国",
                attachments=(("明细.xlsx", b"data-a"), ("装箱.xlsx", b"data-b")),
            ),
            uid=1,
        )
    )
    assert service.tasks.require(result.task_id)["status"] == state.WAITING_INPUT
    engine.mode = "ready"
    return str(result.task_id)


def _supplement_reply(task_id: str, *, message_id: str, uid: int) -> bytes:
    return raw_message(
        make_mail(
            subject=f"Re: [CATER {task_id} v1] 配件发票",
            text="国家映射：高卢=FRA\n目的国：高卢",
            attachments=(("明细.xlsx", b"new-a"), ("装箱.xlsx", b"new-b")),
            message_id=message_id,
        ),
        uid=uid,
    )


def _assert_rollback_invariants(service, task_id: str) -> None:
    """Old active version, inputs, destination and aliases all unchanged."""
    task = service.tasks.require(task_id)
    assert task["version"] == 1
    assert task["status"] == state.WAITING_INPUT
    assert task["destination_name"] == "法国"
    assert task["destination_alpha3"] == "FRA"
    v1 = service.tasks.inputs(task_id, 1)
    assert {item["filename"] for item in v1} == {"明细.xlsx", "装箱.xlsx"}
    contents = sorted(Path(item["path"]).read_bytes() for item in v1)
    assert contents == [b"data-a", b"data-b"]
    # The pending alias was rolled back with the same transaction.
    assert service.references.resolve("高卢") is None
    versions = [
        row["version"]
        for row in service.db.fetchall(
            "SELECT version FROM task_versions WHERE task_id = ?", (task_id,)
        )
    ]
    assert versions == [1]
    # No leftover files from the aborted commit.
    assert not (service.config.tasks_dir / task_id / "v2").exists()
    incoming = service.config.tasks_dir / task_id / "incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_supplement_commit_rolls_back_when_second_file_move_fails(
    service, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _two_input_waiting_task(service, engine)
    real_replace = os.replace
    moves = {"count": 0}

    def flaky_replace(src, dst):
        # Fail while moving the SECOND staged file into the future version's
        # inputs directory; the first file was already moved.
        if f"{os.sep}v2{os.sep}" in str(dst):
            moves["count"] += 1
            if moves["count"] == 2:
                raise OSError("simulated move failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    result = service.ingest(
        _supplement_reply(task_id, message_id="<move-fail@example.com>", uid=2)
    )
    monkeypatch.undo()
    assert result.note == "supplement_attachment_write_failed"
    _assert_rollback_invariants(service, task_id)
    codes = {issue["code"] for issue in service.tasks.issues(task_id)}
    assert "ATTACHMENT_WRITE_FAILED" in codes

    # Re-sending the same supplement afterwards works normally.
    result = service.ingest(
        _supplement_reply(task_id, message_id="<move-retry@example.com>", uid=3)
    )
    assert result.should_process is True
    task = service.tasks.require(task_id)
    assert task["version"] == 2
    assert task["status"] == state.READY_TO_SEND
    assert service.references.resolve("高卢")["alpha3"] == "FRA"
    v2 = service.tasks.inputs(task_id, 2)
    assert {item["filename"] for item in v2} == {"明细.xlsx", "装箱.xlsx"}
    assert sorted(Path(item["path"]).read_bytes() for item in v2) == [b"new-a", b"new-b"]
    # Old version inputs stay untouched (audit retention).
    v1 = service.tasks.inputs(task_id, 1)
    assert sorted(Path(item["path"]).read_bytes() for item in v1) == [b"data-a", b"data-b"]


def test_supplement_commit_rolls_back_when_input_insert_fails(
    service, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cater.app import tasks as tasks_module

    task_id = _two_input_waiting_task(service, engine)
    real_new_id = tasks_module.new_id
    mints = {"count": 0}

    def flaky_new_id(prefix: str) -> str:
        # Fail while registering the SECOND new input row inside the single
        # supplement transaction; alias and version rows precede it.
        if prefix == "input":
            mints["count"] += 1
            if mints["count"] == 2:
                raise RuntimeError("simulated database failure")
        return real_new_id(prefix)

    monkeypatch.setattr(tasks_module, "new_id", flaky_new_id)
    result = service.ingest(
        _supplement_reply(task_id, message_id="<db-fail@example.com>", uid=2)
    )
    monkeypatch.undo()
    assert result.note == "supplement_apply_failed"
    _assert_rollback_invariants(service, task_id)
    codes = {issue["code"] for issue in service.tasks.issues(task_id)}
    assert "SUPPLEMENT_APPLY_FAILED" in codes

    # Re-sending the same supplement afterwards works normally.
    result = service.ingest(
        _supplement_reply(task_id, message_id="<db-retry@example.com>", uid=3)
    )
    assert result.should_process is True
    task = service.tasks.require(task_id)
    assert task["version"] == 2
    assert task["status"] == state.READY_TO_SEND
    assert service.references.resolve("高卢")["alpha3"] == "FRA"
    assert task["destination_alpha3"] == "FRA"
    v2 = service.tasks.inputs(task_id, 2)
    assert {item["filename"] for item in v2} == {"明细.xlsx", "装箱.xlsx"}


# ----------------------------------------------------------------------
# failure notices describe the failure honestly
# ----------------------------------------------------------------------
def test_one_shot_failure_notice_describes_failure_honestly(service, engine) -> None:
    task_id = _waiting_task(service, engine, data=b"version-one")
    # Make the saved input unreadable: a non-recoverable failure with no
    # automatic retries at all.
    Path(service.tasks.inputs(task_id, 1)[0]["path"]).unlink()
    failed = service.process_task(task_id, actor="test")
    assert failed["status"] == state.FAILED
    task = service.tasks.require(task_id)
    assert task["retry_count"] == 0  # no attempt was spent on auto retries

    notices = _notice_rows(service, task_id)
    assert len(notices) == 1
    body = notices[0]["body"]
    assert "第一次尝试" in body
    assert "未进行自动重试" in body
    assert "请勿重复发送" in body
    # The exhausted-retry wording must not appear for a one-shot failure,
    # and neither may the blanket "all your files are saved" claim.
    assert "连续多次" not in body
    assert "自动重试已用完" not in body
    assert "资料已保存" not in body


# ----------------------------------------------------------------------
# taskless guidance survives every SMTP outcome without fake task calls
# ----------------------------------------------------------------------
def _failing_send(exc: Exception):
    def send(message, *, from_address):
        raise exc

    return send


def _guidance_row(service) -> dict:
    rows = [row for row in service.outbox.list_recent() if row["purpose"] == "guidance"]
    assert len(rows) == 1
    return rows[0]


def _assert_no_fake_task(service) -> None:
    assert service.tasks.list() == []  # no fake "None" task is ever created
    assert service.db.scalar("SELECT COUNT(*) FROM task_events") == 0


def test_taskless_guidance_smtp_reject_marks_failed_without_task_call(
    live_config, service_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SimulatorMailGateway()
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    monkeypatch.setattr(
        gateway, "send", _failing_send(SendRejectedError("smtp 550 blocked"))
    )
    result = service.ingest(
        raw_message(make_mail(text="查询状态", message_id="<g-reject@example.com>"), uid=1)
    )
    assert result.filter_reason == "command_without_task_reference"
    row = _guidance_row(service)
    assert row["status"] == "failed"  # original SMTP outcome is preserved
    assert row["attempts"] == 1
    assert "smtp 550" in str(row["last_error"] or "")
    _assert_no_fake_task(service)

    # The persisted failure stays retryable through the normal outbox action.
    monkeypatch.undo()
    retried = service.delivery.retry_failed(str(row["id"]), actor="test")
    assert retried["status"] == "sent"
    assert gateway.sent[-1].recipient == "tester@example.com"
    _assert_no_fake_task(service)


def test_taskless_guidance_smtp_auth_error_marks_failed_without_task_call(
    live_config, service_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SimulatorMailGateway()
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    monkeypatch.setattr(
        gateway, "send", _failing_send(MailAuthError("smtp 535 auth failed"))
    )
    result = service.ingest(
        raw_message(make_mail(text="重试", message_id="<g-auth@example.com>"), uid=1)
    )
    assert result.filter_reason == "command_without_task_reference"
    row = _guidance_row(service)
    assert row["status"] == "failed"
    assert "535" in str(row["last_error"] or "")
    _assert_no_fake_task(service)


def test_taskless_guidance_transient_failure_retries_then_exhausts(
    live_config, service_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SimulatorMailGateway()
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    monkeypatch.setattr(
        gateway, "send", _failing_send(MailTransportError("connection reset"))
    )
    service.ingest(
        raw_message(make_mail(text="查询状态", message_id="<g-transient@example.com>"), uid=1)
    )
    row = _guidance_row(service)
    assert row["status"] == "pending"  # transient failure is re-queued
    assert row["attempts"] == 1 and row["next_attempt_at"]
    _assert_no_fake_task(service)

    # Recovery: once the transport works again the queued guidance is sent.
    monkeypatch.undo()
    service.db.insert("UPDATE outbox SET next_attempt_at = NULL WHERE id = ?", (row["id"],))
    service.delivery.send_due()
    row = _guidance_row(service)
    assert row["status"] == "sent"
    _assert_no_fake_task(service)

    # A persistently failing transport exhausts to failed without any task call.
    service.db.insert(
        "UPDATE outbox SET status = 'pending', attempts = 0, next_attempt_at = NULL WHERE id = ?",
        (row["id"],),
    )
    monkeypatch.setattr(
        gateway, "send", _failing_send(MailTransportError("connection reset"))
    )
    for expected_attempts in (1, 2, 3):
        service.delivery.send_due()
        row = _guidance_row(service)
        assert row["attempts"] == expected_attempts
        service.db.insert(
            "UPDATE outbox SET next_attempt_at = NULL WHERE id = ?", (row["id"],)
        )
    row = _guidance_row(service)
    assert row["status"] == "failed"
    _assert_no_fake_task(service)


def test_taskless_guidance_unknown_send_never_blind_resends(
    live_config, service_factory
) -> None:
    gateway = SimulatorMailGateway(fail_mode="unknown_before_accept")
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    service.ingest(
        raw_message(make_mail(text="查询状态", message_id="<g-unknown@example.com>"), uid=1)
    )
    row = _guidance_row(service)
    assert row["status"] == "send_unknown"
    assert gateway.send_attempts == 1
    assert gateway.sent == []  # acceptance was never confirmed
    _assert_no_fake_task(service)

    # A negative Sent-folder check does not license a resend.
    assert service.recheck_unknown(str(row["id"]), actor="test") is False
    assert gateway.send_attempts == 1
    row = _guidance_row(service)
    assert row["status"] == "send_unknown"
    _assert_no_fake_task(service)

    # Only the explicit admin confirmation re-queues it; then it sends.
    confirmed = service.confirm_unknown_not_sent(str(row["id"]), actor="test")
    assert confirmed["status"] == "pending"
    gateway.fail_mode = "accept"
    service.delivery.send_due()
    row = _guidance_row(service)
    assert row["status"] == "sent"
    _assert_no_fake_task(service)


def test_taskless_guidance_interrupted_send_recovers_without_task_call(
    live_config, service_factory
) -> None:
    gateway = SimulatorMailGateway()
    service = service_factory(live_config, engine=FakeEngine(), gateway=gateway)
    service.ingest(
        raw_message(make_mail(text="查询状态", message_id="<g-restart@example.com>"), uid=1)
    )
    row = _guidance_row(service)
    assert row["status"] == "sent"
    row_id = str(row["id"])
    # Simulate a crash mid-send: the row is left in 'sending'.
    service.db.insert("UPDATE outbox SET status = 'sending' WHERE id = ?", (row_id,))
    service.close()

    restarted = service_factory(
        live_config, engine=FakeEngine(), gateway=SimulatorMailGateway()
    )
    try:
        # Startup recovery already ran inside the factory: the interrupted
        # taskless row became SEND_UNKNOWN (uncertain, never auto-resent)
        # without creating a fake task or raising a foreign-key error.
        row = next(
            item for item in restarted.outbox.list_recent() if str(item["id"]) == row_id
        )
        assert row["status"] == "send_unknown"
        assert restarted.tasks.list() == []
        assert restarted.db.scalar("SELECT COUNT(*) FROM task_events") == 0
        # A second recovery pass is a clean no-op.
        report = restarted.recover()
        assert report["uncertain_outbox"] == []
        assert report["send_unknown_tasks"] == []
    finally:
        restarted.close()
