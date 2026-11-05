"""灵活输入在应用层的落地验收：.xls 附件全链、离线 .xls 提交（真实核心）、
布局确认流（首次提示 → 确认基线 → 结构变化列出）、交付正文布局段落。

合成样例说明：flexible_machine.xls 为去敏合成夹具（FAKE MSO / SYN 序列号），
配套业务表同为合成数据；不包含真实商业资料，也不代表真实新邮件已通过。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cater.app import state
from cater.app.application import AppService
from cater.app.delivery import DeliveryService
from cater.app.engine import CoreProcessEngine
from cater.app.mail.simulator import SimulatorMailGateway

from helpers import FakeEngine, make_config, make_mail, raw_message

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEXIBLE_MACHINE_XLS = (
    REPO_ROOT / "tests" / "core" / "fixtures" / "flexible_machine.xls"
)

pytestmark = pytest.mark.skipif(
    not FLEXIBLE_MACHINE_XLS.is_file(),
    reason="synthetic flexible machine xls fixture not available",
)

# 合成业务表（纯文本 Tab 分列，显式合成，不代表真实新邮件）
SYNTHETIC_MAIL_TABLE = (
    "以下为合成业务表（fixtures only）\n"
    "Dealer Code\tDealer Order\tCust. Purch. Order (MSO#)\tOrder\t"
    "Material\tSerial Number\tTotal Sales Price\n"
    "SYN01\tSYN-ORD-1\tFAKE001\t06200000101\tSEMTEST\tSYN000001A\t50000.00\n"
    "SYN02\tSYN-ORD-2\tFAKE002\t06200000201\tSEMTEST\tSYN000002B\t50000.00\n"
)

PARTS_LAYOUT = {
    "kind": "parts",
    "fingerprint": "fp-parts-0001",
    "sheets": ["CI-For Dealer (2)", "Packing List"],
    "parts_invoice": {
        "sheet": "CI-For Dealer (2)",
        "header_row": 17,
        "subheader_row": 18,
        "columns": {"part_code": "B", "qty": "F", "unit_price": "H"},
        "supplementary": {"hs_code": "L", "product_name": "M", "spec": "N"},
        "data_first_row": 19,
        "data_last_row": 42,
    },
    "packing_total": {
        "sheet": "Packing List",
        "total_row": 79,
        "columns": {"packages": "H", "gross_weight": "K"},
    },
    "checks": [
        {"rule": "PARTS_WEIGHT_QTY_X_UNIT", "result": "pass",
         "detail": "净重=数量×单件重（24/24 行）"},
        {"rule": "PARTS_DETAIL_NET_TOTAL", "result": "pass",
         "detail": "明细净重合计与 TOTAL 行一致"},
    ],
}

MACHINE_LAYOUT = {
    "kind": "machine",
    "fingerprint": "fp-machine-0001",
    "sheets": ["CI", "PL"],
    "machine_table": [
        {"sheet": "CI", "header_row": 56, "columns": {"model": "B", "mso": "O"}},
    ],
    "weight_table": [
        {"sheet": "PL", "header_row": 28, "subheader_row": 29,
         "columns": {"qty": "M", "net": "S"}},
    ],
    "checks": [
        {"rule": "MACHINE_WEIGHT_GROUP_QTY", "result": "pass",
         "detail": "CMOPS 组数量=2 与明细 MSO 数一致"},
    ],
}


def _submit(service, source: Path, *, destination: str = "法国") -> dict:
    return service.submit_offline_sources(
        sender="tester@example.com",
        destination=destination,
        source_paths=[source],
    )


def _delivery_body(service, task_id: str) -> str:
    rows = [
        row
        for row in service.outbox.list_recent()
        if row["purpose"] == "delivery" and row["task_id"] == task_id
    ]
    assert rows, "expected a delivery outbox row"
    return str(rows[-1]["body"])


# ---------------------------------------------------------------------------
# .xls 附件进入口（识别、保存、全链不拒绝）
# ---------------------------------------------------------------------------

def test_xls_attachment_is_accepted_and_saved(service) -> None:
    result = service.ingest(
        raw_message(
            make_mail(
                text="目的国：法国",
                attachments=(("machine.xls", FLEXIBLE_MACHINE_XLS.read_bytes()),),
            )
        )
    )
    task = service.tasks.require(result.task_id)
    assert task["status"] == state.READY_TO_SEND
    inputs = service.tasks.inputs(result.task_id, 1)
    assert [item["filename"] for item in inputs] == ["machine.xls"]
    stored = Path(inputs[0]["path"])
    assert stored.is_file()
    assert stored.read_bytes() == FLEXIBLE_MACHINE_XLS.read_bytes()
    codes = {issue["code"] for issue in service.tasks.issues(result.task_id)}
    assert "UNSUPPORTED_ATTACHMENT" not in codes


def test_xls_attachment_recognized_by_mime_type(service) -> None:
    """扩展名伪装时按 xls MIME 识别。"""
    result = service.ingest(
        raw_message(
            make_mail(
                text="目的国：法国",
                attachments=(
                    ("data.bin", FLEXIBLE_MACHINE_XLS.read_bytes(),
                     "application/vnd.ms-excel"),
                ),
            )
        )
    )
    task = service.tasks.require(result.task_id)
    assert task["status"] == state.READY_TO_SEND
    inputs = service.tasks.inputs(result.task_id, 1)
    assert inputs[0]["mime_type"] in (
        "application/vnd.ms-excel",
        "application/x-xls",
        "application/msexcel",
    )


def test_non_workbook_attachment_still_rejected(service) -> None:
    result = service.ingest(
        raw_message(make_mail(attachments=(("photo.jpg", b"jpeg"),)))
    )
    task = service.tasks.require(result.task_id)
    assert task["status"] == state.WAITING_INPUT
    codes = {issue["code"] for issue in service.tasks.issues(result.task_id)}
    assert "UNSUPPORTED_ATTACHMENT" in codes


# ---------------------------------------------------------------------------
# 离线 .xls 提交 + 真实核心端到端（合成样例）
# ---------------------------------------------------------------------------

def test_offline_xls_real_core_end_to_end(tmp_path: Path) -> None:
    """合成 xls + 合成业务表经真实核心：ready、布局落库、首次使用提示。"""
    config = make_config(tmp_path)
    service = AppService(
        config, engine=CoreProcessEngine(), gateway=SimulatorMailGateway()
    )
    try:
        service.references.create_machine(
            "SEMTEST", "",
            english_name="SEMTEST WHEEL LOADER",
            hs_code="8429510000",
            product_name="装载机",
            declaration="1|0|合成申报要素|无|无|无|无|无|无",
            special_rule_confirmed=True,
            actor="test",
        )
        task = service.submit_offline_sources(
            sender="tester@example.com",
            kind="machine",
            destination="巴西",
            source_paths=[FLEXIBLE_MACHINE_XLS],
            body=SYNTHETIC_MAIL_TABLE,
        )
        assert task["status"] == state.READY_TO_SEND, task.get("last_error")
        detail = service.task_detail(task["id"])
        layout = detail["layout"]
        assert layout and layout["kind"] == "machine"
        assert layout["fingerprint"]
        assert layout["sheets"] == ["CI", "PL"]
        # 历史表（Sheet2/BL）被排除：采用工作表只有 CI/PL
        assert detail["layout_confirmed"] is False
        # 仅记录指纹/工作表/规则ID，不记录正文
        seen = [
            event
            for event in detail["events"]
            if event["code"] == "layout_seen"
        ]
        assert len(seen) == 1
        payload = seen[0]["detail"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["confirmed_layout"] is False
        assert set(payload["checks"]) <= {
            "MACHINE_WEIGHT_GROUP_QTY",
            "MACHINE_PACKING_TOTAL_ROW",
            "MACHINE_CI_TEXT_VS_PL_NET",
        }
        body = _delivery_body(service, task["id"])
        assert "采用工作表：CI、PL" in body
        assert "已执行核对：" in body and "通过" in body
        assert "人工重点核对建议" in body  # 首次使用（未确认）必须提示
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 布局确认流：首次提示 → 显式确认 → 不再提示；结构变化与实际修正分列
# ---------------------------------------------------------------------------

def test_confirm_layout_then_no_rewarning(service, offline_source: Path) -> None:
    service.engine.layout = dict(PARTS_LAYOUT)
    task = _submit(service, offline_source)
    assert task["status"] == state.READY_TO_SEND
    body = _delivery_body(service, task["id"])
    assert "采用工作表：CI-For Dealer (2)、Packing List" in body
    assert "PARTS_WEIGHT_QTY_X_UNIT" not in body  # 正文面向用户，不写规则ID
    assert "人工重点核对建议" in body

    result = service.confirm_layout(task["id"], actor="test-admin")
    assert result["fingerprint"] == PARTS_LAYOUT["fingerprint"]
    assert service.task_detail(task["id"])["layout_confirmed"] is True

    reprocessed = service.reprocess_task(task["id"], actor="test-admin")
    assert reprocessed["version"] == 2
    body2 = _delivery_body(service, task["id"])
    assert "人工重点核对建议" not in body2  # 已确认布局不重复告警
    assert "结构变化" not in body2  # 布局未变


def test_structure_changes_listed_after_confirmation(
    service, offline_source: Path
) -> None:
    service.engine.layout = dict(PARTS_LAYOUT)
    task = _submit(service, offline_source)
    service.confirm_layout(task["id"], actor="test-admin")

    changed = json.loads(json.dumps(PARTS_LAYOUT))
    changed["fingerprint"] = "fp-parts-0002"
    changed["parts_invoice"]["header_row"] = 21
    changed["parts_invoice"]["columns"]["qty"] = "G"
    service.engine.layout = changed
    service.reprocess_task(task["id"], actor="test-admin")

    body = _delivery_body(service, task["id"])
    # 新指纹=新布局：既是首次使用（保留重点提示），又相对已确认基线列出结构变化
    assert "人工重点核对建议" in body
    assert "结构变化（相对已确认布局）：" in body
    assert "表头行" in body and "17→21" in body
    assert "数量" in body and "F→G" in body


def test_corrections_listed_separately(service, offline_source: Path) -> None:
    service.engine.layout = dict(PARTS_LAYOUT)
    task = service.submit_offline_sources(
        sender="tester@example.com",
        destination="法国",
        source_paths=[offline_source],
        body="",
    )
    # 追加一条补充修正后再处理一版
    service.tasks.add_override(
        task["id"], record_key="P1", field="qty", value="3",
        source_message_id="<supp@example.com>",
    )
    service.reprocess_task(task["id"], actor="test-admin")
    body = _delivery_body(service, task["id"])
    assert "实际修正（按您回复的修正项执行）：" in body
    assert "P1.qty=3" in body
    assert "结构变化" not in body  # 两类信息分列，互不替代


def test_layout_lines_never_mark_unexecuted_checks_as_passed() -> None:
    lines = DeliveryService._layout_lines({
        "sheets": ["CI"],
        "checks": [
            {"rule": "A", "result": "pass", "detail": "一致"},
            {"rule": "B", "result": "fail", "detail": "第3行不符"},
            {"rule": "C", "detail": "未执行的核对不得称通过"},
        ],
    })
    text = "\n".join(lines)
    assert "- 通过：一致" in text
    assert "- 未通过：第3行不符" in text
    assert "未执行的核对不得称通过" in text
    assert "- 通过：未执行的核对不得称通过" not in text
    # 无布局信息时保持普通成功正文简短
    assert DeliveryService._layout_lines(None) == []


# ---------------------------------------------------------------------------
# CLI / Web 确认入口
# ---------------------------------------------------------------------------

def test_cli_confirm_layout(monkeypatch, capsys) -> None:
    import cater.app.cli as cli

    calls: list[dict] = []

    class _Stub:
        def confirm_layout(self, task_id, *, actor, version=None):
            calls.append({"task_id": task_id, "actor": actor, "version": version})
            return {"kind": "parts", "fingerprint": "fp-x",
                    "confirmed_at": "t", "version": version or 1}

    import contextlib

    monkeypatch.setattr(
        cli, "_service", lambda *_args, **_kwargs: contextlib.nullcontext(_Stub())
    )
    args = type("Args", (), {"task_id": "task_demo", "version": 2})()
    assert cli.cmd_confirm_layout(args) == 0
    assert calls == [{"task_id": "task_demo", "actor": "cli", "version": 2}]
    out = capsys.readouterr().out
    assert "parts layout fp-x confirmed" in out


def test_web_confirm_layout_endpoint(service, offline_source: Path) -> None:
    from fastapi.testclient import TestClient

    from cater.app.web.app import create_app
    from cater.app.web.security import COOKIE_NAME, CSRF_FIELD

    service.engine.layout = dict(MACHINE_LAYOUT)
    task = _submit(service, offline_source)
    base_url = "http://127.0.0.1:8765"
    with TestClient(create_app(service), base_url=base_url) as client:
        denied = client.post(
            f"/tasks/{task['id']}/confirm-layout",
            data={},
            follow_redirects=False,
        )
        assert denied.status_code == 403
        cookie = client.cookies.get(COOKIE_NAME)
        signer = client.app.state.session_signer
        token = signer.load(cookie)["csrf"]
        accepted = client.post(
            f"/tasks/{task['id']}/confirm-layout",
            data={CSRF_FIELD: token},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
    assert service.task_detail(task["id"])["layout_confirmed"] is True
