"""阶段 3 离线单票执行服务测试（全合成数据）。

覆盖：快照复核拦截（篡改/不 ok）、verify→read 间快照替换的 TOCTOU 可信
绑定（实际消费 payload 必须哈希等于 DB trusted_hash，核心零调用零产物）、
时间戳元数据同一规范化口径不误拒、父邮件与别票材料绝不读取、两票不串、
hold/outbox/任务状态保持、同目录并发独占、拒绝覆盖输出、失败如实报告、
runner 绝不初始化新库/补种参考数据、CLI 入口与最小离线配置（不读 .env）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from cater.app.batch_offline import (
    REPORT_NAME,
    BatchOfflineRunner,
    load_offline_config,
)
from cater.app.cli import main as cli_main
from cater.app.db import Database
from cater.app.errors import ConfigurationError, ValidationError
from cater.core.models import Source, WeightGroup
from cater.core.parsing.machine_wb import MachineRow, MachineWorkbookResult

from batchkit import machine_workbook_bytes
from helpers import make_config, make_mail


# ---------------------------------------------------------------------------
# 合成两票邮件（正文含完整业务字段，真实链路可达 ready）
# ---------------------------------------------------------------------------

_TICKET_FIELDS = {
    "A": {"mso": "MSOA1", "serial": "S000001", "price": "1000",
          "dealer": "DCA1", "dealer_order": "DOA1", "order": "ORDA1"},
    "B": {"mso": "MSOB1", "serial": "S000002", "price": "2000",
          "dealer": "DCB1", "dealer_order": "DOB1", "order": "ORDB1"},
}

_COLUMNS = (
    ("MSO", "mso"), ("Serial Number", "serial"),
    ("Total Sales Price", "price"), ("Dealer Code", "dealer"),
    ("Dealer Order", "dealer_order"), ("Order", "order"),
    ("Material", None),
)


def _rich_two_ticket_mail() -> bytes:
    header = "".join(f"<td>{label}</td>" for label, _ in _COLUMNS)
    body = ""
    for key in ("A", "B"):
        f = _TICKET_FIELDS[key]
        cells = "".join(f"<td>{f[field] if field else 'TESTMODEL'}</td>"
                        for _, field in _COLUMNS)
        body += f"<tr>{cells}</tr>"
    html = ("<html><body><p>请见两票订单。</p><table>"
            f"<tr>{header}</tr>{body}</table></body></html>")
    attachments = (
        ("a-invoice.xlsx", machine_workbook_bytes("INV-A-001", "MSOA1", "S000001", 1000)),
        ("b-invoice.xlsx", machine_workbook_bytes("INV-B-001", "MSOB1", "S000002", 2000)),
    )
    return make_mail(sender="tester@example.com", subject="两票整机发票",
                     text="见附件。", html=html, attachments=attachments)


@pytest.fixture()
def materialized(service):
    """导入并确认两票批次，返回两个持 hold 的子任务信息。"""
    result = service.batches.import_eml_bytes(
        _rich_two_ticket_mail(), filename="batch.eml", actor="tester")
    batch = result["batch"]
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    assert len(keys) == 2
    outcome = service.batches.confirm_and_create(
        batch["id"], plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="confirmer")
    tasks = sorted(outcome["tasks"], key=lambda t: t["candidate_key"])
    return {"batch_id": batch["id"], "plan": plan, "tasks": tasks,
            "preview": result["preview"]}


def _task_for(tasks, letter):
    mso = _TICKET_FIELDS[letter]["mso"]
    # candidate_key 即 candidate_id；通过快照 candidate 身份辨别
    for task in tasks:
        snap = json.loads(Path(task["snapshot_path"]).read_text(encoding="utf-8"))
        if mso in snap["candidate"]["identity"].get("mso", []):
            return task
    raise AssertionError(f"找不到 {letter} 票任务")


def _prepare_ready(service, task_id):
    """参考机型 + 目的国就位（参考服务正式接口；任务目的国模拟人工已回复）。"""
    service.references.store_machines([{
        "model": "TESTMODEL",
        "english_name": "TESTMODEL SYNTHETIC LOADER",
        "hs_code": "8429401900",
        "product_name": "压路机",
        "declaration": "1|0|合成申报要素|是自推进|100KG|品牌|TESTMODEL|无|无|无",
        "destination_scope": "default",
    }], source="test", actor="tester")
    service.db.execute(
        "UPDATE tasks SET destination_name = '法国', destination_alpha3 = 'FRA'"
        " WHERE id = ?", (task_id,))


def _install_fake_machine_parser(monkeypatch, calls: list):
    """契约形态的假 B：按已确认身份返回工作簿解析结果，记录调用参数。"""
    module = types.ModuleType("cater.core.parsing.machine_confirmed")

    def parse_confirmed_machine_sources(sources, candidate, mail_rows, aliases=None):
        calls.append({"sources": list(sources),
                      "candidate_id": candidate["candidate_id"],
                      "mail_rows": list(mail_rows)})
        src = Source(type="workbook", filename=sources[0]["filename"])
        msos = candidate["identity"]["mso"]
        serials = candidate["identity"].get("serial") or [None] * len(msos)
        invoice_rows = [
            MachineRow(mso=m, model="TESTMODEL", loader=s,
                       pin=f"PIN-{m}", engine=f"ENG-{m}", source=src)
            for m, s in zip(msos, serials)
        ]
        groups = [WeightGroup(msos=[m], qty=1, net_weight=Decimal("100"),
                              gross_weight=Decimal("110"), source=src)
                  for m in msos]
        invoice_no = (candidate["identity"].get("invoice_no") or [None])[0]
        return MachineWorkbookResult(
            invoice_rows=invoice_rows, packing_rows=[], weight_groups=groups,
            invoice_no=invoice_no, invoice_source=src,
            layout={"checks": [], "machine_table": [], "weight_table": []},
            issues=[])

    module.parse_confirmed_machine_sources = parse_confirmed_machine_sources
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed", module)
    return module


@pytest.fixture()
def runner(config):
    instance = BatchOfflineRunner(config)
    yield instance
    instance.close()


def _db_snapshot(service, task_id):
    task = service.tasks.require(task_id)
    hold = service.db.fetchone(
        "SELECT * FROM batch_tasks WHERE task_id = ?", (task_id,))
    outbox = service.db.scalar(
        "SELECT COUNT(*) FROM outbox WHERE task_id = ?", (task_id,))
    versions = service.db.scalar(
        "SELECT COUNT(*) FROM task_versions WHERE task_id = ?", (task_id,))
    events = service.db.scalar(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,))
    return {"task": dict(task), "hold": dict(hold), "outbox": outbox,
            "versions": versions, "events": events}


# ---------------------------------------------------------------------------
# 真实链路：ready、隔离输出、正式状态不变
# ---------------------------------------------------------------------------

def test_offline_run_ready_isolated_and_state_unchanged(
        service, materialized, runner, monkeypatch, tmp_path):
    link = _task_for(materialized["tasks"], "A")
    task_id = link["task_id"]
    _prepare_ready(service, task_id)
    calls: list = []
    _install_fake_machine_parser(monkeypatch, calls)
    before = _db_snapshot(service, task_id)
    snapshot_file = Path(link["snapshot_path"])
    snapshot_hash_before = hashlib.sha256(
        snapshot_file.read_bytes()).hexdigest()

    out_dir = tmp_path / "offline-out-a"
    report = runner.run_task(task_id, out_dir, actor="tester")

    assert report["status"] == "ready", report.get("issues")
    assert report["business_identity"] == "MSOA1|INV-A-001"
    assert report["records"] == 1
    assert len(report["artifacts"]) == 2
    for artifact in report["artifacts"]:
        path = Path(artifact["path"])
        assert path.is_file()
        assert path.parent == out_dir
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    # 报告落盘且内容一致
    report_file = out_dir / REPORT_NAME
    assert report_file.is_file()
    persisted = json.loads(report_file.read_text(encoding="utf-8"))
    assert persisted["status"] == "ready"
    assert persisted["snapshot"]["trusted_hash"] == link["snapshot_hash"]
    assert persisted["snapshot"]["verified_hash"] == link["snapshot_hash"]
    assert persisted["references"]["version"] >= 1
    assert persisted["state_unchanged"] is True
    assert persisted["task_state_after"]["execution_hold"] is True
    assert persisted["task_state_after"]["status"] == "RECEIVED"
    assert persisted["task_state_after"]["version"] == 1
    # B 只收到本票快照目录内的资产
    assert len(calls) == 1
    snap_dir = snapshot_file.parent
    for src in calls[0]["sources"]:
        assert Path(src["path"]).parent == snap_dir
    # 正式状态/hold/outbox/版本/事件零变化
    after = _db_snapshot(service, task_id)
    assert after == before
    assert after["hold"]["execution_hold"] == 1
    # 快照文件未被改写
    assert hashlib.sha256(snapshot_file.read_bytes()).hexdigest() == \
        snapshot_hash_before


def test_offline_run_waiting_input_is_honest(
        service, materialized, runner, monkeypatch, tmp_path):
    """缺目的国/机型参考：如实 waiting_input，不伪造 ready。"""
    link = _task_for(materialized["tasks"], "A")
    calls: list = []
    _install_fake_machine_parser(monkeypatch, calls)
    out_dir = tmp_path / "offline-out-wait"
    report = runner.run_task(link["task_id"], out_dir, actor="tester")
    assert report["status"] == "waiting_input"
    codes = {i["code"] for i in report["issues"]}
    assert "MISSING_DESTINATION" in codes
    assert report["artifacts"] == []
    assert not any(p.suffix == ".xlsx" for p in out_dir.iterdir())
    assert (out_dir / REPORT_NAME).is_file()


def test_offline_run_with_real_confirmed_parser(
        service, materialized, runner, tmp_path):
    """真实链路集成：可信快照 → A 建请求 → 核心 confirmed 路径 → 真实 B
    解析器 → reconcile/transform/模板输出。不注入任何假解析器。"""
    pytest.importorskip("cater.core.parsing.machine_confirmed")
    link = _task_for(materialized["tasks"], "A")
    _prepare_ready(service, link["task_id"])
    before = _db_snapshot(service, link["task_id"])

    out_dir = tmp_path / "offline-out-real"
    report = runner.run_task(link["task_id"], out_dir, actor="tester")

    assert report["status"] == "ready", report.get("issues")
    assert report["business_identity"] == "MSOA1|INV-A-001"
    assert report["records"] == 1
    assert len(report["artifacts"]) == 2
    names = sorted(a["filename"] for a in report["artifacts"])
    assert names == ["SEM发运沟通表 上传.xlsx", "企业物料 上传.xlsx"]
    for artifact in report["artifacts"]:
        path = Path(artifact["path"])
        assert path.is_file()
        assert path.parent == out_dir
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    assert report["state_unchanged"] is True
    assert _db_snapshot(service, link["task_id"]) == before


# ---------------------------------------------------------------------------
# 仅序列号正文行：preview → 确认 → A → core/runner 端到端
# ---------------------------------------------------------------------------

def _serial_only_ticket_mail() -> bytes:
    """单票整机：正文业务表有 MSO 列但该票单元格为空（仅序列号可识别）。"""
    fields = {"mso": "", "serial": "S000009", "price": "900",
              "dealer": "DCX9", "dealer_order": "DOX9", "order": "ORDX9"}
    header = "".join(f"<td>{label}</td>" for label, _ in _COLUMNS)
    cells = "".join(f"<td>{fields[field] if field else 'TESTMODEL'}</td>"
                    for _, field in _COLUMNS)
    html = ("<html><body><p>仅序列号一票。</p><table>"
            f"<tr>{header}</tr><tr>{cells}</tr></table></body></html>")
    attachments = (("x-invoice.xlsx", machine_workbook_bytes(
        "INV-X-009", "MSOX9", "S000009", 900)),)
    return make_mail(sender="tester@example.com", subject="仅序列号整机发票",
                     text="见附件。", html=html, attachments=attachments)


def _materialize_serial_only(service):
    """导入并确认仅序列号一票，返回持 hold 的子任务。"""
    result = service.batches.import_eml_bytes(
        _serial_only_ticket_mail(), filename="serial-only.eml", actor="tester")
    batch = result["batch"]
    plan = result["plan"]
    keys = [c["candidate_id"] for c in result["preview"]["candidates"]]
    assert len(keys) == 1
    outcome = service.batches.confirm_and_create(
        batch["id"], plan_id=plan["id"],
        fingerprint=plan["source_fingerprint"],
        selected=keys, exclusions={}, actor="confirmer")
    return outcome["tasks"][0]


def test_offline_run_serial_only_end_to_end(service, runner, tmp_path):
    """真实链路：仅序列号票经 preview→确认→A→核心 confirmed→真实 B，
    正文行由唯一机器配对补认 MSO 后正常出单；可信快照保持冻结不改。"""
    pytest.importorskip("cater.core.parsing.machine_confirmed")
    link = _materialize_serial_only(service)
    task_id = link["task_id"]
    _prepare_ready(service, task_id)
    before = _db_snapshot(service, task_id)

    out_dir = tmp_path / "offline-out-serial"
    report = runner.run_task(task_id, out_dir, actor="tester")

    assert report["status"] == "ready", report.get("issues")
    assert report["business_identity"] == "MSOX9|INV-X-009"
    assert report["records"] == 1
    assert len(report["artifacts"]) == 2
    assert report["state_unchanged"] is True
    assert _db_snapshot(service, task_id) == before
    # 防拼装基线保持原始冻结字段：补认绝不改写可信快照
    snap = json.loads(Path(link["snapshot_path"]).read_text(encoding="utf-8"))
    frozen_rows = snap["mail_rows"]
    assert len(frozen_rows) == 1
    assert not frozen_rows[0]["fields"].get("mso")
    assert frozen_rows[0]["fields"]["serial_number"] == "S000009"
    assert frozen_rows[0]["fields"]["total_sales_price"] == "900"


def test_offline_run_serial_only_ambiguous_pairing_blocks(
        service, runner, monkeypatch, tmp_path):
    """同一序列号对应多台机器（歧义）：离线执行如实阻断，不默认取第一台，
    不产出任何业务文件，正式状态不变。"""
    link = _materialize_serial_only(service)
    task_id = link["task_id"]
    _prepare_ready(service, task_id)
    before = _db_snapshot(service, task_id)

    module = types.ModuleType("cater.core.parsing.machine_confirmed")

    def parse_confirmed_machine_sources(sources, candidate, mail_rows,
                                        aliases=None):
        src = Source(type="workbook", filename=sources[0]["filename"])
        rows = [
            MachineRow(mso="MSOX9", model="TESTMODEL", loader="S000009",
                       pin="P1", engine="E1", source=src),
            MachineRow(mso="MSOX8", model="TESTMODEL", loader="S000009",
                       pin="P2", engine="E2", source=src),
        ]
        return MachineWorkbookResult(
            invoice_rows=rows, packing_rows=[], weight_groups=[],
            invoice_no="INV-X-009", invoice_source=src,
            layout={"checks": [], "machine_table": [], "weight_table": []},
            issues=[])

    module.parse_confirmed_machine_sources = parse_confirmed_machine_sources
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        module)

    out_dir = tmp_path / "offline-out-serial-ambiguous"
    report = runner.run_task(task_id, out_dir, actor="tester")

    assert report["status"] == "waiting_input", report
    codes = {i["code"] for i in report["issues"]}
    assert "MAIL_ROW_SERIAL_AMBIGUOUS" in codes
    assert report["artifacts"] == []
    assert sorted(p.name for p in out_dir.iterdir()) == [REPORT_NAME]
    assert report["state_unchanged"] is True
    assert _db_snapshot(service, task_id) == before


# ---------------------------------------------------------------------------
# 快照可信边界：篡改/复核不通过必须拦截
# ---------------------------------------------------------------------------

def test_tampered_snapshot_is_refused(service, materialized, runner, tmp_path):
    link = _task_for(materialized["tasks"], "A")
    snapshot_file = Path(link["snapshot_path"])
    payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    payload["candidate"]["identity"]["mso"] = ["HACKED1"]
    snapshot_file.write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")
    out_dir = tmp_path / "offline-out-tamper"
    with pytest.raises(ValidationError, match="快照"):
        runner.run_task(link["task_id"], out_dir)
    assert not out_dir.exists()  # 拒绝发生在创建任何输出之前
    hold = service.db.fetchone(
        "SELECT execution_hold FROM batch_tasks WHERE task_id = ?",
        (link["task_id"],))
    assert hold["execution_hold"] == 1


def test_verifier_not_ok_is_refused(service, materialized, runner,
                                    monkeypatch, tmp_path):
    link = _task_for(materialized["tasks"], "A")
    monkeypatch.setattr(
        runner.batches, "verify_batch_task_snapshot",
        lambda task_id: {"task_id": task_id, "snapshot_path": "/tmp/x",
                         "trusted_hash": "a", "verified_hash": "b", "ok": False})
    out_dir = tmp_path / "offline-out-notok"
    with pytest.raises(ValidationError, match="可信哈希"):
        runner.run_task(link["task_id"], out_dir)
    assert not out_dir.exists()


# ---------------------------------------------------------------------------
# TOCTOU 可信绑定：verify 之后快照被替换必须在创建输出前拒绝
# ---------------------------------------------------------------------------

def _wrap_verify_with_swap(runner, monkeypatch, mutate):
    """包装 verify：先真实复核（返回 ok），随后按 mutate 替换快照文件，
    确定性地模拟 verify→read 之间的快照替换（TOCTOU 注入点）。"""
    original = runner.batches.verify_batch_task_snapshot

    def wrapped(task_id):
        result = original(task_id)
        snapshot_file = Path(result["snapshot_path"]) / "snapshot.json"
        payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
        mutate(payload)
        snapshot_file.write_text(json.dumps(payload, ensure_ascii=False),
                                 encoding="utf-8")
        return result

    monkeypatch.setattr(runner.batches, "verify_batch_task_snapshot", wrapped)


def _spy_runner(config, calls: list) -> BatchOfflineRunner:
    """核心调用探针：payload 未绑定可信哈希时核心绝不应被调用。"""
    def spy(request):
        calls.append(request)
        raise AssertionError("核心绝不应被调用：payload 未通过可信哈希绑定")
    return BatchOfflineRunner(config, process_job=spy)


def test_swapped_snapshot_payload_after_verify_is_refused(
        service, materialized, config, monkeypatch, tmp_path):
    """verify 返回 ok 后快照被替换（金额、候选 mail_row_refs 与 facts 同步
    篡改）：runner 必须以 DB 持久化 trusted_hash 绑定实际消费的 payload，
    在创建输出目录前拒绝；核心零调用、零产物、hold/正式状态不变。"""
    link = _task_for(materialized["tasks"], "A")
    _prepare_ready(service, link["task_id"])
    before = _db_snapshot(service, link["task_id"])

    def tamper(payload):
        for row in payload["mail_rows"]:
            row["fields"]["total_sales_price"] = "9999.0000"
        for row in payload["candidate"].get("mail_row_refs", []):
            row["fields"]["total_sales_price"] = "9999.0000"
        payload["candidate"]["facts"]["amount"]["value"] = "9999.0000"

    calls: list = []
    runner = _spy_runner(config, calls)
    try:
        _wrap_verify_with_swap(runner, monkeypatch, tamper)
        out_dir = tmp_path / "offline-out-toctou"
        with pytest.raises(ValidationError, match="可信哈希"):
            runner.run_task(link["task_id"], out_dir, actor="tester")
    finally:
        runner.close()
    assert calls == []                    # 核心零调用
    assert not out_dir.exists()           # 未创建任何输出目录
    hold = service.db.fetchone(
        "SELECT execution_hold FROM batch_tasks WHERE task_id = ?",
        (link["task_id"],))
    assert hold["execution_hold"] == 1    # hold 保持
    assert _db_snapshot(service, link["task_id"]) == before


def test_swapped_candidate_identity_after_verify_is_refused(
        service, materialized, config, monkeypatch, tmp_path):
    """verify 后候选身份被替换（MSO/序列号）：同样被可信哈希绑定拒绝。"""
    link = _task_for(materialized["tasks"], "A")
    _prepare_ready(service, link["task_id"])

    def tamper(payload):
        payload["candidate"]["identity"]["mso"] = ["HACKED1"]
        payload["candidate"]["identity"]["serial"] = ["HACKED9"]

    calls: list = []
    runner = _spy_runner(config, calls)
    try:
        _wrap_verify_with_swap(runner, monkeypatch, tamper)
        out_dir = tmp_path / "offline-out-toctou-id"
        with pytest.raises(ValidationError, match="可信哈希"):
            runner.run_task(link["task_id"], out_dir, actor="tester")
    finally:
        runner.close()
    assert calls == []
    assert not out_dir.exists()


def test_timestamp_only_change_follows_same_canonical_scope(
        service, materialized, runner, monkeypatch, tmp_path):
    """仅 created_at 时间戳元数据变化：绑定使用与 verify 完全相同的规范化
    口径（忽略时间戳等非业务元数据），不得误拒，执行照常完成。"""
    link = _task_for(materialized["tasks"], "A")
    _prepare_ready(service, link["task_id"])
    calls: list = []
    _install_fake_machine_parser(monkeypatch, calls)

    def retime(payload):
        payload["created_at"] = "2001-01-01T00:00:00+00:00"

    _wrap_verify_with_swap(runner, monkeypatch, retime)
    out_dir = tmp_path / "offline-out-ts"
    report = runner.run_task(link["task_id"], out_dir, actor="tester")

    assert report["status"] == "ready", report.get("issues")
    snap = report["snapshot"]
    assert snap["payload_hash"] == snap["trusted_hash"] == \
        snap["verified_hash"]


# ---------------------------------------------------------------------------
# 数据库边界：绝不初始化新库、绝不补种/改写参考数据
# ---------------------------------------------------------------------------

def test_runner_refuses_missing_database(tmp_path):
    """数据库文件不存在：拒绝构造（ConfigurationError），绝不初始化新库。"""
    cfg = make_config(tmp_path)
    with pytest.raises(ConfigurationError, match="绝不初始化新库"):
        BatchOfflineRunner(cfg)
    assert not Path(cfg.db_path).exists()


def test_runner_does_not_seed_or_modify_reference_data(tmp_path):
    """既有但未补种的库：runner 构造不写任何参考数据（countries 保持为空、
    无 reference_seeded 标记），参考库按现状读取。"""
    cfg = make_config(tmp_path)
    seed_db = Database(cfg.db_path)  # 仅迁移建表，不做任何补种
    seed_db.close()

    runner = BatchOfflineRunner(cfg)
    try:
        count = runner.db.scalar("SELECT COUNT(*) FROM countries")
        seeded = runner.db.fetchone(
            "SELECT value FROM runtime_meta WHERE key = 'reference_seeded'")
    finally:
        runner.close()

    assert count == 0
    assert seeded is None


def test_task_must_belong_to_confirmed_batch(service, runner, tmp_path):
    with pytest.raises(KeyError):
        runner.run_task("task-不存在", tmp_path / "o1")
    legacy = service.tasks.create(owner="tester@example.com",
                                  owner_name="Tester", kind="machine",
                                  origin_mail_id=None)
    with pytest.raises(KeyError):
        runner.run_task(str(legacy["id"]), tmp_path / "o2")


# ---------------------------------------------------------------------------
# 父邮件与别票材料绝不读取；两票互不串扰
# ---------------------------------------------------------------------------

def test_run_never_reads_parent_mail_or_other_ticket(
        service, materialized, runner, monkeypatch, tmp_path):
    link_a = _task_for(materialized["tasks"], "A")
    link_b = _task_for(materialized["tasks"], "B")
    _prepare_ready(service, link_a["task_id"])
    calls: list = []
    _install_fake_machine_parser(monkeypatch, calls)

    # 父邮件原件与 B 票快照资产全部破坏：任何误读都会立即暴露
    for asset in service.batches.assets(materialized["batch_id"]):
        if asset["role"] == "raw_mail":
            Path(asset["path"]).write_bytes(b"corrupted-parent-mail")
    b_snap = Path(link_b["snapshot_path"]).parent
    for file in b_snap.iterdir():
        if file.suffix == ".xlsx":
            file.write_bytes(b"corrupted-other-ticket")

    out_dir = tmp_path / "offline-out-a2"
    report = runner.run_task(link_a["task_id"], out_dir, actor="tester")
    assert report["status"] == "ready", report.get("issues")
    assert "MSOB1" not in str(report["business_identity"])
    for src in calls[0]["sources"]:
        assert Path(src["path"]).parent == Path(link_a["snapshot_path"]).parent


def test_two_tickets_do_not_cross(service, materialized, runner,
                                  monkeypatch, tmp_path):
    links = {letter: _task_for(materialized["tasks"], letter)
             for letter in ("A", "B")}
    for letter in ("A", "B"):
        _prepare_ready(service, links[letter]["task_id"])
    calls: list = []
    _install_fake_machine_parser(monkeypatch, calls)
    reports = {}
    for letter in ("A", "B"):
        reports[letter] = runner.run_task(
            links[letter]["task_id"], tmp_path / f"out-{letter}", actor="tester")
    assert reports["A"]["status"] == "ready", reports["A"].get("issues")
    assert reports["B"]["status"] == "ready", reports["B"].get("issues")
    assert reports["A"]["business_identity"] == "MSOA1|INV-A-001"
    assert reports["B"]["business_identity"] == "MSOB1|INV-B-001"
    assert len(calls) == 2
    for call in calls:
        dirs = {Path(s["path"]).parent for s in call["sources"]}
        assert len(dirs) == 1  # 每次调用只见一个快照目录（本票）
    contents_a = {p.name for p in (tmp_path / "out-A").iterdir()}
    contents_b = {p.name for p in (tmp_path / "out-B").iterdir()}
    assert REPORT_NAME in contents_a and REPORT_NAME in contents_b


# ---------------------------------------------------------------------------
# 输出目录：独占创建、拒绝覆盖、并发单赢、失败如实
# ---------------------------------------------------------------------------

def test_existing_output_dir_is_never_overwritten(
        service, materialized, runner, tmp_path):
    link = _task_for(materialized["tasks"], "A")
    out_dir = tmp_path / "occupied"
    out_dir.mkdir()
    marker = out_dir / "keep.txt"
    marker.write_text("precious", encoding="utf-8")
    with pytest.raises(ValidationError, match="拒绝覆盖"):
        runner.run_task(link["task_id"], out_dir)
    assert marker.read_text(encoding="utf-8") == "precious"
    assert not (out_dir / REPORT_NAME).exists()


def test_output_dir_must_not_touch_snapshot_or_production_dirs(
        service, materialized, runner, config, tmp_path):
    link = _task_for(materialized["tasks"], "A")
    snap_dir = Path(link["snapshot_path"]).parent
    snap_bytes = {p.name: p.read_bytes() for p in snap_dir.iterdir()}
    with pytest.raises(ValidationError, match="快照"):
        runner.run_task(link["task_id"], snap_dir / "nested-out")
    for name, data in snap_bytes.items():
        assert (snap_dir / name).read_bytes() == data
    with pytest.raises(ValidationError, match="受保护"):
        runner.run_task(link["task_id"], config.tasks_dir / "hack-out")
    assert not (config.tasks_dir / "hack-out").exists()


def test_concurrent_same_output_dir_single_winner(
        service, materialized, config, monkeypatch, tmp_path):
    link = _task_for(materialized["tasks"], "A")
    _prepare_ready(service, link["task_id"])
    calls: list = []
    _install_fake_machine_parser(monkeypatch, calls)
    out_dir = tmp_path / "raced"

    results, errors = [], []

    def run():
        instance = BatchOfflineRunner(config)
        try:
            results.append(instance.run_task(link["task_id"], out_dir))
        except Exception as exc:  # noqa: BLE001 - 收集后断言类型
            errors.append(exc)
        finally:
            instance.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: run(), range(2)))
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ValidationError)
    assert "并发" in str(errors[0]) or "已存在" in str(errors[0])
    assert results[0]["status"] == "ready"
    assert (out_dir / REPORT_NAME).is_file()


def test_failed_run_writes_honest_failed_report(
        service, materialized, config, tmp_path):
    link = _task_for(materialized["tasks"], "A")

    def boom(request):
        raise RuntimeError("synthetic core crash")

    runner = BatchOfflineRunner(config, process_job=boom)
    try:
        before = _db_snapshot(service, link["task_id"])
        out_dir = tmp_path / "offline-out-fail"
        report = runner.run_task(link["task_id"], out_dir, actor="tester")
    finally:
        runner.close()
    assert report["status"] == "failed"
    assert "RuntimeError" in report["error"]
    assert report["artifacts"] == []
    persisted = json.loads((out_dir / REPORT_NAME).read_text(encoding="utf-8"))
    assert persisted["status"] == "failed"
    assert _db_snapshot(service, link["task_id"]) == before


def test_output_dir_exclusive_creation_never_deletes(
        service, materialized, config, tmp_path):
    """失败路径也不删除/改写既有目录：用一个已被占用的目标验证。"""
    link = _task_for(materialized["tasks"], "A")
    out_dir = tmp_path / "preexisting"
    out_dir.mkdir()
    stale = out_dir / "stale-report.json"
    stale.write_text("{}", encoding="utf-8")
    runner = BatchOfflineRunner(config, process_job=lambda r: (_ for _ in ()).throw(
        AssertionError("不应执行到这里")))
    try:
        with pytest.raises(ValidationError):
            runner.run_task(link["task_id"], out_dir)
    finally:
        runner.close()
    assert stale.read_text(encoding="utf-8") == "{}"
    assert sorted(p.name for p in out_dir.iterdir()) == ["stale-report.json"]


# ---------------------------------------------------------------------------
# CLI 入口与最小离线配置
# ---------------------------------------------------------------------------

def test_cli_run_batch_offline_ready(
        service, materialized, config, monkeypatch, tmp_path, capsys):
    link = _task_for(materialized["tasks"], "A")
    _prepare_ready(service, link["task_id"])
    calls: list = []
    _install_fake_machine_parser(monkeypatch, calls)
    out_dir = tmp_path / "cli-out"
    code = cli_main(["run-batch-offline", link["task_id"],
                     "--output-dir", str(out_dir),
                     "--runtime", str(config.paths.runtime_dir)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "ready" in captured
    assert "execution_hold 保持: True" in captured
    assert (out_dir / REPORT_NAME).is_file()


def test_cli_run_batch_offline_refusals(
        service, materialized, config, tmp_path, capsys):
    link = _task_for(materialized["tasks"], "A")
    code = cli_main(["run-batch-offline", "task-不存在",
                     "--output-dir", str(tmp_path / "x1"),
                     "--runtime", str(config.paths.runtime_dir)])
    assert code == 1
    assert "不属于已确认批次" in capsys.readouterr().err
    occupied = tmp_path / "x2"
    occupied.mkdir()
    code = cli_main(["run-batch-offline", link["task_id"],
                     "--output-dir", str(occupied),
                     "--runtime", str(config.paths.runtime_dir)])
    assert code == 1
    assert sorted(p.name for p in occupied.iterdir()) == []


def test_load_offline_config_ignores_env_and_secrets(tmp_path, monkeypatch):
    """最小离线配置：不读 .env / OS 环境；发送相关保持出厂离线默认。"""
    (tmp_path / ".env").write_text(
        "CATER_MAIL_ACCOUNT=prod@example.com\n"
        "CATER_IMAP_PASSWORD=supersecret\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CATER_MAIL_ACCOUNT", "prod@example.com")
    monkeypatch.setenv("CATER_IMAP_PASSWORD", "supersecret")
    monkeypatch.setenv("CATER_RUNTIME_DIR", str(tmp_path / "env-runtime"))
    config = load_offline_config(runtime_dir=tmp_path / "rt")
    assert config.paths.runtime_dir == tmp_path / "rt"
    assert config.mail.account == ""
    assert config.mail.imap_host == ""
    assert config.sending.enabled is False
    assert config.sending.dry_run is True
    assert config.allowlist.mode == "test"
    assert not (tmp_path / "env-runtime").exists()


def test_load_offline_config_reads_toml_runtime_and_aliases(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[runtime]\ndir = "custom-runtime"\n'
        '[core.header_aliases]\nmso = ["MSO", "MSO#"]\n', encoding="utf-8")
    config = load_offline_config(config_file)
    assert config.paths.runtime_dir.name == "custom-runtime"
    assert config.core.header_aliases == {"mso": ["MSO", "MSO#"]}
