"""补件业务表（table-only 回复）全链回归：真实 intake + 真实核心。

场景（全部合成数据：FAKE MSO / SYN 序列号 / SYN 机型）：
- 原始邮件业务表不完整（缺序列号与 Total Sales Price 列）→ 挂起并追问；
- 原任务发起人直接回复完整业务表（HTML 或以 Tab 分列的纯文本，无需任何
  修正指令）→ 新版本按 MSO 合并填充 → READY；
- 版本请求包含 supplement_mail（冻结来源，重开服务后不变）；
- 真实值冲突 → FIELD_CONFLICT 暂停并保留双来源；
- quoted 历史里的旧表不计入（不视为实质性补充）；
- 非发起人回复被过滤；多版不一致业务表暂停。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cater.app import state
from cater.app.application import AppService
from cater.app.engine import CoreProcessEngine
from cater.app.mail.simulator import SimulatorMailGateway

from helpers import build_machine_xlsx, make_config, make_mail, raw_message

FULL_HEADERS = ["Regional", "Order", "Dealer Order", "Cust. Purch. Order",
                "Dealer", "Ship-to Party", "Dealer Name", "Material",
                "SO Serial Number", "Total Sales Price"]
FULL_ROWS = [
    ["AMS", "0620192963", "9400015379", "FAKE001", "UM16", "UM16",
     "SOTREQ S.A.", "SEMTEST", "SYN000001A", "50000.00"],
    ["AMS", "0620192964", "9400015381", "FAKE002", "UM16", "UM16",
     "SOTREQ S.A.", "SEMTEST", "SYN000002B", "50000.00"],
]

# 原始邮件的不完整表：缺 Serial Number 与 Total Sales Price 列
PARTIAL_HEADERS = ["Regional", "Order", "Dealer Order", "Cust. Purch. Order",
                   "Dealer", "Ship-to Party", "Dealer Name", "Material"]
PARTIAL_ROWS = [row[:8] for row in FULL_ROWS]


def table_html(headers: list[str], rows: list[list[str]]) -> str:
    parts = ["<table border='1'>",
             "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"]
    for row in rows:
        parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


def text_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["\t".join(headers)]
    lines.extend("\t".join(row) for row in rows)
    return "\n".join(lines)


def make_service(tmp_path: Path) -> AppService:
    config = make_config(tmp_path)
    service = AppService(
        config, engine=CoreProcessEngine(), gateway=SimulatorMailGateway()
    )
    service.references.create_machine(
        "SEMTEST", "",
        english_name="SEMTEST WHEEL LOADER",
        hs_code="8429510000",
        product_name="装载机",
        declaration="1|0|合成申报要素|无|无|无|无|无|无",
        special_rule_confirmed=True,
        actor="test",
    )
    return service


def start_incomplete_task(service: AppService, tmp_path: Path) -> str:
    return start_task_with_origin_text(
        service, tmp_path, "目的国：法国\n" + text_table(PARTIAL_HEADERS, PARTIAL_ROWS))


def start_task_with_origin_text(service: AppService, tmp_path: Path,
                                origin_text: str) -> str:
    workbook = build_machine_xlsx(tmp_path / "machine.xlsx")
    result = service.ingest(
        raw_message(
            make_mail(
                text=origin_text,
                attachments=(("machine.xlsx", workbook.read_bytes()),),
                message_id="<origin@example.com>",
            ),
            uid=1,
        )
    )
    task_id = str(result.task_id)
    task = service.tasks.require(task_id)
    assert task["status"] == state.WAITING_INPUT, task.get("last_error")
    codes = [i["code"] for i in service.tasks.issues(task_id)]
    if "目的国" in origin_text:
        assert codes.count("MISSING_FIELD") == 2, codes
    else:
        assert codes == ["DESTINATION_MISSING"], codes
    return task_id


def reply_table(service: AppService, task_id: str, *, html: str | None,
                text: str, message_id: str, uid: int,
                sender: str = "tester@example.com"):
    reply = make_mail(
        sender=sender,
        subject=f"Re: [CATER {task_id} v1] 整机",
        text=text,
        html=html,
        in_reply_to="<origin@example.com>",
        message_id=message_id,
    )
    return service.ingest(raw_message(reply, uid=uid))


def test_table_only_html_reply_fills_missing_fields_to_ready(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)

        # 追问正文：具体缺失字段 + 粘贴完整业务表指引（非逐条修正指令）
        questions = [
            row for row in service.outbox.list_recent()
            if row["purpose"] == "question" and row["task_id"] == task_id
        ]
        assert questions, "expected the v1 question"
        body = str(questions[-1]["body"])
        # 具体缺失字段（机器销售价格）+ 粘贴完整业务表指引，非逐条修正指令
        assert "机器销售价格" in body
        assert "完整业务表" in body and "MSO 列" in body
        assert "修正：" not in body

        # 发起人只回复完整业务表（HTML），无任何指令行
        second = reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="请查收完整业务表",
            message_id="<reply-table@example.com>", uid=2,
        )
        assert second.supplement is True
        assert second.note is None, second.note
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 2
        assert task["status"] == state.READY_TO_SEND, task.get("last_error")

        # 版本来源邮件已按版本持久化（审计），请求包含冻结的 supplement_mails
        links = service.tasks.version_mails(task_id, 2, "supplement_table")
        assert links and all(str(link["mail_id"]) for link in links)
        request_path = (
            service.config.paths.runtime_dir / "tasks" / task_id / "v2" / "request.json"
        )
        request = json.loads(request_path.read_text(encoding="utf-8"))
        supplements = request.get("supplement_mails") or []
        assert len(supplements) == 1
        supplement = supplements[0]
        assert supplement.get("message_id") == "reply-table@example.com"
        assert "Total Sales Price" in (supplement.get("html") or "")

        # 输出与 provenance 使用补充后的值
        detail = service.task_detail(task_id)
        prices = [
            item for item in detail["provenance"]
            if item["field"] == "total_sales_price"
        ]
        assert {item["record_key"] for item in prices} == {"FAKE001", "FAKE002"}
        assert all(str(item["value"]) == "50000.00" for item in prices)
        assert detail["artifacts"], "ready 必须有产物"
    finally:
        service.close()


def test_frozen_sources_survive_service_restart(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        second = reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="完整业务表见附件正文",
            message_id="<reply-restart@example.com>", uid=2,
        )
        assert second.supplement is True
        assert service.tasks.require(task_id)["status"] == state.READY_TO_SEND
        request_path = (
            service.config.paths.runtime_dir / "tasks" / task_id / "v2" / "request.json"
        )
        frozen = json.loads(request_path.read_text(encoding="utf-8"))
    finally:
        service.close()

    # 同一运行目录重开服务：版本来源与冻结请求不变，任务仍 READY
    service2 = AppService(
        config, engine=CoreProcessEngine(), gateway=SimulatorMailGateway()
    )
    try:
        task = service2.tasks.require(task_id)
        assert task["status"] == state.READY_TO_SEND
        links = service2.tasks.version_mails(task_id, 2, "supplement_table")
        assert links
        frozen2 = json.loads(request_path.read_text(encoding="utf-8"))
        assert frozen2 == frozen
        assert frozen2["supplement_mails"][0]["message_id"] == "reply-restart@example.com"
        result = service2.process_task(task_id, actor="test")
        assert result["status"] == state.READY_TO_SEND, result.get("last_error")
    finally:
        service2.close()


def test_table_only_text_reply_fills_missing_fields(tmp_path: Path) -> None:
    """以 Tab 明确分列的纯文本完整业务表同样视为实质性补充。"""
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        second = reply_table(
            service, task_id,
            html=None,
            text=text_table(FULL_HEADERS, FULL_ROWS),
            message_id="<reply-text@example.com>", uid=2,
        )
        assert second.supplement is True
        task = service.tasks.require(task_id)
        assert task["status"] == state.READY_TO_SEND, task.get("last_error")
    finally:
        service.close()


def test_conflicting_supplement_value_pauses_with_provenance(tmp_path: Path) -> None:
    """补件表与原始表同一 MSO 真实值不同：暂停并保留双来源，不静默覆盖。"""
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        changed = [row[:] for row in FULL_ROWS]
        changed[0][4] = "UM99"       # FAKE001 Dealer 与原始邮件真实值不同
        second = reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, changed),
            text="完整业务表",
            message_id="<reply-conflict@example.com>", uid=2,
        )
        assert second.supplement is True
        task = service.tasks.require(task_id)
        assert task["status"] == state.WAITING_INPUT
        conflicts = [
            i for i in service.tasks.issues(task_id, 2)
            if i["code"] == "FIELD_CONFLICT" and i.get("record_key") == "FAKE001"
        ]
        assert conflicts, service.tasks.issues(task_id, 2)
        conflict = conflicts[-1]
        assert "UM16" in conflict["message"] and "UM99" in conflict["message"]
        sources = json.dumps(conflict["sources"], ensure_ascii=False)
        assert "origin@example.com" in sources
        assert "reply-conflict@example.com" in sources
        # 未冲突的 FAKE002 被填充，任务只因冲突暂停
        detail = service.task_detail(task_id)
        filled = [
            item for item in detail["provenance"]
            if item["field"] == "total_sales_price" and item["record_key"] == "FAKE002"
        ]
        assert filled and str(filled[-1]["value"]) == "50000.00"
    finally:
        service.close()


def test_quoted_history_table_is_not_substantive(tmp_path: Path) -> None:
    """只有引用历史里的旧表（blockquote / > 行）不算实质性补充：
    不产生新版本，任务与数据不变。"""
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        quoted_html = (
            "<blockquote>"
            + table_html(FULL_HEADERS, FULL_ROWS)
            + "</blockquote>"
        )
        second = reply_table(
            service, task_id,
            html=quoted_html,
            text="见下文\n> Regional\tOrder\tDealer Order\tCust. Purch. Order\tDealer\n"
                 "> AMS\t0620192963\t9400015379\tFAKE001\tUM16\n"
                 "On yesterday someone wrote:",
            message_id="<reply-quoted@example.com>", uid=2,
        )
        assert second.supplement is True
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 1, "quoted-only 回复不得产生新版本"
        codes = {i["code"] for i in service.tasks.issues(task_id)}
        assert "UNSTRUCTURED_SUPPLEMENT" in codes
        assert service.tasks.version_mails(task_id, 2, "supplement_table") == []
    finally:
        service.close()


def test_non_owner_table_reply_is_filtered(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        second = reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="完整业务表",
            message_id="<reply-nonowner@example.com>", uid=2,
            sender="someone-else@example.com",
        )
        assert second.filtered is True
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 1
        assert task["status"] == state.WAITING_INPUT
    finally:
        service.close()


def test_supplement_with_multiple_inconsistent_tables_pauses(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        changed = [row[:] for row in FULL_ROWS]
        changed[0][3] = "FAKE009"    # 另一版不一致的表
        html = table_html(FULL_HEADERS, FULL_ROWS) + table_html(FULL_HEADERS, changed)
        second = reply_table(
            service, task_id,
            html=html,
            text="完整业务表",
            message_id="<reply-multi@example.com>", uid=2,
        )
        assert second.supplement is True
        task = service.tasks.require(task_id)
        assert task["status"] == state.WAITING_INPUT
        codes = [i["code"] for i in service.tasks.issues(task_id, 2)]
        assert "MULTIPLE_BUSINESS_TABLES" in codes, codes
    finally:
        service.close()


def test_two_step_table_then_destination_reply_keeps_table_source(tmp_path: Path) -> None:
    """两步补件：先发完整业务表（缺目的国仍暂停），再只回目的国——
    表格来源邮件必须随版本继承，最终 READY（不是再次报缺价格）。"""
    service = make_service(tmp_path)
    try:
        # 原始邮件不带目的国：表格补件后仍缺 destination
        task_id = start_task_with_origin_text(
            service, tmp_path, text_table(PARTIAL_HEADERS, PARTIAL_ROWS))
        second = reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="完整业务表",
            message_id="<reply-step1@example.com>", uid=2,
        )
        assert second.supplement is True
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 2
        assert task["status"] == state.WAITING_INPUT

        # 第二步：只回目的国（无表格、无附件）
        third = reply_table(
            service, task_id,
            html=None,
            text="目的国：印度尼西亚",
            message_id="<reply-step2@example.com>", uid=3,
        )
        assert third.supplement is True
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 3
        assert task["status"] == state.READY_TO_SEND, service.tasks.issues(task_id, 3)

        # 两个版本都保留了表格来源邮件（v2 继承到 v3）
        v2_links = service.tasks.version_mails(task_id, 2, "supplement_table")
        v3_links = service.tasks.version_mails(task_id, 3, "supplement_table")
        assert [str(l["mail_id"]) for l in v2_links] == [
            str(l["mail_id"]) for l in v3_links]
        # 冻结请求携带累积的补件表
        request_path = (
            service.config.paths.runtime_dir / "tasks" / task_id / "v3" / "request.json"
        )
        request = json.loads(request_path.read_text(encoding="utf-8"))
        supplements = request.get("supplement_mails") or []
        assert len(supplements) == 1
        assert supplements[0]["message_id"] == "reply-step1@example.com"
    finally:
        service.close()


def test_two_step_table_then_attachment_reply_keeps_table_source(tmp_path: Path) -> None:
    """两步补件：先表格，后替换工作簿附件——表格来源仍随版本继承。"""
    service = make_service(tmp_path)
    try:
        task_id = start_task_with_origin_text(
            service, tmp_path, text_table(PARTIAL_HEADERS, PARTIAL_ROWS))
        second = reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="完整业务表",
            message_id="<reply-ta1@example.com>", uid=2,
        )
        assert second.supplement is True
        assert service.tasks.require(task_id)["status"] == state.WAITING_INPUT

        replacement = build_machine_xlsx(tmp_path / "replacement.xlsx")
        reply = make_mail(
            sender="tester@example.com",
            subject=f"Re: [CATER {task_id} v2] 整机",
            text="更新附件",
            attachments=(("machine.xlsx", replacement.read_bytes()),),
            in_reply_to="<origin@example.com>",
            message_id="<reply-ta2@example.com>",
        )
        third = service.ingest(raw_message(reply, uid=3))
        assert third.supplement is True
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 3
        v3_links = service.tasks.version_mails(task_id, 3, "supplement_table")
        assert len(v3_links) == 1
    finally:
        service.close()


def test_completed_task_explicit_reprocess_retains_table_source(tmp_path: Path) -> None:
    """已完成（READY）任务的显式重新处理：新版本继承全部表格来源邮件。"""
    service = make_service(tmp_path)
    try:
        task_id = start_task_with_origin_text(
            service, tmp_path, text_table(PARTIAL_HEADERS, PARTIAL_ROWS))
        # 第一步：只回目的国；第二步：回完整业务表 → READY
        dest = reply_table(
            service, task_id,
            html=None,
            text="目的国：印度尼西亚",
            message_id="<reply-dest@example.com>", uid=2,
        )
        assert dest.supplement is True
        assert service.tasks.require(task_id)["status"] == state.WAITING_INPUT
        second = reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="完整业务表",
            message_id="<reply-done@example.com>", uid=3,
        )
        assert second.supplement is True
        assert service.tasks.require(task_id)["status"] == state.READY_TO_SEND

        service.reprocess_task(task_id, actor="test-admin")
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 4
        v4_links = service.tasks.version_mails(task_id, 4, "supplement_table")
        assert len(v4_links) == 1
        result = service.process_task(task_id, actor="test")
        assert result["status"] in {state.READY_TO_SEND, state.COMPLETED}, result.get("last_error")
    finally:
        service.close()


def test_two_partial_supplement_tables_accumulate_by_mso(tmp_path: Path) -> None:
    """连续两封部分表格补件按 MSO 累积：第二封不得覆盖第一封已填字段。"""
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        # 第一封：只补序列号列（仍缺 Total Sales Price → 继续暂停）
        serial_headers = PARTIAL_HEADERS + ["SO Serial Number"]
        serial_rows = [row + [serial] for row, serial
                       in zip(PARTIAL_ROWS, ("SYN000001A", "SYN000002B"))]
        second = reply_table(
            service, task_id,
            html=table_html(serial_headers, serial_rows),
            text="补序列号",
            message_id="<reply-partial1@example.com>", uid=2,
        )
        assert second.supplement is True
        assert service.tasks.require(task_id)["status"] == state.WAITING_INPUT

        # 第二封：只补 Total Sales Price 列（MSO + 价格），不带序列号列
        price_headers = ["Cust. Purch. Order", "Total Sales Price"]
        price_rows = [[mso, "50000.00"] for mso in ("FAKE001", "FAKE002")]
        third = reply_table(
            service, task_id,
            html=table_html(price_headers, price_rows),
            text="补价格",
            message_id="<reply-partial2@example.com>", uid=3,
        )
        assert third.supplement is True
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 3
        assert task["status"] == state.READY_TO_SEND, service.tasks.issues(task_id, 3)

        # 两封来源邮件都在 v3 上（累积继承）
        v3_links = service.tasks.version_mails(task_id, 3, "supplement_table")
        assert len(v3_links) == 2
        # 两封的值都进了 provenance：先填的序列号没被第二封抹掉
        detail = service.task_detail(task_id)
        serials = {
            item["record_key"]: str(item["value"])
            for item in detail["provenance"] if item["field"] == "serial_number"
        }
        assert serials == {"FAKE001": "SYN000001A", "FAKE002": "SYN000002B"}
    finally:
        service.close()


def test_supersede_malformed_origin_table_with_clean_supplement(tmp_path: Path) -> None:
    """原始邮件业务表结构损坏（同字段多列 AMBIGUOUS_COLUMN）且一行数据
    都解析不出来时，干净的补件完整表显式接管：留痕 TABLE_SUPERSEDED
    （非阻断），不再被原始表的结构问题永久卡死；缺失字段仍如实暂停。"""
    service = make_service(tmp_path)
    try:
        workbook = build_machine_xlsx(tmp_path / "machine.xlsx")
        # 原始表：两列 Dealer（结构歧义）→ 阻断、零数据行
        bad_headers = ["Regional", "Cust. Purch. Order", "Dealer", "Dealer"]
        bad_html = table_html(bad_headers, [["AMS", "FAKE001", "UM16", "UM16"]])
        result = service.ingest(
            raw_message(
                make_mail(
                    text="目的国：法国",
                    html=bad_html,
                    attachments=(("machine.xlsx", workbook.read_bytes()),),
                    message_id="<origin-bad@example.com>",
                ),
                uid=1,
            )
        )
        task_id = str(result.task_id)
        task = service.tasks.require(task_id)
        assert task["status"] == state.WAITING_INPUT
        codes = [i["code"] for i in service.tasks.issues(task_id)]
        assert "AMBIGUOUS_COLUMN" in codes, codes

        # 补件：干净完整表 → 原始结构问题被显式接管（留痕），但缺字段仍暂停
        second = reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="完整业务表",
            message_id="<reply-supersede@example.com>", uid=2,
        )
        assert second.supplement is True
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 2
        codes_v2 = [i["code"] for i in service.tasks.issues(task_id, 2)]
        assert "TABLE_SUPERSEDED" in codes_v2, codes_v2
        # 结构歧义不再阻断（被接管）；补件表补齐全部缺失字段 → READY
        assert "AMBIGUOUS_COLUMN" not in codes_v2
        assert task["status"] == state.READY_TO_SEND, codes_v2
    finally:
        service.close()


def test_clear_test_data_removes_version_mails_without_orphans(tmp_path: Path) -> None:
    """clear-test 必须连带清掉 task_version_mails（外键指向 mail_messages/
    task_versions），不留孤儿行、不触发 FK 违例。"""
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="完整业务表",
            message_id="<reply-clear@example.com>", uid=2,
        )
        assert service.tasks.version_mails(task_id, 2, "supplement_table")
        assert service.test_identity() == "test"
        result = service.clear_test_data(actor="test", confirm=True)
        assert result["safety_backup"]
        assert service.db.scalar("SELECT COUNT(*) FROM task_version_mails") == 0
        assert service.db.scalar("SELECT COUNT(*) FROM tasks") == 0
        assert service.db.scalar("SELECT COUNT(*) FROM mail_messages") == 0
        assert service.db.quick_check() == "ok"
    finally:
        service.close()


def test_apply_supplement_rollback_preserves_version_mails(tmp_path: Path) -> None:
    """apply_supplement 事务失败整体回滚：版本不前进、来源邮件链接不变。"""
    service = make_service(tmp_path)
    try:
        task_id = start_incomplete_task(service, tmp_path)
        reply_table(
            service, task_id,
            html=table_html(FULL_HEADERS, FULL_ROWS),
            text="完整业务表",
            message_id="<reply-rollback@example.com>", uid=2,
        )
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 2
        links_before = service.tasks.version_mails(task_id, 2, "supplement_table")
        assert len(links_before) == 1

        # 直接调用仓储层：传入不存在的 mail_id 触发 FK 违例 → 整体回滚
        with pytest.raises(Exception):
            service.tasks.apply_supplement(
                task_id,
                actor="tester@example.com",
                reason="forced rollback probe",
                version_mails=[("no_such_mail", "supplement_table")],
            )
        task = service.tasks.require(task_id)
        assert int(task["version"]) == 2, "失败的事务不得推进版本"
        assert service.tasks.version_mails(task_id, 2, "supplement_table") == [
            dict(link) for link in links_before
        ]
        assert service.tasks.version_mails(task_id, 3, "supplement_table") == []
    finally:
        service.close()
