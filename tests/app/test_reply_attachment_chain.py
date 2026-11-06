"""补件附件链回归（真实核心 + 真实 intake 落库）：

- 初始邮件附两份内容不同的合格整机附件 → AMBIGUOUS_INPUT 暂停整票，
  问题正文列出候选文件名，绝不出现“缺少附件”类表述或数值修正指令；
- 等待中的任务收到相同字节、不同文件名的再传 → 视为同一来源候选：
  不产生新输入行、不报文件名不匹配、已存审计记录原样保留；
- 同名附件内容变更 → 只使用当前生效版本（新版本仅含新内容，
  旧版本输入行与其文件保留，不合并新旧）；
- 真实 .xls（合成夹具）经 AppService 保存并处理 → READY。

全部为合成数据（FAKE MSO / SYN 序列号），无真实邮箱访问/发送。
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from cater.app import state
from cater.app.application import AppService
from cater.app.engine import CoreProcessEngine
from cater.app.mail.simulator import SimulatorMailGateway

from helpers import make_config, make_mail, raw_message

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEXIBLE_MACHINE_XLS = (
    REPO_ROOT / "tests" / "core" / "fixtures" / "flexible_machine.xls"
)

pytestmark = pytest.mark.skipif(
    not FLEXIBLE_MACHINE_XLS.is_file(),
    reason="synthetic flexible machine xls fixture not available",
)

SYNTHETIC_MAIL_TABLE = (
    "以下为合成业务表（fixtures only）\n"
    "Dealer Code\tDealer Order\tCust. Purch. Order (MSO#)\tOrder\t"
    "Material\tSerial Number\tTotal Sales Price\n"
    "SYN01\tSYN-ORD-1\tFAKE001\t06200000101\tSEMTEST\tSYN000001A\t50000.00\n"
    "SYN02\tSYN-ORD-2\tFAKE002\t06200000201\tSEMTEST\tSYN000002B\t50000.00\n"
)


def build_machine_xlsx(
    path: Path,
    *,
    net: float = 24000.0,
    serials: tuple[str, str] = ("SYN000001A", "SYN000002B"),
    msos: tuple[str, str] = ("FAKE001", "FAKE002"),
    invoice_no: str = "SYN-TEST-0001",
) -> Path:
    """合成整机发票/箱单 xlsx（结构仿 flexible_machine.xls，数据全合成）。"""
    book = openpyxl.Workbook()
    ci = book.active
    ci.title = "CI"
    ci.cell(9, 12, "INVOICE NO.: " + invoice_no)
    ci.cell(34, 3, f"CMOPS MSO#: {msos[0]}/{msos[1]}\nSEMTEST WHEEL LOADER")
    ci.cell(53, 1, f"NET WEIGHT: {net:g} KG")
    ci.cell(55, 1, "MACHINE INFORMATION:")
    for col, title in ((1, "ITEM#"), (2, "MODEL"), (5, "SERIAL #"),
                       (7, "ENGINE #"), (11, "PIN #"), (15, "MSO")):
        ci.cell(56, col, title)
    for row, (serial, mso, engine, pin) in (
        (57, (serials[0], msos[0], "SYNENG001", "SYNPIN0000001")),
        (58, (serials[1], msos[1], "SYNENG002", "SYNPIN0000002")),
    ):
        ci.cell(row, 1, row - 56)
        ci.cell(row, 2, "SEMTEST")
        ci.cell(row, 5, serial)
        ci.cell(row, 7, engine)
        ci.cell(row, 11, pin)
        ci.cell(row, 15, mso)
    pl = book.create_sheet("PL")
    pl.cell(9, 14, "INVOICE NO.: " + invoice_no)
    pl.cell(28, 1, "DESCRIPTION OF GOODS")
    pl.cell(28, 13, "QTY")
    pl.cell(28, 16, "TOTAL WEIGHT/KGS")
    pl.cell(29, 16, "GROSS")
    pl.cell(29, 19, "NET")
    description = (
        f"CMOPS MSO#: {msos[0]}/{msos[1]}\nSEMTEST WHEEL LOADER\n"
        f"SN: {serials[0]}/{serials[1]}"
    )
    pl.cell(30, 1, description)
    pl.cell(30, 13, 2)
    pl.cell(30, 14, "UNITS")
    pl.cell(30, 16, net)
    pl.cell(30, 19, net)
    pl.cell(31, 1, "TOTAL")
    pl.cell(31, 13, 2)
    pl.cell(31, 16, net)
    pl.cell(31, 19, net)
    book.save(path)
    return path


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


def reply_mail(task_id: str, *, text: str, attachments=(),
               in_reply_to: str, message_id: str):
    return make_mail(
        subject=f"Re: [CATER {task_id} v1] 整机",
        text=text,
        attachments=attachments,
        in_reply_to=in_reply_to,
        message_id=message_id,
    )


def question_bodies(service: AppService, task_id: str) -> list[str]:
    return [
        str(row["body"])
        for row in service.outbox.list_recent()
        if row["purpose"] == "question" and row["task_id"] == task_id
    ]


# ---------------------------------------------------------------------------
# 歧义：两份内容不同的合格附件 → 暂停整票，正文列出候选文件名
# ---------------------------------------------------------------------------

def test_initial_mail_two_different_workbooks_pauses_with_candidates(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    try:
        first = build_machine_xlsx(tmp_path / "machine-a.xlsx")
        second = build_machine_xlsx(tmp_path / "machine-b.xlsx", net=25000.0)
        result = service.ingest(
            raw_message(
                make_mail(
                    text="目的国：法国\n" + SYNTHETIC_MAIL_TABLE,
                    attachments=(
                        ("machine-a.xlsx", first.read_bytes()),
                        ("machine-b.xlsx", second.read_bytes()),
                    ),
                    message_id="<amb@example.com>",
                ),
                uid=1,
            )
        )
        task_id = str(result.task_id)
        task = service.tasks.require(task_id)
        assert task["status"] == state.WAITING_INPUT
        issue_codes = [i["code"] for i in service.tasks.issues(task_id)]
        assert issue_codes.count("AMBIGUOUS_INPUT") == 1, issue_codes
        assert "MISSING_SOURCE" not in issue_codes
        bodies = question_bodies(service, task_id)
        assert bodies, "expected a question outbox row"
        body = bodies[-1]
        assert "machine-a.xlsx" in body and "machine-b.xlsx" in body
        # 歧义正文绝不误称缺少附件，也不要求数值修正（附件未定稿）
        assert "缺少" not in body
        assert "修正：" not in body
        # 两份输入都已保存（审计保留，不删除）
        filenames = sorted(
            item["filename"] for item in service.tasks.inputs(task_id, 1)
        )
        assert filenames == ["machine-a.xlsx", "machine-b.xlsx"]
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 相同字节再传（换名）= 同一来源候选：不阻塞、不重复、审计保留
# ---------------------------------------------------------------------------

def test_identical_reupload_different_name_is_one_candidate(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        workbook = build_machine_xlsx(tmp_path / "machine.xlsx")
        payload = workbook.read_bytes()
        result = service.ingest(
            raw_message(
                make_mail(
                    text=SYNTHETIC_MAIL_TABLE,  # 无目的国：任务先挂起
                    attachments=(("machine.xlsx", payload),),
                    message_id="<dup@example.com>",
                ),
                uid=1,
            )
        )
        task_id = str(result.task_id)
        assert service.tasks.require(task_id)["status"] == state.WAITING_INPUT
        version1_inputs = service.tasks.inputs(task_id, 1)
        assert [item["filename"] for item in version1_inputs] == ["machine.xlsx"]

        reply = reply_mail(
            task_id,
            text=f"任务编号：{task_id}\n",
            attachments=(("renamed-copy.xlsx", payload),),
            in_reply_to="<dup@example.com>",
            message_id="<dup2@example.com>",
        )
        second = service.ingest(raw_message(reply, uid=2))
        assert second.supplement is True

        task = service.tasks.require(task_id)
        version = int(task["version"])
        assert version == 2
        # 相同字节不产生第二个输入行；既有输入行与文件原样保留
        version2_inputs = service.tasks.inputs(task_id, version)
        assert [(item["filename"], item["file_hash"]) for item in version2_inputs] == [
            (item["filename"], item["file_hash"]) for item in version1_inputs
        ]
        stored = Path(version1_inputs[0]["path"])
        assert stored.is_file() and stored.read_bytes() == payload
        # 不得按“文件名不匹配”阻塞
        issue_codes = {i["code"] for i in service.tasks.issues(task_id)}
        assert "ATTACHMENT_NAME_MISMATCH" not in issue_codes
        # 补齐目的国后应正常出票，且只有一套输出
        reply3 = reply_mail(
            task_id,
            text=f"任务编号：{task_id}\n目的国：法国\n",
            in_reply_to="<dup2@example.com>",
            message_id="<dup3@example.com>",
        )
        service.ingest(raw_message(reply3, uid=3))
        task = service.tasks.require(task_id)
        assert task["status"] == state.READY_TO_SEND, task.get("last_error")
        final_inputs = service.tasks.inputs(task_id, int(task["version"]))
        assert [item["filename"] for item in final_inputs] == ["machine.xlsx"]
        detail = service.task_detail(task_id)
        assert detail["artifacts"], "ready 必须有产物"
        assert len(detail["artifacts"]) == len(
            {item["filename"] for item in detail["artifacts"]}
        ), "输出不得重复"
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 同名附件内容变更 → 只使用当前生效版本，不合并新旧
# ---------------------------------------------------------------------------

def test_same_name_replacement_uses_current_version_only(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        original = build_machine_xlsx(tmp_path / "machine.xlsx")
        original_payload = original.read_bytes()
        result = service.ingest(
            raw_message(
                make_mail(
                    text=SYNTHETIC_MAIL_TABLE,
                    attachments=(("machine.xlsx", original_payload),),
                    message_id="<repl@example.com>",
                ),
                uid=1,
            )
        )
        task_id = str(result.task_id)
        assert service.tasks.require(task_id)["status"] == state.WAITING_INPUT
        version1_inputs = service.tasks.inputs(task_id, 1)
        old_hash = str(version1_inputs[0]["file_hash"])
        old_path = Path(version1_inputs[0]["path"])

        changed = build_machine_xlsx(tmp_path / "changed.xlsx", net=25000.0)
        changed_payload = changed.read_bytes()
        assert changed_payload != original_payload
        reply = reply_mail(
            task_id,
            text=f"任务编号：{task_id}\n目的国：法国\n",
            attachments=(("machine.xlsx", changed_payload),),
            in_reply_to="<repl@example.com>",
            message_id="<repl2@example.com>",
        )
        second = service.ingest(raw_message(reply, uid=2))
        assert second.supplement is True

        task = service.tasks.require(task_id)
        assert task["status"] == state.READY_TO_SEND, task.get("last_error")
        version2_inputs = service.tasks.inputs(task_id, 2)
        # 新版本只含当前生效版本（新内容），不与旧内容合并
        assert [item["filename"] for item in version2_inputs] == ["machine.xlsx"]
        new_hash = str(version2_inputs[0]["file_hash"])
        assert new_hash != old_hash
        assert Path(version2_inputs[0]["path"]).read_bytes() == changed_payload
        # 旧版本输入行与其存储文件保留（审计不删除）
        assert old_path.is_file() and old_path.read_bytes() == original_payload
        assert str(service.tasks.inputs(task_id, 1)[0]["file_hash"]) == old_hash
        # 输出使用新内容（净重 25000 进入 provenance）
        provenance = service.task_detail(task_id)["provenance"]
        # 单台净重 = 箱单总净重 / 台数：新内容 25000/2=12500（旧内容应为 12000）
        net_values = [
            float(item["value"])
            for item in provenance
            if item["field"] == "net_weight" and item["record_key"] == "FAKE001"
        ]
        assert net_values and all(value == 12500.0 for value in net_values)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 真实 .xls（合成夹具）经 AppService 保存并处理
# ---------------------------------------------------------------------------

def test_saved_xls_attachment_processes_ready(tmp_path: Path) -> None:
    """生产缺陷场景的正面回归：保存过的 .xls 必须被选中并正常出票。"""
    service = make_service(tmp_path)
    try:
        result = service.ingest(
            raw_message(
                make_mail(
                    text="目的国：法国\n" + SYNTHETIC_MAIL_TABLE,
                    attachments=(("machine.xls", FLEXIBLE_MACHINE_XLS.read_bytes()),),
                    message_id="<xls@example.com>",
                ),
                uid=1,
            )
        )
        task_id = str(result.task_id)
        task = service.tasks.require(task_id)
        assert task["status"] == state.READY_TO_SEND, task.get("last_error")
        inputs = service.tasks.inputs(task_id, 1)
        assert [item["filename"] for item in inputs] == ["machine.xls"]
        stored = Path(inputs[0]["path"])
        assert stored.is_file()
        assert stored.read_bytes() == FLEXIBLE_MACHINE_XLS.read_bytes()
        issue_codes = {i["code"] for i in service.tasks.issues(task_id)}
        assert "MISSING_SOURCE" not in issue_codes
        assert "AMBIGUOUS_INPUT" not in issue_codes
    finally:
        service.close()
