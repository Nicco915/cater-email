"""多票批次服务层测试（一）：导入、分层去重、方案版本与人工调整。

所有材料均为合成数据。运行要求与全套件一致（隔离 runtime，不读根 .env）。
"""
from __future__ import annotations

import json

import pytest

from cater.app.batches import HOLD_REASON
from cater.app.errors import ValidationError

from batchkit import (
    identity_free_workbook_bytes,
    import_two_ticket,
    machine_workbook_bytes,
    two_ticket_mail,
)


# ---------------------------------------------------------------------------
# 导入与去重
# ---------------------------------------------------------------------------


def test_import_creates_batch_plan_assets_and_events(service):
    batch_id, result = import_two_ticket(service)
    assert result["created"] is True
    preview = result["preview"]
    assert preview["candidate_count"] == 2
    assert preview["batch_ready"] is True
    batch = service.batches.require_batch(batch_id)
    assert batch["status"] == "open"
    assert batch["owner"] == "tester@example.com"
    assert batch["current_plan_id"]

    assets = service.batches.assets(batch_id)
    roles = {a["role"] for a in assets}
    assert roles == {"raw_mail", "attachment"}
    # 原始邮件 + 两份发票附件 = 3 份材料，全部哈希落盘
    assert len(assets) == 3
    for asset in assets:
        from pathlib import Path
        path = Path(asset["path"])
        assert path.is_file()
        import hashlib
        assert hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]

    events = [e["action"] for e in service.batches.events(batch_id)]
    assert "batch_imported" in events
    assert "plan_created" in events
    # mail_messages 关联行保持单票语义：task_id 为空且带过滤原因
    mail_row = service.mail_store.get(str(batch["mail_id"]))
    assert mail_row is not None
    assert mail_row["task_id"] is None
    assert mail_row["filter_reason"] == "multi_ticket_batch_import"


def test_import_rejections(service):
    with pytest.raises(ValidationError):
        service.batches.import_eml_bytes(b"", filename="x.eml", actor="t")
    from cater.app.preview import MAX_MESSAGE_BYTES
    with pytest.raises(ValidationError):
        service.batches.import_eml_bytes(
            b"x" * (MAX_MESSAGE_BYTES + 1), filename="x.eml", actor="t")
    # 缺发件人的邮件不能建批次
    from email.message import EmailMessage
    message = EmailMessage()
    message["To"] = "bot@example.com"
    message["Subject"] = "no sender"
    message.set_content("hi")
    with pytest.raises(ValidationError):
        service.batches.import_eml_bytes(
            message.as_bytes(), filename="x.eml", actor="t")


def test_repeat_import_same_mail_dedupes_to_one_batch(service):
    raw = two_ticket_mail()
    first = service.batches.import_eml_bytes(raw, filename="a.eml", actor="t1")
    second = service.batches.import_eml_bytes(raw, filename="b.eml", actor="t2")
    assert first["created"] is True
    assert second["created"] is False
    assert second["batch"]["id"] == first["batch"]["id"]
    # 去重不新增方案版本（识别器与指纹未变）
    assert len(service.batches.plans(first["batch"]["id"])) == 1
    assert len(service.batches.list_batches()) == 1


def test_concurrent_import_race_returns_single_batch(service, monkeypatch):
    """模拟并发：首个事务唯一约束冲突时返回既有批次，绝不建第二个。"""
    raw = two_ticket_mail()
    first = service.batches.import_eml_bytes(raw, filename="a.eml", actor="t1")
    # 第二次导入时让前置去重查询落空一次，强制 INSERT 撞唯一约束，
    # 走 _ConcurrentImport 分支；其后的兜底查询恢复正常。
    real_fetchone = type(service.db).fetchone
    state = {"missed": False}

    def fake_fetchone(self, sql, params=()):
        if ("FROM mail_batches WHERE account" in sql
                and not state["missed"]):
            state["missed"] = True
            return None
        return real_fetchone(self, sql, params)

    monkeypatch.setattr(type(service.db), "fetchone", fake_fetchone)
    second = service.batches.import_eml_bytes(raw, filename="b.eml", actor="t2")
    assert second["created"] is False
    assert second["batch"]["id"] == first["batch"]["id"]
    assert len(service.batches.list_batches()) == 1


def test_different_sender_or_content_makes_separate_batches(service):
    raw_a = two_ticket_mail(sender="tester@example.com")
    raw_b = two_ticket_mail(sender="other@example.com",
                            subject="另一位发起人的同一批发票")
    raw_c = two_ticket_mail(sender="tester@example.com",
                            subject="主题不同内容不同")
    first = service.batches.import_eml_bytes(raw_a, filename="a.eml", actor="t")
    second = service.batches.import_eml_bytes(raw_b, filename="b.eml", actor="t")
    third = service.batches.import_eml_bytes(raw_c, filename="c.eml", actor="t")
    ids = {first["batch"]["id"], second["batch"]["id"], third["batch"]["id"]}
    # 不同发起人 => 不同批次；同发起人但内容不同 => 不同批次
    assert len(ids) == 3
    assert len(service.batches.list_batches()) == 3


def test_rule_upgrade_adds_plan_version_not_new_batch(service, monkeypatch):
    """识别器升级：重复导入同一封邮件 -> 同一批次上追加新方案版本。"""
    raw = two_ticket_mail()
    first = service.batches.import_eml_bytes(raw, filename="a.eml", actor="t")
    batch_id = first["batch"]["id"]
    from cater.core import split_preview
    monkeypatch.setattr(split_preview, "INSPECTOR_VERSION", "test-upgraded")
    result = service.batches.import_eml_bytes(raw, filename="again.eml", actor="t")
    assert result["created"] is False
    assert result["batch"]["id"] == batch_id
    plans = service.batches.plans(batch_id)
    assert len(plans) == 2
    assert plans[-1]["version"] == 2
    assert plans[-1]["inspector_version"] == "test-upgraded"
    assert plans[-1]["derived_from"] == plans[0]["id"]
    # 指纹含识别器版本 => 新指纹，但批次身份（account/owner/mail_hash）不变
    assert plans[-1]["source_fingerprint"] != plans[0]["source_fingerprint"]


def test_source_fingerprint_not_permanent_identity(service):
    """批次身份只看 (account, owner, mail_hash)；指纹仅属方案版本。"""
    batch_id, _ = import_two_ticket(service)
    batch = service.batches.require_batch(batch_id)
    assert batch["mail_hash"]
    assert "source_fingerprint" not in batch


# ---------------------------------------------------------------------------
# 人工调整：指派/排除/撤销与新方案版本
# ---------------------------------------------------------------------------


def _unmatched_row_batch(service, **kwargs):
    """两票 + 一行未归属正文行的批次。"""
    kwargs.setdefault("extra_rows", [("MSOXX", "S009999", 3000)])
    return import_two_ticket(service, **kwargs)


def test_adjust_exclude_unmatched_mail_row_creates_new_plan(service):
    batch_id, result = _unmatched_row_batch(service)
    plan = result["plan"]
    preview = result["preview"]
    assert len(preview["unmatched_mail_rows"]) == 1
    row = preview["unmatched_mail_rows"][0]

    detail = service.batches.adjust_plan(
        batch_id, base_plan_id=plan["id"],
        ops=[{"type": "mail_row", "table_index": row["table_index"],
              "row": row["row"], "action": "exclude",
              "reason": "已在另一批次处理"}],
        actor="reviewer")
    new_plan = detail["plan"]
    assert new_plan["version"] == 2
    assert new_plan["derived_from"] == plan["id"]
    new_preview = detail["preview"]
    assert new_preview["unmatched_mail_rows"] == []
    assert len(new_preview["excluded_mail_rows"]) == 1
    assert new_preview["batch_ready"] is True
    # 调整有审计事件
    actions = [e["action"] for e in detail["events"]]
    assert "plan_adjusted" in actions


def test_adjust_assign_unmatched_row_attaches_and_rechecks(service):
    """指派把歧义/未命中行挂到唯一候选，随后机器映射/金额核对重新执行。"""
    batch_id, result = _unmatched_row_batch(service)
    plan = result["plan"]
    preview = result["preview"]
    row = preview["unmatched_mail_rows"][0]
    cand_a = next(c for c in preview["candidates"]
                  if "INV-A-001" in c["identity"].get("invoice_no", []))
    detail = service.batches.adjust_plan(
        batch_id, base_plan_id=plan["id"],
        ops=[{"type": "mail_row", "table_index": row["table_index"],
              "row": row["row"], "action": "assign",
              "candidate_id": cand_a["candidate_id"], "reason": ""}],
        actor="reviewer")
    new_preview = detail["preview"]
    assert new_preview["unmatched_mail_rows"] == []
    cand = next(c for c in new_preview["candidates"]
                if c["candidate_id"] == cand_a["candidate_id"])
    attached = [r for r in cand["mail_row_refs"] if r.get("assigned")]
    assert len(attached) == 1
    # 指派后重新核对：MSOXX/S009999 不属于 A 票 -> 身份问题 + 金额不完整
    codes = {i["code"] for i in cand["issues"]}
    assert codes & {"MAIL_ROW_MACHINE_AMBIGUOUS", "MAIL_ROW_NO_MACHINE",
                    "AMOUNT_CONFLICT"}
    assert cand["needs_confirmation"] is True
    assert new_preview["batch_ready"] is False


def test_adjust_validations(service):
    batch_id, result = _unmatched_row_batch(service)
    plan = result["plan"]
    preview = result["preview"]
    row = preview["unmatched_mail_rows"][0]

    # 排除必须给理由
    with pytest.raises(ValidationError):
        service.batches.adjust_plan(
            batch_id, base_plan_id=plan["id"],
            ops=[{"type": "mail_row", "table_index": row["table_index"],
                  "row": row["row"], "action": "exclude", "reason": ""}],
            actor="a")
    # 指派目标必须存在
    with pytest.raises(ValidationError):
        service.batches.adjust_plan(
            batch_id, base_plan_id=plan["id"],
            ops=[{"type": "mail_row", "table_index": row["table_index"],
                  "row": row["row"], "action": "assign",
                  "candidate_id": "cand-nope", "reason": ""}],
            actor="a")
    # 已归属的正文行不能再调整（目标必须来自未归属列表）
    with pytest.raises(ValidationError):
        service.batches.adjust_plan(
            batch_id, base_plan_id=plan["id"],
            ops=[{"type": "mail_row", "table_index": row["table_index"],
                  "row": 1, "action": "exclude", "reason": "伪造定位"}],
            actor="a")
    # 不存在的批次/方案
    with pytest.raises(KeyError):
        service.batches.adjust_plan(
            "batch-nope", base_plan_id=plan["id"],
            ops=[{"type": "mail_row", "table_index": 0, "row": 1,
                  "action": "exclude", "reason": "x"}], actor="a")


def test_adjust_stale_base_plan_rejected(service):
    """乐观锁：基于过期方案版本的调整被拒绝。"""
    batch_id, result = _unmatched_row_batch(service)
    plan = result["plan"]
    row = result["preview"]["unmatched_mail_rows"][0]
    op = {"type": "mail_row", "table_index": row["table_index"],
          "row": row["row"], "action": "exclude", "reason": "先处理掉"}
    service.batches.adjust_plan(batch_id, base_plan_id=plan["id"],
                                ops=[op], actor="a")
    # 再基于 v1 调整 -> 过期拒绝
    with pytest.raises(ValidationError):
        service.batches.adjust_plan(batch_id, base_plan_id=plan["id"],
                                    ops=[op], actor="a")


def test_adjust_clear_reverts_previous_op(service):
    batch_id, result = _unmatched_row_batch(service)
    plan = result["plan"]
    row = result["preview"]["unmatched_mail_rows"][0]
    op = {"type": "mail_row", "table_index": row["table_index"],
          "row": row["row"], "action": "exclude", "reason": "误排除"}
    detail = service.batches.adjust_plan(
        batch_id, base_plan_id=plan["id"], ops=[op], actor="a")
    assert detail["preview"]["unmatched_mail_rows"] == []
    # 撤销 -> 回到未归属状态
    detail2 = service.batches.adjust_plan(
        batch_id, base_plan_id=detail["plan"]["id"],
        ops=[{"type": "mail_row", "table_index": row["table_index"],
              "row": row["row"], "action": "clear"}], actor="a")
    assert len(detail2["preview"]["unmatched_mail_rows"]) == 1
    # 没有可撤销的调整时报错
    with pytest.raises(ValidationError):
        service.batches.adjust_plan(
            batch_id, base_plan_id=detail2["plan"]["id"],
            ops=[{"type": "mail_row", "table_index": row["table_index"],
                  "row": row["row"], "action": "clear"}], actor="a")


def test_adjust_source_exclude_and_assign(service):
    """附件级调整：坏 Excel 显式排除；无身份工作簿可指派到唯一候选。"""
    batch_id, result = import_two_ticket(
        service, broken_attachment=True,
        extra_attachments=(("notes.xlsx", identity_free_workbook_bytes()),))
    plan = result["plan"]
    preview = result["preview"]
    unmatched = {s["sha256"]: s for s in preview["unmatched_sources"]}
    broken = next(s for s in unmatched.values()
                  if "broken.xlsx" in (s.get("filenames") or [s.get("filename")]))
    notes = next(s for s in unmatched.values()
                 if "notes.xlsx" in (s.get("filenames") or [s.get("filename")]))
    assert broken["status"] in {"unreadable", "unsupported"}
    assert notes["status"] == "empty"
    cand_a = next(c for c in preview["candidates"]
                  if "INV-A-001" in c["identity"].get("invoice_no", []))

    # 坏 Excel 不能"指派"，只能有理由排除
    with pytest.raises(ValidationError):
        service.batches.adjust_plan(
            batch_id, base_plan_id=plan["id"],
            ops=[{"type": "source", "sha256": broken["sha256"],
                  "action": "assign", "candidate_id": cand_a["candidate_id"],
                  "reason": ""}], actor="a")
    detail = service.batches.adjust_plan(
        batch_id, base_plan_id=plan["id"],
        ops=[{"type": "source", "sha256": broken["sha256"],
              "action": "exclude", "reason": "损坏文件，已要求发起人重发"},
             {"type": "source", "sha256": notes["sha256"],
              "action": "assign", "candidate_id": cand_a["candidate_id"],
              "reason": ""}],
        actor="reviewer")
    new_preview = detail["preview"]
    assert {s["sha256"] for s in new_preview["unmatched_sources"]} == set() or \
        all(s["sha256"] not in {broken["sha256"], notes["sha256"]}
            for s in new_preview["unmatched_sources"])
    assert any(broken["sha256"] == s["sha256"]
               for s in new_preview["excluded_sources"])
    cand = next(c for c in new_preview["candidates"]
                if c["candidate_id"] == cand_a["candidate_id"])
    assigned_refs = [r for r in cand["source_refs"] if r.get("assigned")]
    assert any(notes["sha256"] == r["sha256"] for r in assigned_refs)


def test_adjust_rejected_after_materialization(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    preview = result["preview"]
    keys = [c["candidate_id"] for c in preview["candidates"]]
    service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="confirmer")
    with pytest.raises(ValidationError):
        service.batches.adjust_plan(
            batch_id, base_plan_id=plan["id"],
            ops=[{"type": "source", "sha256": "0" * 64, "action": "exclude",
                  "reason": "x"}], actor="a")
