"""多票批次服务层测试（三）：失败原子性、崩溃恢复与逐票快照隔离。

承诺：文件落盘失败/事务中途失败 -> 零任务零确认；重启后重试得到同一组
任务；快照文件先于 DB 提交持久化，篡改即拒绝。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cater.app.errors import ValidationError

from batchkit import import_two_ticket


def _keys(preview):
    return [c["candidate_id"] for c in preview["candidates"]]


def _confirm(service, batch_id, plan, keys):
    return service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="confirmer")


def test_snapshot_files_durable_and_isolated_per_ticket(service):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])
    outcome = _confirm(service, batch_id, plan, keys)
    tasks = {t["candidate_key"]: t for t in outcome["tasks"]}
    assert len(tasks) == 2
    attachment_assets = [a for a in service.batches.assets(batch_id)
                         if a["role"] == "attachment"]

    seen_dirs = set()
    for key, item in tasks.items():
        snap_path = Path(item["snapshot_path"])
        assert snap_path.is_file()
        snap_dir = snap_path.parent
        seen_dirs.add(str(snap_dir))
        payload = json.loads(snap_path.read_text(encoding="utf-8"))
        assert payload["batch_id"] == batch_id
        assert payload["plan_id"] == plan["id"]
        assert payload["source_fingerprint"] == plan["source_fingerprint"]
        assert payload["candidate"]["candidate_id"] == key
        # 快照内只有本票自己的附件，且逐文件哈希一致
        cand = next(c for c in result["preview"]["candidates"]
                    if c["candidate_id"] == key)
        own_shas = {r["sha256"] for r in cand["source_refs"]}
        assert {f["sha256"] for f in payload["assets"]} == own_shas
        for f in payload["assets"]:
            data = Path(f["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == f["sha256"]
            assert Path(f["path"]).parent == snap_dir
    # 两票快照目录互不共享（独立冻结输入）
    assert len(seen_dirs) == 2
    # 批次资产原件不被移动（生命周期安全）
    for asset in attachment_assets:
        assert Path(asset["path"]).is_file()


def test_file_write_failure_leaves_zero_tasks_and_open_batch(
        service, monkeypatch):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])

    import cater.app.batches as batches_mod
    real_write = batches_mod._write_file_atomic

    def failing_write(path, data):
        raise OSError("模拟磁盘写失败")

    monkeypatch.setattr(batches_mod, "_write_file_atomic", failing_write)
    with pytest.raises(OSError):
        _confirm(service, batch_id, plan, keys)
    # 零任务零确认
    assert service.batches.batch_tasks(batch_id) == []
    batch = service.batches.require_batch(batch_id)
    assert batch["status"] == "open"
    assert batch["confirmed_at"] is None
    # 恢复后重试成功（同请求 -> 完整建票）
    monkeypatch.setattr(batches_mod, "_write_file_atomic", real_write)
    outcome = _confirm(service, batch_id, plan, keys)
    assert len(outcome["tasks"]) == 2


def test_db_failure_mid_transaction_rolls_back_everything(
        service, monkeypatch):
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])

    real_insert = type(service.tasks)._insert_task_conn

    def failing_insert(self, conn, **kwargs):
        raise RuntimeError("模拟事务中途失败")

    monkeypatch.setattr(
        type(service.tasks), "_insert_task_conn", failing_insert)
    with pytest.raises(RuntimeError):
        _confirm(service, batch_id, plan, keys)
    # 事务整体回滚：零任务、候选决定未落、批次未确认
    assert service.batches.batch_tasks(batch_id) == []
    assert all(c["decision"] == "pending"
               for c in service.batches.candidates(plan["id"]))
    batch = service.batches.require_batch(batch_id)
    assert batch["status"] == "open"
    assert batch["confirmed_at"] is None

    # 崩溃残留：快照目录已随确认事务发布但未被引用（事务回滚）；发布
    # 未满宽限窗口时 sweep 不会报告，重试发布新独占目录成功（旧残留
    # 不阻塞、绝不覆盖已引用目录）。
    snaps_root = service.batches._snapshots_dir(batch_id)
    published = [p for p in snaps_root.rglob("*")
                 if p.is_dir() and (p / "snapshot.json").is_file()]
    assert len(published) == 2
    # 恢复后重试：完整创建同一组任务（残留的发布目录被替换，不阻塞）
    monkeypatch.setattr(
        type(service.tasks), "_insert_task_conn", real_insert)
    outcome = _confirm(service, batch_id, plan, keys)
    assert len(outcome["tasks"]) == 2
    assert service.batches.orphan_snapshot_report()["count"] == 0


def test_stale_residue_swept_after_ttl(service, monkeypatch):
    """陈旧（超过安全窗口）的未引用发布/暂存目录被 sweep 回收。"""
    import time as time_mod
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])
    real_insert = type(service.tasks)._insert_task_conn

    def failing_insert(self, conn, **kwargs):
        raise RuntimeError("模拟事务中途失败")

    monkeypatch.setattr(
        type(service.tasks), "_insert_task_conn", failing_insert)
    with pytest.raises(RuntimeError):
        _confirm(service, batch_id, plan, keys)
    # 把残留目录（含方案中间目录）的 mtime 拨到宽限窗口之前
    snaps_root = service.batches._snapshots_dir(batch_id)
    old = time_mod.time() - 1200
    import os
    for root, dirnames, filenames in os.walk(snaps_root):
        for name in dirnames:
            os.utime(Path(root) / name, (old, old))
    report = service.batches.orphan_snapshot_report()
    assert report["count"] == 2
    assert {o["batch_id"] for o in report["orphans"]} == {batch_id}
    swept = service.batches.sweep_orphan_snapshots()
    assert swept["count"] == 2
    assert service.batches.orphan_snapshot_report()["count"] == 0
    # 恢复后重试：完整创建同一组任务
    monkeypatch.setattr(
        type(service.tasks), "_insert_task_conn", real_insert)
    outcome = _confirm(service, batch_id, plan, keys)
    assert len(outcome["tasks"]) == 2


def test_retry_reuses_verified_snapshot_files(service):
    """重试时快照文件已存在且哈希一致 -> 复用而不是重写（崩溃幂等）。"""
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])
    first = _confirm(service, batch_id, plan, keys)
    snap_paths = {t["snapshot_path"] for t in first["tasks"]}
    second = _confirm(service, batch_id, plan, keys)
    assert second["idempotent"] is True
    assert {t["snapshot_path"] for t in second["tasks"]} == snap_paths


def test_tampered_batch_asset_refused(service):
    """批次资产被篡改：哈希校验失败，拒绝建票，绝不静默读新数据。"""
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])
    asset = next(a for a in service.batches.assets(batch_id)
                 if a["role"] == "attachment")
    path = Path(asset["path"])
    path.write_bytes(b"tampered content")
    with pytest.raises(ValidationError, match="校验失败"):
        _confirm(service, batch_id, plan, keys)
    assert service.batches.batch_tasks(batch_id) == []
    assert service.batches.require_batch(batch_id)["status"] == "open"


def test_tampered_staged_snapshot_refused(service, monkeypatch):
    """发布后对独占快照目录内附件的篡改，在提交前最终复核中被识别拒绝。"""
    import cater.app.batches as batches_mod
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])

    real_publish = batches_mod.BatchService._publish_snapshot_dirs

    def tampering_publish(self, conn, batch_id, plan, candidates):
        published = real_publish(self, conn, batch_id, plan, candidates)
        # 模拟“发布校验后、事务提交前”目录内附件被改写
        target = Path(published[keys[0]]["dir"])
        asset_file = next(p for p in target.iterdir() if p.name != "snapshot.json")
        asset_file.write_bytes(b"planted tampered content")
        return published

    monkeypatch.setattr(
        batches_mod.BatchService, "_publish_snapshot_dirs", tampering_publish)
    with pytest.raises(ValidationError, match="不一致|篡改"):
        _confirm(service, batch_id, plan, keys)
    assert service.batches.batch_tasks(batch_id) == []
    assert service.batches.require_batch(batch_id)["status"] == "open"


def test_materialized_batch_immutable(service):
    """已物化批次：不可再调整方案，事件/快照/任务保持原样。"""
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = _keys(result["preview"])
    outcome = _confirm(service, batch_id, plan, keys)
    event_count = len(service.batches.events(batch_id))
    task_ids = {t["task_id"] for t in outcome["tasks"]}

    with pytest.raises(ValidationError):
        service.batches.adjust_plan(
            batch_id, base_plan_id=plan["id"],
            ops=[{"type": "source", "sha256": "0" * 64,
                  "action": "exclude", "reason": "x"}], actor="a")
    assert service.batches.require_batch(batch_id)["status"] == "materialized"
    assert len(service.batches.events(batch_id)) == event_count
    assert {t["task_id"] for t in service.batches.batch_tasks(batch_id)} \
        == task_ids
