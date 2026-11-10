"""阶段 4 逐票目的国确认服务测试（全合成数据）。

覆盖：启用/停用国家与别名、票号只取服务器已保存 preview、get 未物化拒绝、
跨批次/未物化/非 held/删除/归档任务拒绝、伪造 revision/plan/参考版本、
并发双连接、批量中途失败全回滚、部分选择、同值无操作与改值前后审计、
父邮件正文/其他票数据不泄露、无网络与凭据/.env 访问。测试只通过服务公共
接口确认，不借助 TaskRepository.update_destination 模拟“已确认”。
"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cater.app import state
from cater.app.batch_destinations import (
    DESTINATION_EVENT_CODE,
    BatchDestinationService,
)
from cater.app.db import Database
from cater.app.errors import ValidationError
from cater.app.reference import ReferenceService
from cater.app.tasks import TaskRepository

from batchkit import import_two_ticket


# ---------------------------------------------------------------------------
# 构造与查询辅助
# ---------------------------------------------------------------------------


def _dest(service) -> BatchDestinationService:
    """按 G 的接线方式组合既有服务（不改 application.py）。"""
    return BatchDestinationService(
        service.db, service.references, service.tasks, service.logger)


def _materialize(service, *, subject: str = "两票整机发票") -> dict:
    batch_id, result = import_two_ticket(service, subject=subject)
    plan = result["plan"]
    keys = sorted(c["candidate_id"] for c in result["preview"]["candidates"])
    outcome = service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="confirmer")
    tasks = sorted(outcome["tasks"], key=lambda t: t["candidate_key"])
    return {
        "batch_id": batch_id,
        "plan": plan,
        "keys": keys,
        "tasks": tasks,
        "outcome": outcome,
        "preview": result["preview"],
    }


def _task_by_invoice(service, materialized: dict, invoice: str) -> str:
    """经服务器保存的候选 preview 身份找到票（不看前端/邮件正文）。"""
    for candidate_key in materialized["keys"]:
        row = service.db.fetchone(
            "SELECT preview_json FROM batch_candidates "
            "WHERE plan_id = ? AND candidate_key = ?",
            (materialized["plan"]["id"], candidate_key))
        preview = json.loads(row["preview_json"])
        if invoice in preview.get("identity", {}).get("invoice_no", []):
            for task in materialized["tasks"]:
                if task["candidate_key"] == candidate_key:
                    return str(task["task_id"])
    raise AssertionError(f"找不到 {invoice} 票")


def _dest_events(service, task_id: str | None = None) -> list[dict]:
    if task_id is None:
        rows = service.db.fetchall(
            "SELECT * FROM task_events WHERE code = ? ORDER BY id",
            (DESTINATION_EVENT_CODE,))
    else:
        rows = service.db.fetchall(
            "SELECT * FROM task_events WHERE code = ? AND task_id = ? ORDER BY id",
            (DESTINATION_EVENT_CODE, task_id))
    return [dict(row) for row in rows]


def _batch_dest_events(service, batch_id: str) -> list[dict]:
    return [event for event in service.batches.events(batch_id)
            if event["action"] == DESTINATION_EVENT_CODE]


# ---------------------------------------------------------------------------
# get：只读已物化票、启用国家、服务器票号
# ---------------------------------------------------------------------------


def test_get_returns_enabled_countries_and_rows(service):
    materialized = _materialize(service)
    data = _dest(service).get_batch_destinations(materialized["batch_id"])

    batch = service.batches.require_batch(materialized["batch_id"])
    assert data["batch_id"] == materialized["batch_id"]
    assert data["plan_id"] == batch["confirmed_plan_id"]
    assert data["reference_version"] == service.references.reference_version()

    enabled = {country["alpha3"] for country in
               service.references.list_countries(enabled_only=True, limit=500)}
    assert {country["alpha3"] for country in data["countries"]} == enabled
    assert all(set(country) == {"name_zh", "alpha3"}
               for country in data["countries"])

    assert len(data["rows"]) == 2
    assert {row["task_id"] for row in data["rows"]} == {
        task["task_id"] for task in materialized["tasks"]}
    required = {
        "task_id", "candidate_key", "ticket_no", "destination_name",
        "destination_alpha3", "revision", "confirmed_by", "confirmed_at",
        "task_status", "execution_hold",
    }
    for row in data["rows"]:
        assert required <= set(row)
        assert row["revision"] == 0
        assert row["confirmed_by"] is None
        assert row["confirmed_at"] is None
        assert row["task_status"] == state.RECEIVED
        assert row["execution_hold"] == 1
        assert row["destination_name"] is None
        assert row["destination_alpha3"] is None
        assert row["ticket_no"] in {"INV-A-001", "INV-B-001"}
        # 不泄露父邮件正文/owner 等无关数据
        assert not {"owner", "owner_name", "mail_subject", "body",
                    "html_body", "text_body"} & set(row)


def test_get_rejects_unmaterialized_and_unknown_batch(service):
    batch_id, _ = import_two_ticket(service)
    with pytest.raises(ValidationError, match="未物化"):
        _dest(service).get_batch_destinations(batch_id)
    with pytest.raises(KeyError):
        _dest(service).get_batch_destinations("batch_unknown")


def test_get_ticket_no_comes_from_saved_preview_json(service):
    materialized = _materialize(service)
    dest = _dest(service)
    candidate_key = materialized["keys"][0]
    service.db.execute(
        "UPDATE batch_candidates SET preview_json = ? "
        "WHERE plan_id = ? AND candidate_key = ?",
        (json.dumps({"identity": {"invoice_no": ["SERVER-SAVED-999"]}}),
         materialized["plan"]["id"], candidate_key))
    data = dest.get_batch_destinations(materialized["batch_id"])
    row = next(item for item in data["rows"]
               if item["candidate_key"] == candidate_key)
    assert row["ticket_no"] == "SERVER-SAVED-999"

    service.db.execute(
        "UPDATE batch_candidates SET preview_json = 'not-json' "
        "WHERE plan_id = ? AND candidate_key = ?",
        (materialized["plan"]["id"], candidate_key))
    data = dest.get_batch_destinations(materialized["batch_id"])
    row = next(item for item in data["rows"]
               if item["candidate_key"] == candidate_key)
    assert row["ticket_no"] == candidate_key


# ---------------------------------------------------------------------------
# confirm：首次确认写当前值与真实修订、审计
# ---------------------------------------------------------------------------


def test_first_confirmation_writes_destination_and_audit(service):
    materialized = _materialize(service)
    dest = _dest(service)
    data = dest.get_batch_destinations(materialized["batch_id"])
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    before_task = service.tasks.require(task_id)
    before_version = service.tasks.get_version(task_id, before_task["version"])
    snapshot_path = Path(next(
        task["snapshot_path"] for task in materialized["tasks"]
        if task["task_id"] == task_id))
    snapshot_bytes = snapshot_path.read_bytes()

    outcome = dest.confirm_destinations(
        materialized["batch_id"],
        plan_id=data["plan_id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops-user",
        reason="客户邮件明确要求发往法国",
        expected_reference_version=data["reference_version"],
    )

    assert outcome["changed_task_ids"] == [task_id]
    assert outcome["unchanged_task_ids"] == []
    change = outcome["changes"][0]
    assert change["changed"] is True
    assert change["revision"] > 0
    assert change["previous_destination_alpha3"] is None
    assert change["destination_alpha3"] == "FRA"

    task = service.tasks.require(task_id)
    assert task["destination_name"] == "法国"
    assert task["destination_alpha3"] == "FRA"
    assert task["status"] == before_task["status"] == state.RECEIVED
    assert task["version"] == before_task["version"]
    assert task["retry_count"] == before_task["retry_count"]
    assert service.tasks.is_execution_held(task_id) is True
    assert service.tasks.batch_hold(task_id)["execution_hold"] == 1
    assert service.db.scalar("SELECT COUNT(*) FROM outbox") == 0
    assert snapshot_path.read_bytes() == snapshot_bytes

    version = service.tasks.get_version(task_id, task["version"])
    assert version["destination_name"] == "法国"
    assert version["destination_alpha3"] == "FRA"
    assert before_version["destination_alpha3"] is None

    events = _dest_events(service, task_id)
    assert len(events) == 1
    assert events[0]["id"] == change["revision"]
    assert events[0]["actor"] == "ops-user"
    detail = json.loads(events[0]["detail_json"])
    assert detail["previous_destination_alpha3"] is None
    assert detail["destination_alpha3"] == "FRA"
    assert detail["reason"] == "客户邮件明确要求发往法国"
    assert detail["reference_version"] == data["reference_version"]
    assert detail["selection_scope"] == [task_id]

    batch_events = _batch_dest_events(service, materialized["batch_id"])
    assert len(batch_events) == 1
    assert batch_events[0]["actor"] == "ops-user"
    batch_detail = batch_events[0]["detail"]
    assert batch_detail["reason"] == "客户邮件明确要求发往法国"
    assert batch_detail["reference_version"] == data["reference_version"]
    assert batch_detail["selected_task_ids"] == [task_id]
    assert batch_detail["changed_task_ids"] == [task_id]
    assert batch_detail["updates"][0]["revision"] == change["revision"]

    refreshed = dest.get_batch_destinations(materialized["batch_id"])
    row = next(item for item in refreshed["rows"]
               if item["task_id"] == task_id)
    assert row["destination_alpha3"] == "FRA"
    assert row["destination_name"] == "法国"
    assert row["revision"] == change["revision"]
    assert row["confirmed_by"] == "ops-user"
    assert row["confirmed_at"] == events[0]["created_at"]


def test_same_value_is_noop_without_new_audit(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    first = dest.confirm_destinations(
        materialized["batch_id"],
        plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops-1", reason="首次确认",
    )
    revision = first["changes"][0]["revision"]
    task_before = service.tasks.require(task_id)
    event_count = len(_dest_events(service, task_id))
    batch_event_count = len(_batch_dest_events(
        service, materialized["batch_id"]))

    second = dest.confirm_destinations(
        materialized["batch_id"],
        plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": revision}],
        actor="ops-2", reason="重复确认同值",
    )
    assert second["changed_task_ids"] == []
    assert second["unchanged_task_ids"] == [task_id]
    assert second["changes"][0]["changed"] is False
    assert second["changes"][0]["revision"] == revision
    task_after = service.tasks.require(task_id)
    assert task_after == task_before  # 同值无操作：连 updated_at 都不变
    assert len(_dest_events(service, task_id)) == event_count
    assert len(_batch_dest_events(
        service, materialized["batch_id"])) == batch_event_count


def test_changed_value_adds_event_with_before_and_after(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    first = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops-1", reason="先按法国确认")
    first_revision = first["changes"][0]["revision"]

    second = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "DEU",
                  "expected_revision": first_revision}],
        actor="ops-2", reason="客户改发德国",
    )
    change = second["changes"][0]
    assert change["changed"] is True
    assert change["revision"] > first_revision
    assert change["previous_destination_alpha3"] == "FRA"
    assert change["destination_alpha3"] == "DEU"
    detail = json.loads(_dest_events(service, task_id)[-1]["detail_json"])
    assert detail["previous_destination_name"] == "法国"
    assert detail["destination_name"] == "德国"
    assert detail["reason"] == "客户改发德国"
    assert service.tasks.require(task_id)["destination_alpha3"] == "DEU"
    assert service.tasks.get_version(
        task_id, service.tasks.require(task_id)["version"]
    )["destination_alpha3"] == "DEU"
    row = next(item for item in second["rows"] if item["task_id"] == task_id)
    assert row["revision"] == change["revision"]
    assert row["confirmed_by"] == "ops-2"


def test_partial_selection_leaves_other_ticket_untouched(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_a = _task_by_invoice(service, materialized, "INV-A-001")
    task_b = _task_by_invoice(service, materialized, "INV-B-001")
    before_b = service.tasks.require(task_b)
    version_b_before = service.tasks.get_version(task_b, before_b["version"])
    snapshot_b = Path(next(
        task["snapshot_path"] for task in materialized["tasks"]
        if task["task_id"] == task_b)).read_bytes()

    outcome = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_a, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops", reason="只确认 A 票",
    )
    assert outcome["changed_task_ids"] == [task_a]
    after_b = service.tasks.require(task_b)
    assert after_b == before_b
    assert service.tasks.get_version(task_b, before_b["version"]) == version_b_before
    assert after_b["destination_alpha3"] is None
    assert _dest_events(service, task_b) == []
    assert Path(next(
        task["snapshot_path"] for task in materialized["tasks"]
        if task["task_id"] == task_b)).read_bytes() == snapshot_b


# ---------------------------------------------------------------------------
# 国家解析：启用/停用/别名
# ---------------------------------------------------------------------------


def test_country_code_case_and_alias_resolve_to_enabled_country(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    service.references.add_alias(
        "合成法兰西", "FRA", actor="reference-admin", reason="测试启用别名")

    outcome = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "合成法兰西",
                  "expected_revision": 0}],
        actor="ops", reason="别名确认法国",
    )
    assert outcome["changes"][0]["destination_alpha3"] == "FRA"
    assert outcome["changes"][0]["destination_name"] == "法国"
    assert outcome["changes"][0]["requested_country"] == "合成法兰西"
    detail = json.loads(_dest_events(service, task_id)[0]["detail_json"])
    assert detail["requested_country"] == "合成法兰西"

    # 内置别名 Holland 与大小写形式的代码都能解析
    task_b = _task_by_invoice(service, materialized, "INV-B-001")
    outcome = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_b, "country": "holland",
                  "expected_revision": 0}],
        actor="ops", reason="内置别名确认荷兰",
    )
    assert outcome["changes"][0]["destination_alpha3"] == "NLD"

    current_revision = _dest_events(service, task_id)[0]["id"]
    again = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "fra",
                  "expected_revision": current_revision}],
        actor="ops", reason="大小写同值",
    )
    assert again["changes"][0]["changed"] is False
    assert again["changes"][0]["destination_alpha3"] == "FRA"
    assert service.tasks.require(task_id)["destination_alpha3"] == "FRA"


def test_disabled_country_is_rejected_and_hidden(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    service.references.set_country_enabled(
        "FRA", False, actor="reference-admin", reason="暂停法国")
    data = dest.get_batch_destinations(materialized["batch_id"])
    assert "FRA" not in {country["alpha3"] for country in data["countries"]}

    with pytest.raises(ValidationError, match="不可用|停用"):
        dest.confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason="停用国家应拒绝")
    assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service, task_id) == []


def test_disabled_alias_is_rejected_but_alpha3_still_works(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    service.references.set_alias_enabled(
        "holland", False, actor="reference-admin", reason="停用别名")
    with pytest.raises(ValidationError, match="不可用|停用"):
        dest.confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "Holland",
                      "expected_revision": 0}],
            actor="ops", reason="停用别名应拒绝")
    assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service, task_id) == []

    # 直接 alpha3 仍按启用国家解析，不受其他别名停用影响
    outcome = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "NLD",
                  "expected_revision": 0}],
        actor="ops", reason="按国家代码确认")
    assert outcome["changes"][0]["destination_alpha3"] == "NLD"


@pytest.mark.parametrize("country", ["ATLANTIS", "  ", "法国-伪造"])
def test_unknown_country_rejected_zero_write(service, country):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    with pytest.raises(ValidationError):
        _dest(service).confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": country,
                      "expected_revision": 0}],
            actor="ops", reason="非法国家")
    assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service, task_id) == []


# ---------------------------------------------------------------------------
# 归属/物化/hold/生命周期/状态拒绝
# ---------------------------------------------------------------------------


def test_unmaterialized_batch_confirm_rejected(service):
    batch_id, result = import_two_ticket(service)
    with pytest.raises(ValidationError, match="未物化"):
        _dest(service).confirm_destinations(
            batch_id, plan_id=result["plan"]["id"],
            updates=[{"task_id": "task_fake", "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason="未物化应拒绝")


def test_cross_batch_task_rejected_zero_write(service):
    first = _materialize(service, subject="第一批")
    second = _materialize(service, subject="第二批")
    task_other = _task_by_invoice(service, second, "INV-A-001")
    task_own = _task_by_invoice(service, first, "INV-A-001")
    with pytest.raises(ValidationError, match="不属于"):
        _dest(service).confirm_destinations(
            first["batch_id"], plan_id=first["plan"]["id"],
            updates=[
                {"task_id": task_own, "country": "FRA", "expected_revision": 0},
                {"task_id": task_other, "country": "DEU", "expected_revision": 0},
            ],
            actor="ops", reason="跨批次应拒绝")
    for task_id in (task_own, task_other):
        assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service) == []


def test_forged_plan_id_rejected_zero_write(service):
    first = _materialize(service, subject="第一批")
    second = _materialize(service, subject="第二批")
    task_id = _task_by_invoice(service, first, "INV-A-001")
    for forged in (second["plan"]["id"], "plan_forged", ""):
        with pytest.raises(ValidationError):
            _dest(service).confirm_destinations(
                first["batch_id"], plan_id=forged,
                updates=[{"task_id": task_id, "country": "FRA",
                          "expected_revision": 0}],
                actor="ops", reason="伪造方案")
    assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service) == []


def test_non_held_task_rejected(service):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    service.db.insert(
        "UPDATE batch_tasks SET execution_hold = 0 WHERE task_id = ?",
        (task_id,))
    with pytest.raises(ValidationError, match="挂起"):
        _dest(service).confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason="非挂起应拒绝")
    assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service, task_id) == []


@pytest.mark.parametrize("column", ["deleted_at", "archived_at"])
def test_deleted_or_archived_task_rejected(service, column):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    service.db.insert(
        f"UPDATE tasks SET {column} = '2026-01-01T00:00:00Z' WHERE id = ?",
        (task_id,))
    with pytest.raises(ValidationError, match="删除|归档"):
        _dest(service).confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason="已删除/归档应拒绝")
    assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service, task_id) == []


def test_non_received_task_rejected(service):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    service.db.insert(
        "UPDATE tasks SET status = ? WHERE id = ?", (state.CANCELLED, task_id))
    with pytest.raises(ValidationError, match="RECEIVED|状态"):
        _dest(service).confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason="非 RECEIVED 应拒绝")
    assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service, task_id) == []


def test_only_current_task_version_is_updated(service):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    updated = service.tasks.start_next_version(
        task_id, actor="tester", reason="测试重新收集")
    assert updated["version"] == 2
    outcome = _dest(service).confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops", reason="只写当前版本")
    assert outcome["changes"][0]["changed"] is True
    old = service.tasks.get_version(task_id, 1)
    current = service.tasks.get_version(task_id, 2)
    assert old["destination_alpha3"] is None
    assert current["destination_alpha3"] == "FRA"


# ---------------------------------------------------------------------------
# 修订/参考版本/输入校验
# ---------------------------------------------------------------------------


def test_stale_revision_rejected_without_overwrite(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    first = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops-1", reason="首次")
    with pytest.raises(ValidationError, match="修订已过期"):
        dest.confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "DEU",
                      "expected_revision": 0}],
            actor="ops-2", reason="伪造旧修订")
    task = service.tasks.require(task_id)
    assert task["destination_alpha3"] == "FRA"
    assert len(_dest_events(service, task_id)) == 1
    assert _dest_events(service, task_id)[0]["id"] == \
        first["changes"][0]["revision"]


def test_reference_version_rechecked_inside_transaction(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    data = dest.get_batch_destinations(materialized["batch_id"])
    # 表单渲染后参考数据发生变化（另一处显式修改产生新 config_versions）
    service.references.set_country_enabled(
        "DEU", False, actor="reference-admin", reason="版本变化")
    with pytest.raises(ValidationError, match="参考数据版本"):
        dest.confirm_destinations(
            materialized["batch_id"], plan_id=data["plan_id"],
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason="过期参考版本",
            expected_reference_version=data["reference_version"])
    assert service.tasks.require(task_id)["destination_alpha3"] is None
    assert _dest_events(service, task_id) == []


def test_optional_reference_version_records_current_version(service):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    current = service.references.reference_version()
    outcome = _dest(service).confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops", reason="未提供参考版本",
        expected_reference_version=None)
    assert outcome["reference_version"] == service.references.reference_version()
    event = _dest_events(service, task_id)[0]
    assert json.loads(event["detail_json"])["reference_version"] == current


@pytest.mark.parametrize("bad_reference", [True, False, -1, "3", 1.5])
def test_invalid_reference_version_type_rejected(service, bad_reference):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    with pytest.raises(ValidationError, match="expected_reference_version"):
        _dest(service).confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason="非法参考版本",
            expected_reference_version=bad_reference)
    assert _dest_events(service, task_id) == []


def test_invalid_updates_rejected_with_zero_write(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_a = _task_by_invoice(service, materialized, "INV-A-001")
    task_b = _task_by_invoice(service, materialized, "INV-B-001")
    plan_id = materialized["plan"]["id"]
    bad_batches = [
        [],
        [{"task_id": task_a, "country": "FRA"}],
        [{"task_id": task_a, "country": "FRA", "expected_revision": 0,
          "unknown": True}],
        [{"task_id": task_a, "country": "FRA", "expected_revision": 0},
         {"task_id": task_a, "country": "DEU", "expected_revision": 0}],
        [{"task_id": "", "country": "FRA", "expected_revision": 0}],
        [{"task_id": task_a, "country": "", "expected_revision": 0}],
        [{"task_id": task_a, "country": "FRA", "expected_revision": True}],
        [{"task_id": task_a, "country": "FRA", "expected_revision": -1}],
        [{"task_id": task_a, "country": "FRA", "expected_revision": "0"}],
        [{"task_id": task_a, "country": "FRA", "expected_revision": None}],
        # 合法首项 + 非法次项：整体零写入
        [{"task_id": task_a, "country": "FRA", "expected_revision": 0},
         {"task_id": task_b, "country": "DEU", "expected_revision": True}],
    ]
    for updates in bad_batches:
        with pytest.raises(ValidationError):
            dest.confirm_destinations(
                materialized["batch_id"], plan_id=plan_id,
                updates=updates, actor="ops", reason="非法输入")
    assert service.tasks.require(task_a)["destination_alpha3"] is None
    assert service.tasks.require(task_b)["destination_alpha3"] is None
    assert _dest_events(service) == []
    assert _batch_dest_events(service, materialized["batch_id"]) == []


@pytest.mark.parametrize("reason", ["", "   ", None, 123])
def test_empty_or_invalid_reason_rejected(service, reason):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    with pytest.raises(ValidationError, match="reason"):
        _dest(service).confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason=reason)
    assert _dest_events(service, task_id) == []


def test_empty_actor_or_plan_rejected(service):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    dest = _dest(service)
    with pytest.raises(ValidationError, match="actor"):
        dest.confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="  ", reason="合法理由")
    with pytest.raises(ValidationError, match="plan_id"):
        dest.confirm_destinations(
            materialized["batch_id"], plan_id="",
            updates=[{"task_id": task_id, "country": "FRA",
                      "expected_revision": 0}],
            actor="ops", reason="合法理由")
    assert _dest_events(service, task_id) == []


# ---------------------------------------------------------------------------
# 并发双连接、事务中途失败
# ---------------------------------------------------------------------------


def test_concurrent_two_connections_only_one_wins(service):
    materialized = _materialize(service)
    plan_id = materialized["plan"]["id"]
    task_id = _task_by_invoice(service, materialized, "INV-A-001")
    reference_version = service.references.reference_version()
    other_db = Database(service.db.path)
    try:
        other = BatchDestinationService(
            other_db,
            ReferenceService(other_db, service.logger),
            TaskRepository(other_db, service.logger),
            service.logger,
        )
        barrier = threading.Barrier(2)

        def attempt(dest, country, actor):
            barrier.wait(timeout=10)
            try:
                dest.confirm_destinations(
                    materialized["batch_id"], plan_id=plan_id,
                    updates=[{"task_id": task_id, "country": country,
                              "expected_revision": 0}],
                    actor=actor, reason="并发同一修订",
                    expected_reference_version=reference_version)
                return "ok"
            except ValidationError as exc:
                assert "修订已过期" in str(exc)
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(attempt, _dest(service), "FRA", "ops-1"),
                pool.submit(attempt, other, "DEU", "ops-2"),
            ]
            outcomes = [future.result(timeout=30) for future in futures]
    finally:
        other_db.close()

    assert sorted(outcomes) == ["ok", "stale"]
    task = service.tasks.require(task_id)
    assert task["destination_alpha3"] in {"FRA", "DEU"}
    events = _dest_events(service, task_id)
    assert len(events) == 1
    assert events[0]["id"] == service.db.scalar(
        "SELECT MAX(id) FROM task_events WHERE task_id = ?", (task_id,))


def test_mid_batch_failure_rolls_back_every_ticket(service):
    materialized = _materialize(service)
    task_a = _task_by_invoice(service, materialized, "INV-A-001")
    task_b = _task_by_invoice(service, materialized, "INV-B-001")
    # 第二票写审计事件时强制失败：第一票已写入的内容也必须回滚。
    service.db.execute(
        "CREATE TRIGGER fail_batch_destination_event "
        "BEFORE INSERT ON task_events "
        f"WHEN NEW.code = '{DESTINATION_EVENT_CODE}' "
        f"AND NEW.task_id = '{task_b}' "
        "BEGIN SELECT RAISE(ABORT, '模拟审计写入失败'); END")
    with pytest.raises(sqlite3.IntegrityError):
        _dest(service).confirm_destinations(
            materialized["batch_id"], plan_id=materialized["plan"]["id"],
            updates=[
                {"task_id": task_a, "country": "FRA", "expected_revision": 0},
                {"task_id": task_b, "country": "DEU", "expected_revision": 0},
            ],
            actor="ops", reason="中途失败")
    for task_id in (task_a, task_b):
        assert service.tasks.require(task_id)["destination_alpha3"] is None
        assert service.tasks.get_version(
            task_id, service.tasks.require(task_id)["version"]
        )["destination_alpha3"] is None
    assert _dest_events(service) == []
    assert _batch_dest_events(service, materialized["batch_id"]) == []


# ---------------------------------------------------------------------------
# 数据边界：不泄露父邮件正文/别票数据，无网络
# ---------------------------------------------------------------------------


def test_audit_and_get_do_not_leak_parent_mail_or_other_ticket(service):
    materialized = _materialize(service)
    dest = _dest(service)
    task_a = _task_by_invoice(service, materialized, "INV-A-001")
    task_b = _task_by_invoice(service, materialized, "INV-B-001")
    outcome = dest.confirm_destinations(
        materialized["batch_id"], plan_id=materialized["plan"]["id"],
        updates=[{"task_id": task_a, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops", reason="只确认 A")
    payloads = [json.loads(event["detail_json"])
                for event in _dest_events(service, task_a)]
    payloads.extend(event["detail"] for event in
                    _batch_dest_events(service, materialized["batch_id"]))
    serialized = json.dumps(payloads, ensure_ascii=False)
    assert task_b not in serialized
    assert "两票整机发票" not in serialized
    assert "见附件发票与箱单" not in serialized
    for payload in payloads:
        for forbidden in ("body", "html", "text", "owner", "owner_name",
                          "mail_subject", "origin_mail_id"):
            assert forbidden not in payload
    get_payload = json.dumps(
        dest.get_batch_destinations(materialized["batch_id"]),
        ensure_ascii=False)
    for forbidden in ("body", "owner", "mail_subject", "origin_mail_id"):
        assert forbidden not in get_payload


def test_no_network_access(service, monkeypatch):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")

    def forbidden(*args, **kwargs):
        raise AssertionError("目的国确认不得访问网络")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    dest = _dest(service)
    data = dest.get_batch_destinations(materialized["batch_id"])
    outcome = dest.confirm_destinations(
        materialized["batch_id"], plan_id=data["plan_id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops", reason="纯离线确认",
        expected_reference_version=data["reference_version"])
    assert outcome["changes"][0]["destination_alpha3"] == "FRA"


def test_no_credential_or_env_access(service, monkeypatch):
    materialized = _materialize(service)
    task_id = _task_by_invoice(service, materialized, "INV-A-001")

    def forbidden(*args, **kwargs):
        raise AssertionError("目的国确认不得读取凭据/.env")

    from cater.app import config as config_mod
    monkeypatch.setattr(config_mod, "_mail_environment", forbidden)
    monkeypatch.setattr(
        config_mod.CredentialProvider, "imap_password", forbidden)
    monkeypatch.setattr(
        config_mod.CredentialProvider, "smtp_password", forbidden)
    dest = _dest(service)
    data = dest.get_batch_destinations(materialized["batch_id"])
    outcome = dest.confirm_destinations(
        materialized["batch_id"], plan_id=data["plan_id"],
        updates=[{"task_id": task_id, "country": "FRA",
                  "expected_revision": 0}],
        actor="ops", reason="不触碰凭据",
        expected_reference_version=data["reference_version"])
    assert outcome["changes"][0]["destination_alpha3"] == "FRA"
