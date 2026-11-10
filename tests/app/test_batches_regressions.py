"""阻断缺陷复现与修复的回归测试（阶段 2 返工）。

1) 同内容不同名附件只按哈希落盘/入库一份，原名入审计；批次身份唯一
   冲突之外的 IntegrityError 是明确失败并清理孤儿材料。
2) 快照完整业务内容校验（candidate/资产清单/正文行/版本/指纹/路径
   边界），不依赖两个 ID；时间戳规范化；确认持久化可信快照哈希并支持
   事后核验；跨方案残留不阻塞合法新方案；输入隔离保持。
3) 首轮识别失败后可重入恢复 v1（重复导入/并发恢复不产生重复版本或
   孤儿材料）。
附加：原子写独占暂存与并发发布验证（受控 barrier + 双连接真实并发）。
TOCTOU：迟到旧请求 p1 在同步边界前暂停、p2 先确认成功，恢复 p1 后
必须在改动任何已发布文件前被拦下（同进程受控 barrier + 独立进程
确认/清扫竞争双证明）；清扫在同一可靠同步边界重新核验引用，已引用
目录绝不改写/删除；锁外不得 rmdir 空容器——活跃事务刚 mkdir 尚未
写文件的在制品目录必须幸存（独立进程 barrier 证明），空容器本阶段
保留不动。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from cater.app import engine as engine_mod
from cater.app.errors import ValidationError

from batchkit import (
    import_two_ticket,
    machine_workbook_bytes,
    two_ticket_mail,
)
from helpers import make_config, make_mail


# ---------------------------------------------------------------------------
# 缺陷 1：附件按哈希去重 + 精确身份冲突判定
# ---------------------------------------------------------------------------


def test_same_content_different_name_attachments_deduped(service):
    """first.xlsx 与 copy.xlsx 同内容：只入库一份，原名并入审计。"""
    data = machine_workbook_bytes("SYN-DUP", "SYNMSO", "S123456", 100)
    raw = make_mail(
        sender="tester@example.com", subject="重复附件",
        text="见附件",
        attachments=(("first.xlsx", data), ("copy.xlsx", data)))
    result = service.batches.import_eml_bytes(
        raw, filename="dup.eml", actor="t")
    # 修复前：这里抛 _ConcurrentImport（附件唯一冲突被误判为重复导入）
    assert result["created"] is True
    preview = result["preview"]
    assert preview["candidate_count"] == 1
    assert preview["batch_ready"] is True
    attachments = [a for a in service.batches.assets(result["batch"]["id"])
                   if a["role"] == "attachment"]
    assert len(attachments) == 1  # 同哈希只入库一份
    assert attachments[0]["size_bytes"] == len(data)


def test_non_identity_integrity_error_is_explicit_failure(
        service, monkeypatch, tmp_path):
    """批次身份之外的 IntegrityError 必须明确失败并清理孤儿材料。"""
    real_transaction = type(service.db).transaction

    class _GuardedConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "INSERT INTO batch_assets" in sql:
                raise sqlite3.IntegrityError("模拟非身份唯一冲突")
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextlib.contextmanager
    def failing_transaction(self):
        with real_transaction(self) as conn:
            yield _GuardedConnection(conn)

    monkeypatch.setattr(type(service.db), "transaction", failing_transaction)
    raw = two_ticket_mail()
    with pytest.raises(sqlite3.IntegrityError):
        service.batches.import_eml_bytes(raw, filename="bad.eml", actor="t")
    # 无批次残留、孤儿材料被清理
    assert service.batches.list_batches() == []
    root = service.batches._root()
    if root.is_dir():
        assert list(root.iterdir()) == []


# ---------------------------------------------------------------------------
# 缺陷 2：快照完整业务校验 + 可信哈希 + 跨方案残留
# ---------------------------------------------------------------------------


def test_tampered_snapshot_content_refused(service, monkeypatch):
    """改 candidate 金额/清空 mail_rows（保留两个 ID）：确认必须拒绝。"""
    import cater.app.batches as batches_mod
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    real_publish = batches_mod.BatchService._publish_snapshot_dirs

    def tampering_publish(self, conn, batch_id, plan, candidates):
        published = real_publish(self, conn, batch_id, plan, candidates)
        for key in keys:
            snap = Path(published[key]["dir"]) / "snapshot.json"
            payload = json.loads(snap.read_text())
            payload["candidate"]["facts"]["amount"]["value"] = "999999"
            payload["mail_rows"] = []
            snap.write_text(json.dumps(payload))
        return published

    monkeypatch.setattr(
        batches_mod.BatchService, "_publish_snapshot_dirs", tampering_publish)
    with pytest.raises(ValidationError):
        service.batches.confirm_and_create(
            batch_id, plan_id=plan["id"],
            fingerprint=plan["source_fingerprint"],
            selected=keys, exclusions={}, actor="x")
    assert service.batches.batch_tasks(batch_id) == []
    assert service.batches.require_batch(batch_id)["status"] == "open"


def test_snapshot_trusted_hash_persisted_and_verifiable(service):
    """确认持久化可信哈希；事后 verify 通过；篡改后 verify 拒绝。"""
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    outcome = service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="x")
    for item in outcome["tasks"]:
        assert item["snapshot_hash"]
        check = service.batches.verify_batch_task_snapshot(item["task_id"])
        assert check["ok"] is True
        assert check["verified_hash"] == item["snapshot_hash"]
    # 篡改已物化任务快照 -> verify 拒绝
    target = outcome["tasks"][0]
    snap = Path(target["snapshot_path"])
    payload = json.loads(snap.read_text())
    payload["candidate"]["facts"]["amount"]["value"] = "1"
    snap.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        service.batches.verify_batch_task_snapshot(target["task_id"])


def test_cross_plan_residue_does_not_block_new_confirmation(service):
    """跨方案残留发布目录不阻塞也不被覆盖：合法新方案确认成功。"""
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    # 预置一个旧方案残留的发布目录（新布局形态，身份为旧方案）
    residue = (service.batches._snapshots_dir(batch_id) / ("plan-" + "f" * 32)
               / f"{keys[0]}-{'e' * 16}")
    residue.mkdir(parents=True)
    (residue / "snapshot.json").write_text(json.dumps({
        "schema": 1, "batch_id": batch_id, "plan_id": "plan-old",
        "plan_version": 99, "source_fingerprint": "f" * 64,
        "candidate": {"candidate_id": keys[0]}, "assets": [],
        "mail_rows": []}))
    outcome = service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="x")
    assert len(outcome["tasks"]) == 2
    # 已确认任务引用的是本次独占目录（非残留目录），核验通过
    assert all(service.batches.verify_batch_task_snapshot(t["task_id"])["ok"]
               for t in outcome["tasks"])
    assert all(Path(t["snapshot_path"]).parent != residue
               for t in outcome["tasks"])
    # 残留目录原样保留（不被他人确认改动），等 sweep 回收
    assert (residue / "snapshot.json").is_file()


def test_snapshot_input_isolation_per_ticket(service):
    """两票快照目录互不共享；批次资产原件不被移动。"""
    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    outcome = service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="x")
    import hashlib
    dirs = set()
    for item in outcome["tasks"]:
        payload = json.loads(Path(item["snapshot_path"]).read_text())
        assert Path(item["snapshot_path"]).parent == \
            Path(item["snapshot_path"]).resolve().parent
        dirs.add(str(Path(item["snapshot_path"]).parent))
        own_shas = {f["sha256"] for f in payload["assets"]}
        for f in payload["assets"]:
            assert Path(f["path"]).parent == Path(item["snapshot_path"]).parent
            assert hashlib.sha256(Path(f["path"]).read_bytes()).hexdigest() \
                == f["sha256"]
    assert len(dirs) == 2
    for asset in service.batches.assets(batch_id):
        assert Path(asset["path"]).is_file()


# ---------------------------------------------------------------------------
# 缺陷 3：首轮识别失败的可重入恢复
# ---------------------------------------------------------------------------


def test_initial_inspect_failure_recovers_on_reimport(service, monkeypatch):
    """首轮 _run_inspect 抛错 -> 重复导入可重入恢复 v1，无重复版本。"""
    import cater.app.batches as batches_mod
    real_loader = batches_mod.load_inspect_sources
    state = {"calls": 0}

    def flaky():
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("首轮识别故障")
        return real_loader()

    monkeypatch.setattr(batches_mod, "load_inspect_sources", flaky)
    raw = two_ticket_mail()
    with pytest.raises(RuntimeError):
        service.batches.import_eml_bytes(raw, filename="flaky.eml", actor="t")
    # 批次行已建但无方案；重复导入触发可重入恢复
    monkeypatch.setattr(batches_mod, "load_inspect_sources", real_loader)
    result = service.batches.import_eml_bytes(raw, filename="flaky.eml",
                                              actor="t")
    assert result["created"] is False
    plan = result["plan"]
    assert plan is not None
    assert plan["version"] == 1
    assert len(service.batches.plans(result["batch"]["id"])) == 1
    assert result["batch"]["status"] == "open"


def test_concurrent_initial_recovery_single_version(service, monkeypatch):
    """并发重复导入对"无方案"批次：只恢复出一个 v1，无重复版本。"""
    import cater.app.batches as batches_mod
    real_loader = batches_mod.load_inspect_sources
    state = {"calls": 0}

    def flaky():
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("首轮识别故障")
        return real_loader()

    monkeypatch.setattr(batches_mod, "load_inspect_sources", flaky)
    raw = two_ticket_mail()
    with pytest.raises(RuntimeError):
        service.batches.import_eml_bytes(raw, filename="f.eml", actor="t")
    monkeypatch.setattr(batches_mod, "load_inspect_sources", real_loader)
    # 并发重复导入同一邮件（同进程多线程，真实竞争恢复路径）
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(service.batches.import_eml_bytes(
                raw, filename="f.eml", actor="t"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    batch_ids = {r["batch"]["id"] for r in results}
    assert len(batch_ids) == 1
    batch_id = batch_ids.pop()
    assert len(service.batches.plans(batch_id)) == 1
    assert len(service.batches.list_batches()) == 1


# ---------------------------------------------------------------------------
# 附加：原子写独占暂存 + 真实并发（受控 barrier + 双连接）
# ---------------------------------------------------------------------------


def test_write_file_atomic_unique_temp_no_clobber(monkeypatch):
    """固定 .tmp 名被替换后，并发写互不覆盖/丢失 replace 源。"""
    import cater.app.batches as batches_mod

    written: list[bytes] = []
    real_open = Path.open

    def tracking_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if "b" in mode and "w" in mode:
            original_write = handle.write

            def wrapped(data):
                written.append(bytes(data))
                return original_write(data)
            handle.write = wrapped
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)
    target = Path("/tmp/_atomic_test.bin")
    barrier = threading.Barrier(8)

    def worker(payload: bytes):
        barrier.wait()
        batches_mod._write_file_atomic(target, payload)

    payloads = [f"payload-{i}".encode() * 100 for i in range(8)]
    threads = [threading.Thread(target=worker, args=(p,))
               for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert target.read_bytes() in payloads
    # 无残留固定 .tmp 名
    assert not (target.parent / (target.name + ".tmp")).exists()
    target.unlink(missing_ok=True)


def test_concurrent_import_distinct_content_single_batch(tmp_path):
    """双连接 + 受控 barrier：同一邮件并发导入只建一个批次。"""
    from cater.app.batches import BatchService
    from cater.app.db import Database
    from cater.app.logging import JsonlLogger
    from cater.app.mail_store import MailStore
    from cater.app.tasks import TaskRepository

    db_path = tmp_path / "runtime" / "cater.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    logger = JsonlLogger(tmp_path / "runtime" / "app.jsonl")

    def build_service(apply_migrations: bool):
        conn = sqlite3.connect(str(db_path), check_same_thread=False,
                               isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        if apply_migrations:
            conn.executescript(_SCHEMA_HINT)  # 由首个连接完成建表
        db = Database(db_path, connection=conn)
        config = make_config(tmp_path)
        tasks = TaskRepository(db, logger)
        return (BatchService(db, config, logger, tasks, MailStore(db, config)),
                conn)

    # 先由单个连接完成迁移（双连接并发迁移会互相阻塞）
    _, setup_conn = build_service(apply_migrations=True)
    setup_conn.close()

    raw = two_ticket_mail()
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        service, conn = build_service(apply_migrations=False)
        barrier.wait()
        try:
            results.append(service.import_eml_bytes(
                raw, filename="race.eml", actor="t"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(results) == 2
    assert len({r["batch"]["id"] for r in results}) == 1  # 同批次
    # 只有一份批次、一份材料（孤儿被清理）
    probe, conn = build_service(apply_migrations=False)
    assert len(probe.list_batches()) == 1
    batch_id = results[0]["batch"]["id"]
    assert len(probe.assets(batch_id)) == 3  # raw + 2 附件（哈希去重）
    conn.close()


_SCHEMA_HINT = "SELECT 1;"  # 占位：迁移由 Database.__init__ 完成


# ---------------------------------------------------------------------------
# TOCTOU：迟到旧请求 + 跨进程确认/清扫竞争
# ---------------------------------------------------------------------------


def test_stale_confirm_never_overwrites_published_snapshots(
        service, monkeypatch):
    """受控 barrier 复现协调人 TOCTOU：旧请求 p1 在同步边界前暂停，
    主线程产生 p2 并确认成功；恢复 p1 后必须在改动任何已发布文件前被
    拦下——p2 已确认快照字节级不变，trusted_hash 核验通过。"""
    import hashlib

    import cater.app.batches as batches_mod

    batch_id, result = import_two_ticket(service)
    plan1 = result["plan"]
    keys = sorted(c["candidate_id"]
                  for c in result["preview"]["candidates"])
    preview = json.loads(str(plan1["preview_json"]))

    gate = threading.Event()
    release = threading.Event()
    holder: dict[str, threading.Thread] = {}
    real_lock_fn = batches_mod.BatchService._confirm_lock

    def gated_lock(self, batch_id):
        if threading.current_thread() is holder.get("t"):
            gate.set()
            assert release.wait(30), "主线程未在限时内完成 p2 确认"
        return real_lock_fn(self, batch_id)

    monkeypatch.setattr(
        batches_mod.BatchService, "_confirm_lock", gated_lock)

    errors: list[BaseException] = []

    def stale_confirm():
        try:
            service.batches.confirm_and_create(
                batch_id, plan_id=plan1["id"],
                fingerprint=plan1["source_fingerprint"],
                selected=keys, exclusions={}, actor="stale")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=stale_confirm)
    holder["t"] = t
    t.start()
    assert gate.wait(30), "旧请求未到达同步边界闸门"

    # 主线程：产生 p2（候选相同即可），随后确认 p2 建票成功
    batch = service.batches.require_batch(batch_id)
    plan2 = service.batches._add_plan_version(
        batch, base=plan1, preview=preview, assignments=[],
        actor="main", reason="并发产生新方案")
    outcome2 = service.batches.confirm_and_create(
        batch_id, plan_id=plan2["id"],
        fingerprint=plan2["source_fingerprint"],
        selected=keys, exclusions={}, actor="main")
    assert outcome2["idempotent"] is False

    def snapshot_bytes(task):
        d = Path(task["snapshot_path"]).parent
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(d.iterdir())}

    before = {t_["task_id"]: snapshot_bytes(t_) for t_ in outcome2["tasks"]}
    release.set()
    t.join(30)
    assert not t.is_alive()
    # p1 被明确拒绝（ValidationError），绝不静默成功
    assert len(errors) == 1 and isinstance(errors[0], ValidationError), \
        errors
    # p2 已发布快照字节级不变（迟到 p1 未触碰），trusted_hash 核验通过
    after = {t_["task_id"]: snapshot_bytes(t_) for t_ in outcome2["tasks"]}
    assert after == before
    assert all(
        service.batches.verify_batch_task_snapshot(t_["task_id"])["ok"]
        for t_ in outcome2["tasks"])
    # 事务只关联正确版本：任务/快照均为 p2
    batch = service.batches.require_batch(batch_id)
    assert batch["confirmed_plan_id"] == plan2["id"]
    for t_ in outcome2["tasks"]:
        payload = json.loads(Path(t_["snapshot_path"]).read_text())
        assert payload["plan_id"] == plan2["id"]
    # p1 未留下任何私有发布目录
    snaps = service.batches._snapshots_dir(batch_id)
    dirs = [p for p in snaps.rglob("*") if p.is_dir()
            and (p / "snapshot.json").is_file()]
    assert len(dirs) == 2


def test_cross_process_confirm_sweep_race(service, tmp_path):
    """独立进程确认 + 主进程连续清扫：已引用目录不被改写/删除，
    已确认快照与 trusted_hash 核验通过；未引用陈旧残留被回收。"""
    import hashlib
    import os
    import subprocess
    import sys
    import time
    from types import SimpleNamespace

    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = sorted(c["candidate_id"]
                  for c in result["preview"]["candidates"])
    runtime = service.batches.config.paths.runtime_dir
    db_path = service.batches.config.db_path

    # 预置一个陈旧未引用残留目录（应被清扫回收，且不影响确认）
    stale = (service.batches._snapshots_dir(batch_id)
             / ("plan-" + "0" * 32) / f"{keys[0]}-{'d' * 16}")
    stale.mkdir(parents=True)
    (stale / "snapshot.json").write_text("{}")
    old = time.time() - 1200
    for root, dirnames, _ in os.walk(service.batches._snapshots_dir(batch_id)):
        for name in dirnames:
            os.utime(Path(root) / name, (old, old))

    driver = tmp_path / "confirm_driver.py"
    driver.write_text(
        "import hashlib, json, sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from cater.app.batches import BatchService\n"
        "from cater.app.db import Database\n"
        "from cater.app.logging import JsonlLogger\n"
        "from cater.app.mail_store import MailStore\n"
        "from cater.app.tasks import TaskRepository\n"
        "runtime = Path(sys.argv[2]); db_path = Path(sys.argv[3])\n"
        "spec = json.loads(sys.argv[4])\n"
        "config = SimpleNamespace(paths=SimpleNamespace(runtime_dir=runtime))\n"
        "db = Database(db_path)\n"
        "logger = JsonlLogger(runtime / 'logs' / 'driver.jsonl')\n"
        "tasks = TaskRepository(db, logger)\n"
        "svc = BatchService(db, config, logger, tasks, MailStore(db, config))\n"
        "outcome = svc.confirm_and_create(\n"
        "    spec['batch_id'], plan_id=spec['plan_id'],\n"
        "    fingerprint=spec['fingerprint'], selected=spec['selected'],\n"
        "    exclusions={}, actor='driver')\n"
        "proof = {}\n"
        "for t in outcome['tasks']:\n"
        "    d = Path(t['snapshot_path']).parent\n"
        "    proof[t['task_id']] = {\n"
        "        'dir': str(d),\n"
        "        'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()\n"
        "                  for p in sorted(d.iterdir())},\n"
        "    }\n"
        "print(json.dumps({'ok': True, 'proof': proof}))\n",
        encoding="utf-8")
    import cater.app.batches as batches_mod
    src_root = str(
        Path(batches_mod.__file__).resolve().parent.parent.parent)
    spec = json.dumps({"batch_id": batch_id, "plan_id": plan["id"],
                       "fingerprint": plan["source_fingerprint"],
                       "selected": keys})
    proc = subprocess.Popen(
        [sys.executable, str(driver), src_root, str(runtime), str(db_path),
         spec],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # 主进程在整个确认期间连续清扫（真实双进程竞争）
    for _ in range(2000):
        if proc.poll() is not None:
            break
        service.batches.sweep_orphan_snapshots()
        time.sleep(0.02)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    data = json.loads(out.strip().splitlines()[-1])
    assert data["ok"] is True
    # 收尾再清扫一次：陈旧残留必须被回收
    service.batches.sweep_orphan_snapshots()
    assert not stale.exists()
    # 已引用目录不被改写/删除：字节级与独立进程提交时一致，核验通过
    for task_id, item in data["proof"].items():
        d = Path(item["dir"])
        assert d.is_dir()
        actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(d.iterdir())}
        assert actual == item["files"]
        assert service.batches.verify_batch_task_snapshot(task_id)["ok"]


def test_sweep_never_removes_live_in_progress_publish_dir(service, tmp_path):
    """独立进程 barrier：活跃事务（持 BEGIN IMMEDIATE）刚 mkdir 空发布
    目录（_publish_snapshot_dirs 的 mkdir→首写窗口）后暂停；清扫不得
    删除该在制品目录；释放后真实确认可完成且 trusted_hash 核验通过。"""
    import json
    import os
    import subprocess
    import sys

    batch_id, result = import_two_ticket(service)
    plan = result["plan"]
    keys = sorted(c["candidate_id"]
                  for c in result["preview"]["candidates"])
    snaps = service.batches._snapshots_dir(batch_id)
    live_rel = f"{plan['id']}/{keys[0]}-{'c' * 16}"
    live = snaps / live_rel
    db_path = service.batches.config.db_path

    driver = tmp_path / "live_window_driver.py"
    driver.write_text(
        "import json, sqlite3, sys\n"
        "from pathlib import Path\n"
        "db_path = Path(sys.argv[1]); live = Path(sys.argv[2])\n"
        "conn = sqlite3.connect(str(db_path), isolation_level=None)\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "live.mkdir(parents=True)  # 在制品：mkdir 后、首写前\n"
        "print('READY', flush=True)\n"
        "input()  # 主进程清扫窗口\n"
        "print(json.dumps({'exists_after_sweep': live.exists()}),"
        " flush=True)\n"
        "conn.execute('ROLLBACK')\n"
        "conn.close()\n",
        encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(db_path), str(live)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert line.strip() == "READY", line
        # 主进程在子进程持活跃事务且空目录刚建立时清扫：旧实现的锁外
        # 空目录 rmdir 会删掉该在制品目录
        swept = service.batches.sweep_orphan_snapshots()
        assert live.is_dir(), "清扫删除了活跃事务的在制品目录"
        assert not any(str(live) == r or str(live).startswith(r + os.sep)
                       for r in swept["removed"])
        proc.stdin.write("\n")
        proc.stdin.flush()
        outcome = json.loads(proc.stdout.readline())
        assert outcome["exists_after_sweep"] is True
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    # 释放后允许完成真实确认并验证 hash（在制品空容器保留无害）
    confirmed = service.batches.confirm_and_create(
        batch_id, plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="main")
    assert confirmed["idempotent"] is False
    assert all(service.batches.verify_batch_task_snapshot(t["task_id"])["ok"]
               for t in confirmed["tasks"])
