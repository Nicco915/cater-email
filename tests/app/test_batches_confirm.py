"""多票批次服务层测试（二）：确认边界与幂等建票。

核心承诺：双击/并发/重启后相同请求返回同一组任务；不同请求显式冲突。
"""
from __future__ import annotations

import pytest

from cater.app.batches import HOLD_REASON
from cater.app.errors import ValidationError

from batchkit import import_two_ticket


def _confirm(service, batch_id, plan, keys, exclusions=None, actor="confirmer"):
    return service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions=exclusions or {}, actor=actor)


def _keys(preview):
    return [c["candidate_id"] for c in preview["candidates"]]


def test_confirm_creates_held_tasks_in_one_operation(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])
    assert len(keys) == 2

    outcome = _confirm(service, batch_id, plan, keys)
    assert outcome["idempotent"] is False
    tasks = outcome["tasks"]
    assert len(tasks) == 2
    assert {t["candidate_key"] for t in tasks} == set(keys)
    for item in tasks:
        assert item["execution_hold"] == 1
        assert item["hold_reason"] == HOLD_REASON
        assert item["task_status"] == "RECEIVED"
        task = service.tasks.get(item["task_id"])
        assert task["origin_mail_id"] == service.batches.require_batch(
            batch_id)["mail_id"]
    batch = outcome["batch"]
    assert batch["status"] == "materialized"
    assert batch["confirmed_plan_id"] == plan["id"]
    assert batch["confirmed_fingerprint"] == plan["source_fingerprint"]
    assert batch["confirmed_by"] == "confirmer"
    # 每个候选的决定都落库（审计）
    for cand in service.batches.candidates(plan["id"]):
        assert cand["decision"] == "selected"
        assert cand["decided_by"] == "confirmer"
    actions = [e["action"] for e in service.batches.events(batch_id)]
    assert "batch_confirmed" in actions
    assert "batch_materialized" in actions


def test_confirm_double_click_returns_same_task_set(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])
    first = _confirm(service, batch_id, plan, keys)
    second = _confirm(service, batch_id, plan, keys)
    assert second["idempotent"] is True
    assert ({t["task_id"] for t in second["tasks"]}
            == {t["task_id"] for t in first["tasks"]})
    # 不新增任务
    assert len(service.batches.batch_tasks(batch_id)) == 2
    # 并发迟到（事务内状态复核）路径同样幂等
    third = _confirm(service, batch_id, plan, keys)
    assert third["idempotent"] is True


def test_confirm_different_selection_or_plan_conflicts(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])
    _confirm(service, batch_id, plan, keys)
    # 同方案但选票组合不同 -> 显式冲突，不静默加票
    with pytest.raises(ValidationError):
        _confirm(service, batch_id, plan, keys[:1],
                 exclusions={keys[1]: "换个组合"})
    # 同选票但指纹不同 -> 显式冲突
    with pytest.raises(ValidationError):
        service.batches.confirm_and_create(
            batch_id, plan_id=plan["id"], fingerprint="0" * 64,
            selected=keys, exclusions={}, actor="confirmer")


def test_confirm_with_excluded_candidate_creates_subset(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    preview = result["preview"]
    cand_a = next(c for c in preview["candidates"]
                  if "INV-A-001" in c["identity"].get("invoice_no", []))
    cand_b = next(c for c in preview["candidates"]
                  if "INV-B-001" in c["identity"].get("invoice_no", []))
    outcome = _confirm(service, batch_id, plan, [cand_b["candidate_id"]],
                       exclusions={cand_a["candidate_id"]: "A 票改走单独邮件"})
    tasks = outcome["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["candidate_key"] == cand_b["candidate_id"]
    decisions = {c["candidate_key"]: c
                 for c in service.batches.candidates(plan["id"])}
    assert decisions[cand_a["candidate_id"]]["decision"] == "excluded"
    assert (decisions[cand_a["candidate_id"]]["decision_reason"]
            == "A 票改走单独邮件")
    assert decisions[cand_b["candidate_id"]]["decision"] == "selected"


# ---------------------------------------------------------------------------
# 确认拒绝边界
# ---------------------------------------------------------------------------


def test_confirm_rejects_stale_plan_and_fingerprint(service):
    batch_id, result = import_two_ticket(
        service, extra_rows=[("MSOXX", "S009999", 3000)])
    plan_v1 = result["plan"]
    row = result["preview"]["unmatched_mail_rows"][0]
    detail = service.batches.adjust_plan(
        batch_id, base_plan_id=plan_v1["id"],
        ops=[{"type": "mail_row", "table_index": row["table_index"],
              "row": row["row"], "action": "exclude", "reason": "非本批"}],
        actor="a")
    plan_v2 = detail["plan"]
    keys = _keys(detail["preview"])
    # 过期方案版本拒绝
    with pytest.raises(ValidationError):
        _confirm(service, batch_id, plan_v1, keys)
    # 指纹不匹配拒绝（伪造/过期页面）
    with pytest.raises(ValidationError):
        service.batches.confirm_and_create(
            batch_id, plan_id=plan_v2["id"], fingerprint="f" * 64,
            selected=keys, exclusions={}, actor="a")
    # 正确版本+指纹成功
    outcome = _confirm(service, batch_id, plan_v2, keys)
    assert outcome["idempotent"] is False


def test_confirm_rejects_unresolved_unmatched_row(service):
    batch_id, result = import_two_ticket(
        service, extra_rows=[("MSOXX", "S009999", 3000)])
    plan = result["plan"]
    keys = _keys(result["preview"])
    with pytest.raises(ValidationError, match="未归属的正文业务行"):
        _confirm(service, batch_id, plan, keys)
    # 仍未建任何任务
    assert service.batches.batch_tasks(batch_id) == []
    assert service.batches.require_batch(batch_id)["status"] == "open"


def test_confirm_rejects_unreadable_excel_not_default_ignored(service):
    batch_id, result = import_two_ticket(service, broken_attachment=True)
    plan = result["plan"]
    keys = _keys(result["preview"])
    with pytest.raises(ValidationError):
        _confirm(service, batch_id, plan, keys)
    assert service.batches.batch_tasks(batch_id) == []


def test_confirm_selection_shape_rejections(service):
    """选票形状校验：未知候选/空选择/未覆盖/重叠/无理由排除全部拒绝。"""
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    preview = result["preview"]
    assert all(not c["needs_confirmation"] for c in preview["candidates"])
    keys = _keys(preview)
    # 伪造选择不存在的候选
    with pytest.raises(ValidationError, match="不存在于当前方案"):
        _confirm(service, batch_id, plan, keys + ["cand-fake"])
    # 排除不存在的候选
    with pytest.raises(ValidationError, match="不存在于当前方案"):
        _confirm(service, batch_id, plan, keys,
                 exclusions={"cand-fake": "伪造"})
    # 空选择拒绝
    with pytest.raises(ValidationError, match="不能为空"):
        _confirm(service, batch_id, plan, [],
                 exclusions={k: "都排除" for k in keys})
    # 未覆盖候选（既未选入也未排除）拒绝：不能悄悄丢票
    with pytest.raises(ValidationError, match="既未选入也未排除"):
        _confirm(service, batch_id, plan, keys[:1])
    # 选入+排除重叠拒绝
    with pytest.raises(ValidationError, match="同时被选入和排除"):
        _confirm(service, batch_id, plan, keys,
                 exclusions={keys[0]: "重叠"})
    # 排除无理由拒绝
    with pytest.raises(ValidationError, match="排除候选"):
        _confirm(service, batch_id, plan, keys[:1],
                 exclusions={keys[1]: "   "})
    # 全部拒绝后批次仍然开放、零任务
    assert service.batches.require_batch(batch_id)["status"] == "open"
    assert service.batches.batch_tasks(batch_id) == []


def test_confirm_rejects_problematic_candidate_selection(service):
    """带未解决问题的候选：选入被拒，排除后可确认其余票。"""
    # A 票正文金额 999 与附件 1000 冲突 -> needs_confirmation
    from batchkit import (machine_workbook_bytes, mail_table_html)
    from helpers import make_mail
    raw = make_mail(
        sender="tester@example.com",
        subject="金额冲突批次",
        text="见附件。",
        html=mail_table_html([("MSOA1", "S000001", 999),
                              ("MSOB1", "S000002", 2000)]),
        attachments=(
            ("a-invoice.xlsx",
             machine_workbook_bytes("INV-A-001", "MSOA1", "S000001", 1000)),
            ("b-invoice.xlsx",
             machine_workbook_bytes("INV-B-001", "MSOB1", "S000002", 2000)),
        ),
    )
    result = service.batches.import_eml_bytes(
        raw, filename="conflict.eml", actor="t")
    batch_id = result["batch"]["id"]
    plan = result["plan"]
    preview = result["preview"]
    cand_a = next(c for c in preview["candidates"]
                  if "INV-A-001" in c["identity"].get("invoice_no", []))
    cand_b = next(c for c in preview["candidates"]
                  if "INV-B-001" in c["identity"].get("invoice_no", []))
    assert cand_a["needs_confirmation"] is True
    assert preview["batch_ready"] is False
    # 选入问题候选被拒（不存在"忽略全部问题"的入口）
    with pytest.raises(ValidationError, match="仍存在未解决问题"):
        _confirm(service, batch_id, plan,
                 [cand_a["candidate_id"], cand_b["candidate_id"]])
    # 有理由排除 A 后可确认 B
    outcome = _confirm(service, batch_id, plan, [cand_b["candidate_id"]],
                       exclusions={cand_a["candidate_id"]: "金额待发起人确认"})
    assert len(outcome["tasks"]) == 1
    assert outcome["tasks"][0]["candidate_key"] == cand_b["candidate_id"]
