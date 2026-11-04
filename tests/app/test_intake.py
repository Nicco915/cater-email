from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from cater.app import state
from cater.app.application import AppService
from cater.app.mail.simulator import SimulatorMailGateway

from helpers import FakeEngine, make_config, make_mail, raw_message


def test_non_allowlisted_sender_is_filtered(service) -> None:
    result = service.ingest(
        raw_message(make_mail(sender="stranger@example.com", text="目的国：法国"))
    )
    assert result.filtered is True
    assert result.filter_reason == "sender_not_allowlisted"
    assert service.tasks.list() == []


def test_self_bounce_and_auto_mail_are_filtered(tmp_path: Path, service_factory) -> None:
    config = make_config(
        tmp_path,
        test_addresses=(
            "tester@example.com",
            "bot@example.com",
            "mailer-daemon@example.com",
        ),
    )
    service = service_factory(config)
    cases = [
        (make_mail(sender="bot@example.com"), "self_message"),
        (make_mail(sender="mailer-daemon@example.com"), "bounce"),
        (make_mail(auto_submitted="auto-replied"), "automated_reply"),
    ]
    for index, (raw, reason) in enumerate(cases, start=1):
        result = service.ingest(raw_message(raw, uid=index))
        assert result.filter_reason == reason
    assert service.tasks.list() == []


def test_test_and_production_allowlists_are_isolated(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        mode="production",
        test_addresses=("tester@example.com",),
        production_addresses=("prod@example.com",),
    )
    service = AppService(
        config, engine=FakeEngine(), gateway=SimulatorMailGateway()
    )
    try:
        blocked = service.ingest(
            raw_message(make_mail(sender="tester@example.com"), uid=1)
        )
        assert blocked.filter_reason == "sender_not_allowlisted"
        accepted = service.ingest(
            raw_message(make_mail(sender="prod@example.com"), uid=2)
        )
        assert accepted.created is True
        assert service.tasks.list()[0]["owner"] == "prod@example.com"
    finally:
        service.close()


def test_first_scan_only_establishes_watermark(service, gateway) -> None:
    gateway.add_message(make_mail(message_id="<old@example.com>"))
    first = service.poll_once()
    assert first.first_scan is True
    assert first.scanned == 0
    assert service.tasks.list() == []

    gateway.add_message(make_mail(message_id="<new@example.com>"))
    second = service.poll_once()
    assert second.first_scan is False
    assert second.created_tasks == 1
    assert len(service.tasks.list()) == 1


def test_history_can_be_processed_when_explicitly_allowed(tmp_path: Path) -> None:
    config = make_config(tmp_path, first_run_new_only=False)
    gateway = SimulatorMailGateway()
    gateway.add_message(make_mail(message_id="<history@example.com>"))
    service = AppService(config, engine=FakeEngine(), gateway=gateway)
    try:
        report = service.poll_once()
        assert report.scanned == 1
        assert report.created_tasks == 1
        assert len(service.tasks.list()) == 1
    finally:
        service.close()


def test_uidvalidity_change_rescans_without_duplicate_tasks(service, gateway) -> None:
    service.poll_once()  # establish an empty watermark
    gateway.add_message(make_mail(message_id="<first@example.com>"))
    service.poll_once()
    gateway.add_message(make_mail(message_id="<second@example.com>"))
    service.poll_once()
    task_count = len(service.tasks.list())
    assert task_count == 2

    gateway.change_uid_validity(2000)
    report = service.poll_once()
    assert report.uidvalidity_changed is True
    # Messages already seen are re-fetched but raw-content dedupe prevents
    # duplicate tasks. (Messages skipped by the initial watermark were never
    # fingerprinted and are outside this assertion.)
    assert report.duplicates >= 2
    assert len(service.tasks.list()) == task_count


def test_duplicate_raw_content_does_not_create_second_task(service) -> None:
    raw = make_mail(message_id="<same@example.com>")
    first = service.ingest(raw_message(raw, uid=1))
    second = service.ingest(raw_message(raw, uid=2))
    assert first.created is True
    assert second.duplicate is True
    assert second.task_id == first.task_id
    assert len(service.tasks.list()) == 1


def test_duplicate_processed_content_creates_notice_but_no_second_delivery(
    service, engine
) -> None:
    engine.content_hash = "same-business-content"
    first = service.ingest(
        raw_message(make_mail(message_id="<one@example.com>", subject="one"), uid=1)
    )
    second = service.ingest(
        raw_message(make_mail(message_id="<two@example.com>", subject="two"), uid=2)
    )
    assert first.task_id != second.task_id
    assert service.tasks.require(first.task_id)["status"] == state.READY_TO_SEND
    assert service.tasks.require(second.task_id)["status"] == state.COMPLETED
    deliveries = [
        row
        for row in service.outbox.list_recent()
        if row["purpose"] == "delivery"
    ]
    assert len(deliveries) == 1
    notices = [
        row for row in service.outbox.list_recent() if row["purpose"] == "notice"
    ]
    assert len(notices) == 1


def test_missing_destination_waits_and_enqueues_question(service) -> None:
    result = service.ingest(
        raw_message(
            make_mail(
                text="请处理附件",
                attachments=(("明细.xlsx", b"xlsx"),),
            )
        )
    )
    task = service.tasks.require(result.task_id)
    assert task["status"] == state.WAITING_INPUT
    detail = service.task_detail(result.task_id)
    codes = {issue["code"] for issue in detail["issues"]}
    assert "DESTINATION_MISSING" in codes
    questions = [row for row in service.outbox.list_recent() if row["purpose"] == "question"]
    assert len(questions) == 1
    assert questions[0]["status"] == "dry_run"
    assert "DESTINATION_MISSING" in questions[0]["body"] or "目的国" in questions[0]["body"]


def test_unknown_destination_waits(service) -> None:
    result = service.ingest(
        raw_message(make_mail(text="目的国：火星", attachments=(("明细.xlsx", b"xlsx"),)))
    )
    task = service.tasks.require(result.task_id)
    assert task["status"] == state.WAITING_INPUT
    codes = {issue["code"] for issue in service.tasks.issues(result.task_id)}
    assert "DESTINATION_UNKNOWN" in codes


def test_core_waiting_and_unsupported_become_waiting_input(service, engine) -> None:
    engine.mode = "waiting"
    first = service.ingest(
        raw_message(make_mail(message_id="<wait@example.com>"), uid=1)
    )
    assert service.tasks.require(first.task_id)["status"] == state.WAITING_INPUT

    engine.mode = "unsupported"
    second = service.ingest(
        raw_message(make_mail(message_id="<unsupported@example.com>"), uid=2)
    )
    assert service.tasks.require(second.task_id)["status"] == state.WAITING_INPUT
    codes = {issue["code"] for issue in service.tasks.issues(second.task_id)}
    assert "UNSUPPORTED_BUSINESS" in codes


def test_core_unavailable_is_retryable(service, engine) -> None:
    engine.mode = "core_unavailable"
    result = service.ingest(raw_message(make_mail()))
    task = service.tasks.require(result.task_id)
    assert task["status"] == state.RETRY_PENDING
    assert "core not installed" in (task["last_error"] or "")


def test_xlsx_attachment_is_saved_as_task_input(service) -> None:
    result = service.ingest(
        raw_message(
            make_mail(attachments=(("business.xlsx", b"actual-xlsx-bytes"),))
        )
    )
    inputs = service.tasks.inputs(result.task_id, 1)
    assert len(inputs) == 1
    stored = Path(inputs[0]["path"])
    assert stored.is_file()
    assert stored.read_bytes() == b"actual-xlsx-bytes"
    assert inputs[0]["filename"] == "business.xlsx"


def test_unsupported_and_oversized_attachments_wait(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        mail=replace(
            config.mail,
            max_attachment_bytes=10,
            max_total_attachment_bytes=10,
        ),
    )
    service = AppService(
        config, engine=FakeEngine(), gateway=SimulatorMailGateway()
    )
    try:
        result = service.ingest(
            raw_message(
                make_mail(attachments=(("big.xlsx", b"x" * 100), ("photo.jpg", b"jpeg")))
            )
        )
        task = service.tasks.require(result.task_id)
        assert task["status"] == state.WAITING_INPUT
        codes = {issue["code"] for issue in service.tasks.issues(result.task_id)}
        assert {"ATTACHMENT_TOO_LARGE", "UNSUPPORTED_ATTACHMENT"} <= codes
    finally:
        service.close()


def _waiting_task(service, *, message_id: str, uid: int = 1) -> str:
    result = service.ingest(
        raw_message(
            make_mail(
                message_id=message_id,
                text="请处理附件",
                attachments=(("明细.xlsx", b"xlsx"),),
            ),
            uid=uid,
        )
    )
    assert service.tasks.require(result.task_id)["status"] == state.WAITING_INPUT
    return str(result.task_id)


def test_supplement_mapping_and_destination_apply_atomically(service) -> None:
    task_id = _waiting_task(service, message_id="<initial@example.com>")
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 配件发票",
        text=f"任务编号：{task_id}\n国家映射：高卢=FRA\n目的国：高卢\n",
        in_reply_to="<whatever@example.com>",
        message_id="<reply@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.supplement is True
    task = service.tasks.require(task_id)
    assert task["status"] == state.READY_TO_SEND
    assert task["destination_alpha3"] == "FRA"
    assert service.references.resolve("高卢")["alpha3"] == "FRA"


def test_supplement_conflict_writes_no_partial_alias(service) -> None:
    task_id = _waiting_task(service, message_id="<initial-conflict@example.com>")
    service.references.add_alias("测试国", "FRA", actor="test", reason="fixture")
    reply = make_mail(
        subject="Re: 配件发票",
        text=(
            f"任务编号：{task_id}\n"
            "国家映射：测试国=DEU\n"
            "国家映射：新高卢=FRA\n"
        ),
        message_id="<conflict@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.supplement is True
    assert service.references.resolve("新高卢") is None
    task = service.tasks.require(task_id)
    assert task["status"] == state.WAITING_INPUT
    codes = {issue["code"] for issue in service.tasks.issues(task_id)}
    assert "COUNTRY_MAPPING_CONFLICT" in codes


def test_supplement_from_non_owner_is_filtered(
    tmp_path: Path, service_factory
) -> None:
    config = make_config(
        tmp_path,
        test_addresses=("tester@example.com", "stranger@example.com"),
    )
    service = service_factory(config)
    task_id = _waiting_task(service, message_id="<owner@example.com>")
    reply = make_mail(
        sender="stranger@example.com",
        text=f"任务编号：{task_id}\n目的国：法国\n",
        message_id="<stranger-reply@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.filter_reason == "supplement_from_non_owner"
    assert service.tasks.require(task_id)["status"] == state.WAITING_INPUT


def test_ambiguous_supplement_reference_is_filtered(service) -> None:
    first = _waiting_task(service, message_id="<amb-one@example.com>", uid=1)
    second = _waiting_task(service, message_id="<amb-two@example.com>", uid=2)
    reply = make_mail(
        text=f"任务编号：{first}\n任务编号：{second}\n目的国：法国\n",
        message_id="<ambiguous@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=3))
    assert result.filter_reason == "ambiguous_task_reference"
    assert service.tasks.require(first)["status"] == state.WAITING_INPUT
    assert service.tasks.require(second)["status"] == state.WAITING_INPUT


def test_free_form_supplement_is_not_interpreted(service) -> None:
    task_id = _waiting_task(service, message_id="<free@example.com>")
    reply = make_mail(
        subject="Re: 配件发票",
        text=f"任务编号：{task_id}\n改成法国吧，谢谢\n",
        message_id="<free-reply@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.supplement is True
    codes = {issue["code"] for issue in service.tasks.issues(task_id)}
    assert "UNSTRUCTURED_SUPPLEMENT" in codes
    assert service.tasks.require(task_id)["status"] == state.WAITING_INPUT