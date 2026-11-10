"""多票批次服务层测试（四）：执行挂起的全入口强制。

批次子任务本阶段保持挂起：poller/recover/retry/process/reprocess/deliver/
补件/回收站等任何入口都不能绕过；假 engine 被触碰即测试失败。
"""
from __future__ import annotations

import pytest

from cater.app import state
from cater.app.errors import StateTransitionError, ValidationError
from cater.app.intake import GUIDANCE_BATCH_HELD

from batchkit import import_two_ticket
from helpers import make_mail, raw_message


def _materialized_tasks(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    outcome = service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="confirmer")
    return batch_id, [t["task_id"] for t in outcome["tasks"]]


def test_held_task_blocked_at_all_execution_entries(service, engine, gateway):
    _, task_ids = _materialized_tasks(service)
    assert len(task_ids) == 2
    for task_id in task_ids:
        with pytest.raises(StateTransitionError, match="尚未开放执行"):
            service.process_task(task_id)
        with pytest.raises(StateTransitionError, match="尚未开放执行"):
            service.retry_task(task_id)
        with pytest.raises(StateTransitionError, match="尚未开放执行"):
            service.reprocess_task(task_id)
        with pytest.raises(StateTransitionError, match="尚未开放执行"):
            service.confirm_reprocess(task_id)
        with pytest.raises(StateTransitionError, match="尚未开放执行"):
            service.deliver_task(task_id)
    # 假 engine 与网关从未被触碰
    assert engine.calls == []
    assert gateway.sent == []
    for task_id in task_ids:
        assert service.tasks.require(task_id)["status"] == state.RECEIVED
    assert service.db.fetchall("SELECT id FROM outbox") == []


def test_held_task_excluded_from_retry_queue(service, engine):
    _, task_ids = _materialized_tasks(service)
    # 强制进入 RETRY_PENDING：自动重试队列仍必须跳过挂起任务
    for task_id in task_ids:
        service.db.insert(
            "UPDATE tasks SET status = ?, next_retry_at = NULL WHERE id = ?",
            (state.RETRY_PENDING, task_id))
    assert service.tasks.due_for_retry() == []
    assert service.retry_pending_tasks() == []
    assert engine.calls == []


def test_held_task_survives_startup_recovery(service_factory, config):
    first = service_factory(config)
    _, task_ids = _materialized_tasks(first)
    first.close()
    # 重启：启动恢复不能把挂起任务推进 RETRY_PENDING
    second = service_factory(config)
    for task_id in task_ids:
        assert second.tasks.require(task_id)["status"] == state.RECEIVED
    assert second.tasks.due_for_retry() == []


def test_supplement_to_held_task_filtered_with_guidance(service, engine):
    batch_id, task_ids = _materialized_tasks(service)
    task_id = task_ids[0]
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 两票整机发票",
        text=f"任务编号：{task_id}\n目的国：法国\n",
        in_reply_to="<whatever@example.com>",
        message_id="<held-supplement@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.filtered is True
    assert result.filter_reason == "batch_task_held"
    assert result.guidance == GUIDANCE_BATCH_HELD
    assert engine.calls == []
    assert service.tasks.require(task_id)["status"] == state.RECEIVED


def test_retry_command_to_held_task_filtered(service, engine):
    _, task_ids = _materialized_tasks(service)
    task_id = task_ids[0]
    reply = make_mail(
        subject=f"Re: [CATER {task_id} v1] 两票整机发票",
        text=f"任务编号：{task_id}\n重新处理\n",
        message_id="<held-retry@example.com>",
    )
    result = service.ingest(raw_message(reply, uid=2))
    assert result.filtered is True
    assert result.filter_reason == "batch_task_held"
    assert engine.calls == []
    assert service.tasks.require(task_id)["status"] == state.RECEIVED


def test_held_task_cannot_be_trashed_or_batch_deleted(service):
    from cater.app.errors import BatchDisallowedError
    _, task_ids = _materialized_tasks(service)
    for task_id in task_ids:
        with pytest.raises(ValidationError, match="批次"):
            service.tasks.delete_to_trash(task_id, actor="admin")
    # 批量删除整体拒绝（原子：全撤销），任务保持未删除
    with pytest.raises(BatchDisallowedError):
        service.tasks.batch_delete(task_ids, actor="admin")
    for task_id in task_ids:
        assert service.tasks.is_trashed(task_id) is False


def test_legacy_single_ticket_flow_unaffected(service, engine):
    """既有单票链路不受影响：正常任务不挂起、可处理。"""
    raw = make_mail(
        subject="配件发票",
        text="目的国：法国",
        attachments=(("parts.xlsx", b"fake-xlsx-input"),),
        message_id="<legacy@example.com>",
    )
    result = service.ingest(raw_message(raw, uid=1))
    assert result.created is True
    assert service.tasks.is_execution_held(result.task_id) is False
    service.process_task(result.task_id)
    assert engine.calls  # 正常任务正常进入引擎
