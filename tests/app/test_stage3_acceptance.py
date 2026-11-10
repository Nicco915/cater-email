"""阶段 3 独立端到端验收（全合成材料；真实三封仅隔离运行时只读回放）。

本文件只验收，不实现、不修改其他 worker 的测试。覆盖真实链路：

1. 合成多票 EML → 命令行 ``import-batch`` 持久化批次 →
   服务端 ``confirm_and_create`` 人工确认（同一生产边界）→
   ``execution_hold=1`` 的逐票任务 → 命令行 ``run-batch-offline``
   显式离线执行（真实 ``CoreProcessEngine``，不注入假 core）→
   重新打开生成模板，逐票核对字段/数量/单位/净毛重/金额/HS/来源。
2. 坏输入（非有限价格、被篡改快照）与缺参考（无机型库）必须阻断，
   绝不产出 ready 业务文件。
3. 离线执行前后正式 ``tasks``/``batch_tasks``/``outbox``/版本/事件、
   批次快照与生产输出目录逐字节不变，hold 不释放。
4. 本机存在真实研究邮件时，只在隔离 runtime 只读导入回放；不设置
   目的国、不确认、不生成 ready/任务/快照，原始 .eml 哈希不变。

可跟踪测试只使用合成数据：发票号、MSO、序列号、料号、金额、重量均
为 ``SYN*`` 假值；真实材料路径只出现在 gitignored 的 input/ 下且缺失
时 skip，不复制任何真实字段进本文件或 fixture。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
from openpyxl import Workbook

from cater.app.application import AppService
from cater.app.engine import CoreProcessEngine
from cater.app.errors import ValidationError
from cater.app.mail.simulator import SimulatorMailGateway

from helpers import make_config, make_mail

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "templates"
REAL_RESEARCH_ROOT = REPO_ROOT / "input" / "邮件研究"

DESTINATION_NAME = "法国"
DESTINATION_ALPHA3 = "FRA"

pytestmark = pytest.mark.skipif(
    not (TEMPLATE_DIR / "machine_materials.xlsx").is_file()
    or not (TEMPLATE_DIR / "machine_comm.xlsx").is_file()
    or not (TEMPLATE_DIR / "parts_ledger.xlsx").is_file(),
    reason="仓库模板缺失，端到端验收无法生成模板文件",
)


# ---------------------------------------------------------------------------
# 合成业务材料（全部为假数据）
# ---------------------------------------------------------------------------

MACHINE_MODEL = "SYNLOADER"
MACHINE_HS = "8429510000"
MACHINE_PRODUCT = "合成装载机"
MACHINE_DECLARATION = "1|0|合成装载机|是自推进|100KG|合成品牌|SYNLOADER|无|无|无"
MACHINE_ENGLISH = "SYNTHETIC LOADER"

MACHINE_TICKETS = (
    {
        "key": "A",
        "invoice_no": "SYN-M-INV-A01",
        "mso": "SYNMA1",
        "serial": "SYNSN0001",
        "pin": "SYNPINSYNSN0001",
        "engine": "SYNENG0001",
        "price": "1000",
        "net": "100",
        "gross": "110",
        "dealer_code": "SYNDCA1",
        "dealer_order": "SYNDOA1",
        "order": "SYN-SO-A1",
        "attachment": "machine-a.xlsx",
    },
    {
        "key": "B",
        "invoice_no": "SYN-M-INV-B02",
        "mso": "SYNMB2",
        "serial": "SYNSN0002",
        "pin": "SYNPINSYNSN0002",
        "engine": "SYNENG0002",
        "price": "2000",
        "net": "200",
        "gross": "220",
        "dealer_code": "SYNDCB2",
        "dealer_order": "SYNDOB2",
        "order": "SYN-SO-B2",
        "attachment": "machine-b.xlsx",
    },
)

KIT_PARTS_A = (
    {
        "kit_no": 9900001, "part_code": "SYN-P100", "hs": "84219900",
        "coo": "US", "qty_per_set": 4, "sets": 3,
        "unit_price": "28.11", "unit_weight": "0.9",
        "name": "合成滤芯", "spec": "1|合成申报要素A",
    },
    {
        "kit_no": 9900001, "part_code": "SYN-B200", "hs": "73181510",
        "coo": "CN", "qty_per_set": 40, "sets": 3,
        "unit_price": "1.25", "unit_weight": "0.21",
        "name": "合成螺栓", "spec": "1|合成申报要素B",
    },
)
KIT_PARTS_B = (
    {
        "kit_no": 9900002, "part_code": "SYN-Q900", "hs": "84821000",
        "coo": "US", "qty_per_set": 4, "sets": 2,
        "unit_price": "30", "unit_weight": "1.0",
        "name": "合成轴承", "spec": "1|合成申报要素C",
    },
    {
        "kit_no": 9900002, "part_code": "SYN-R800", "hs": "73181510",
        "coo": "CN", "qty_per_set": 10, "sets": 2,
        "unit_price": "2.5", "unit_weight": "0.5",
        "name": "合成垫圈", "spec": "1|合成申报要素D",
    },
)

# 每个配件票：生成工作簿的参数 + 重开模板时逐字段期望值。
KIT_TICKETS = (
    {
        "key": "A",
        "invoice_no": "SYN-K-INV-A01",
        "mso": "SYNKA",
        "serial": "SYNKSN0001",
        "order_code": "SYNQMA",
        "parts": KIT_PARTS_A,
        "sets": 3,
        "packages": 2,
        "base_gross": "100",
        "tare": "10",
        "attachment": "kit-a.xlsx",
    },
    {
        "key": "B",
        "invoice_no": "SYN-K-INV-B02",
        "mso": "SYNKB",
        "serial": "SYNKSN0002",
        "order_code": "SYNQMB",
        "parts": KIT_PARTS_B,
        "sets": 2,
        "packages": 3,
        "base_gross": "150",
        "tare": "12",
        "attachment": "kit-b.xlsx",
    },
)

KIT_FEE = Decimal("10")


# ---------------------------------------------------------------------------
# 合成工作簿构造（独立实现，不复用被验收 worker 的测试构造器）
# ---------------------------------------------------------------------------


def _machine_workbook_bytes(ticket: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws["A1"] = f"INVOICE NO.: {ticket['invoice_no']}"
    for col, label in (
        (1, "MODEL"), (2, "MSO#"), (3, "SERIAL#"),
        (4, "PIN#"), (5, "ENGINE#"),
    ):
        ws.cell(3, col, label)
    ws.cell(4, 1, MACHINE_MODEL)
    ws.cell(4, 2, ticket["mso"])
    ws.cell(4, 3, ticket["serial"])
    ws.cell(4, 4, ticket["pin"])
    ws.cell(4, 5, ticket["engine"])
    ws["A6"] = "TOTAL AMOUNT (IN WORDS): SAY USD ONLY"
    ws["G6"] = _num(ticket["price"])

    pl = wb.create_sheet("Packing list")
    for col, label in (
        (1, "DESCRIPTION OF GOODS"), (2, "QTY"),
        (3, "NET WEIGHT (KGS)"), (4, "GROSS WEIGHT (KGS)"),
    ):
        pl.cell(2, col, label)
    pl.cell(3, 1, "SYNTHETIC MACHINE")
    pl.cell(3, 2, 1)
    pl.cell(3, 3, _num(ticket["net"]))
    pl.cell(3, 4, _num(ticket["gross"]))
    pl.cell(4, 1, "TOTAL")
    pl.cell(4, 2, 1)
    pl.cell(4, 3, _num(ticket["net"]))
    pl.cell(4, 4, _num(ticket["gross"]))
    return _workbook_bytes(wb)


def _write_kit_invoice(ws, parts, invoice_no: str) -> None:
    ws["G6"] = f"INVOICE NO.: {invoice_no}\n\nPRICE EFF DATE: NOV 03, 2026"
    headers = [
        "Health Care Kit No", "CHN HS Code", "COO", "DESCRIPTION OF GOODS",
        None, "Qty/SET", "SET QTY", "TOTAL Qty", "U'Price", "T'Price",
    ]
    for col, value in enumerate(headers, 1):
        ws.cell(10, col, value)
    ws["I11"] = "IN USD"
    ws["K11"] = "单件净重"
    ws["L11"] = "总净重"
    ws["Q11"] = "HS"
    ws["R11"] = "中文品名"
    ws["S11"] = "申报要素"

    row = 12
    amount_total = Decimal("0")
    net_total = Decimal("0")
    for part in parts:
        qty = Decimal(str(part["qty_per_set"])) * Decimal(str(part["sets"]))
        price = Decimal(str(part["unit_price"]))
        amount = price * qty
        net = Decimal(str(part["unit_weight"])) * qty
        ws.cell(row, 1, part["kit_no"])
        ws.cell(row, 2, part["hs"])
        ws.cell(row, 3, part["coo"])
        ws.cell(row, 4, part["part_code"])
        ws.cell(row, 5, "SYNTHETIC DESCRIPTION")
        ws.cell(row, 6, part["qty_per_set"])
        ws.cell(row, 7, part["sets"])
        ws.cell(row, 8, int(qty))
        ws.cell(row, 9, float(price))
        ws.cell(row, 10, float(amount))
        ws.cell(row, 11, float(Decimal(str(part["unit_weight"]))))
        ws.cell(row, 12, float(net))
        ws.cell(row, 17, part["hs"])
        ws.cell(row, 18, part["name"])
        ws.cell(row, 19, part["spec"])
        amount_total += amount
        net_total += net
        row += 1

    ws.cell(row, 1, "APPLY TO CUTOMS CHARGE")
    ws.cell(row, 8, 1)
    ws.cell(row, 9, float(KIT_FEE))
    ws.cell(row, 10, float(KIT_FEE))
    amount_total += KIT_FEE
    row += 1

    ws.cell(row, 1, "TOTAL AMOUNT (IN WORDS): SAY SYNTHETIC US DOLLARS")
    ws.cell(row, 10, float(amount_total))
    ws.cell(row, 12, float(net_total))


def _write_kit_packing(ws, ticket: dict) -> None:
    headers = [
        "KIT", "PO", "Order Code", "MSO", "Machine Model",
        "Machine Serial No", "QTY", "Packages", None, "Dimension", "Weight",
    ]
    for col, value in enumerate(headers, 1):
        ws.cell(8, col, value)
    ws.cell(9, 1, ticket["parts"][0]["kit_no"])
    ws.cell(9, 2, "SYN-PO-0001")
    ws.cell(9, 3, ticket["order_code"])
    ws.cell(9, 4, ticket["mso"])
    ws.cell(9, 5, "922F")
    ws.cell(9, 6, ticket["serial"])
    ws.cell(9, 7, ticket["sets"])
    ws.cell(9, 8, ticket["packages"])
    ws.cell(9, 11, float(_num(ticket["base_gross"])))

    tare = Decimal(str(ticket["tare"]))
    ws.cell(10, 1, "TARE WEIGHT")
    ws.cell(10, 11, float(tare))
    gross = _num(ticket["base_gross"]) + tare
    ws.cell(11, 1, "TOTAL")
    ws.cell(11, 7, ticket["sets"])
    ws.cell(11, 8, ticket["packages"])
    ws.cell(11, 11, float(gross))

    row = 13
    ws.cell(row, 1, "Every kit list is as below:")
    row += 1
    lower_headers = [
        "Kit No", "Machine Model", "Event/ part description", "CHN HS Code",
        "COO", "DESCRIPTION OF GOODS", None, "QTY/SET", "SET QTY",
        "TOTAL QTY", "NET WEIGHT",
    ]
    for col, value in enumerate(lower_headers, 1):
        ws.cell(row, col, value)
    row += 1
    qty_total = Decimal("0")
    net_total = Decimal("0")
    for part in ticket["parts"]:
        qty = Decimal(str(part["qty_per_set"])) * Decimal(str(part["sets"]))
        net = Decimal(str(part["unit_weight"])) * qty
        ws.cell(row, 1, part["kit_no"])
        ws.cell(row, 2, "922F")
        ws.cell(row, 3, "Maintenance 500H")
        ws.cell(row, 4, part["hs"])
        ws.cell(row, 5, part["coo"])
        ws.cell(row, 6, part["part_code"])
        ws.cell(row, 8, part["qty_per_set"])
        ws.cell(row, 9, part["sets"])
        ws.cell(row, 10, int(qty))
        ws.cell(row, 11, float(net))
        qty_total += qty
        net_total += net
        row += 1
    ws.cell(row, 1, "TOTAL")
    ws.cell(row, 10, float(qty_total))
    ws.cell(row, 11, float(net_total))


def _kit_workbook_bytes(ticket: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice "
    _write_kit_invoice(ws, ticket["parts"], ticket["invoice_no"])
    second = wb.create_sheet("Invoice  (2)")
    _write_kit_invoice(second, ticket["parts"], ticket["invoice_no"])
    _write_kit_packing(wb.create_sheet("Packing list"), ticket)
    bl = wb.create_sheet("BL")
    bl["A1"] = "Shipper"
    bl["D22"] = "Kind of packages;description of goods"
    bl["D25"] = f"INVOICE NO.:  {ticket['invoice_no']}"
    bl["A27"] = "Freight Prepaid"
    return _workbook_bytes(wb)


def _workbook_bytes(wb: Workbook) -> bytes:
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _num(value) -> Decimal:
    return Decimal(str(value))


def _machine_mail_html() -> str:
    columns = (
        "MSO", "Serial Number", "Total Sales Price",
        "Dealer Code", "Dealer Order", "Order", "Material",
    )
    header = "".join(f"<td>{name}</td>" for name in columns)
    rows = "".join(
        "<tr>"
        f"<td>{ticket['mso']}</td>"
        f"<td>{ticket['serial']}</td>"
        f"<td>{ticket['price']}</td>"
        f"<td>{ticket['dealer_code']}</td>"
        f"<td>{ticket['dealer_order']}</td>"
        f"<td>{ticket['order']}</td>"
        f"<td>{MACHINE_MODEL}</td>"
        "</tr>"
        for ticket in MACHINE_TICKETS
    )
    return (
        "<html><body><p>两张合成整机发票，请见附件。</p><table>"
        f"<tr>{header}</tr>{rows}</table></body></html>"
    )


def _machine_eml() -> bytes:
    attachments = tuple(
        (ticket["attachment"], _machine_workbook_bytes(ticket))
        for ticket in MACHINE_TICKETS
    )
    return make_mail(
        sender="tester@example.com",
        subject="合成两票整机发票",
        text="见附件两张整机发票。",
        html=_machine_mail_html(),
        attachments=attachments,
    )


def _kit_eml() -> bytes:
    attachments = tuple(
        (ticket["attachment"], _kit_workbook_bytes(ticket))
        for ticket in KIT_TICKETS
    )
    return make_mail(
        sender="tester@example.com",
        subject="合成两票配件发票",
        text="见附件两套配件发票/箱单。",
        html="<html><body><p>两张配件票，请见附件。</p></body></html>",
        attachments=attachments,
    )


# ---------------------------------------------------------------------------
# 隔离运行时、CLI 与数据库快照工具
# ---------------------------------------------------------------------------


@dataclass
class Sandbox:
    root: Path
    runtime: Path
    config_path: Path
    service: AppService


@pytest.fixture()
def sandbox(tmp_path: Path):
    runtime = tmp_path / "runtime"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[runtime]\n"
        f"dir = {json.dumps(str(runtime))}\n",
        encoding="utf-8",
    )
    service = AppService(
        make_config(tmp_path),
        engine=CoreProcessEngine(),
        gateway=SimulatorMailGateway(),
    )
    box = Sandbox(
        root=tmp_path, runtime=runtime,
        config_path=config_path, service=service,
    )
    try:
        yield box
    finally:
        service.close()


def _cli(sandbox: Sandbox, *args: str) -> subprocess.CompletedProcess[str]:
    """从无 .env 的临时工作目录调用真实 CLI（清除所有 CATER_* 环境）。"""
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("CATER_")
    }
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    command = [
        sys.executable, "-m", "cater.app.cli",
        "--config", str(sandbox.config_path),
        "--runtime", str(sandbox.runtime),
        *args,
    ]
    return subprocess.run(
        command, cwd=str(sandbox.root), env=environment,
        capture_output=True, text=True, timeout=300,
    )


def _write_eml(sandbox: Sandbox, name: str, payload: bytes) -> Path:
    path = sandbox.root / name
    path.write_bytes(payload)
    return path


def _import_eml(sandbox: Sandbox, path: Path) -> tuple[dict, dict]:
    result = _cli(
        sandbox, "import-batch", "--eml", str(path), "--json")
    assert result.returncode == 0, (
        f"import-batch 失败 rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}")
    payload = json.loads(result.stdout)
    detail = sandbox.service.batches.batch_detail(payload["batch_id"])
    return payload, detail


def _confirm_all(sandbox: Sandbox, detail: dict) -> list[dict]:
    preview = detail["preview"]
    assert preview is not None
    assert preview["batch_ready"] is True, preview.get("issues")
    candidates = preview["candidates"]
    keys = [str(candidate["candidate_id"]) for candidate in candidates]
    outcome = sandbox.service.batches.confirm_and_create(
        detail["batch"]["id"],
        plan_id=detail["plan"]["id"],
        fingerprint=detail["plan"]["source_fingerprint"],
        selected=keys,
        exclusions={},
        actor="acceptance-confirmer",
    )
    tasks = outcome["tasks"]
    assert len(tasks) == len(keys)
    return sorted(tasks, key=lambda task: str(task["candidate_key"]))


def _task_snapshot(task: dict) -> dict:
    return json.loads(Path(task["snapshot_path"]).read_text(encoding="utf-8"))


def _invoice_of(task: dict) -> str:
    snapshot = _task_snapshot(task)
    values = snapshot["candidate"]["identity"].get("invoice_no") or []
    assert len(values) == 1, values
    return str(values[0])


def _map_by_invoice(tasks: list[dict]) -> dict[str, dict]:
    return {_invoice_of(task): task for task in tasks}


def _run_offline(sandbox: Sandbox, task_id: str, out_dir: Path) -> dict:
    result = _cli(
        sandbox, "run-batch-offline", task_id,
        "--output-dir", str(out_dir), "--json")
    report_path = out_dir / "offline_report.json"
    assert result.returncode == 0, (
        f"run-batch-offline 失败 rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}")
    assert report_path.is_file(), result.stderr
    return json.loads(report_path.read_text(encoding="utf-8"))


def _db_state(sandbox: Sandbox) -> dict[str, list[dict]]:
    """正式数据快照：离线执行不得改变任何一行（任务/hold/outbox/审计）。"""
    tables = (
        "tasks", "task_versions", "task_events", "batch_tasks",
        "batch_candidates", "batch_events", "batch_assets",
        "batch_plan_versions", "mail_batches", "mail_messages", "outbox",
    )
    state: dict[str, list[dict]] = {}
    for table in tables:
        rows = sandbox.service.db.fetchall(
            f"SELECT * FROM {table} ORDER BY rowid")
        state[table] = [dict(row) for row in rows]
    return state


def _tree_state(root: Path) -> list[tuple[str, str, int]]:
    if not root.exists():
        return []
    return [
        (
            str(path.relative_to(root)),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _file_fingerprint(path: Path) -> tuple[str, int]:
    stat = path.stat()
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        stat.st_mtime_ns,
    )


def _assert_number(value, expected) -> None:
    assert value is not None, f"缺少数值，期望 {expected}"
    assert Decimal(str(value)) == Decimal(str(expected)), (
        f"数值 {value!r} != 期望 {expected!r}")


def _assert_blank(value) -> None:
    assert value in (None, ""), f"期望空白，实际 {value!r}"


def _assert_no_values_after(worksheet, last_row: int) -> None:
    for row in worksheet.iter_rows(min_row=last_row + 1):
        for cell in row:
            assert cell.value in (None, ""), (
                f"数据行之后仍有值：{cell.coordinate}={cell.value!r}")


def _cell_texts(worksheet) -> str:
    return "\n".join(
        str(cell.value)
        for row in worksheet.iter_rows()
        for cell in row
        if cell.value is not None
    )


def _assert_no_tokens(worksheet, tokens: tuple[str, ...]) -> None:
    text = _cell_texts(worksheet)
    found = [token for token in tokens if token in text]
    assert not found, f"输出混入其他票标识：{found}"


def _assert_state_untouched(
    sandbox: Sandbox,
    *,
    db_before: dict,
    batches_before: list,
    tasks_dir_before: list,
    mail_dir_before: list,
) -> None:
    assert _db_state(sandbox) == db_before, "离线执行改变了正式数据库行"
    assert _tree_state(sandbox.runtime / "batches") == batches_before, (
        "离线执行改写了批次快照目录")
    assert _tree_state(sandbox.service.config.tasks_dir) == tasks_dir_before, (
        "离线执行写入了生产 outputs（runtime/tasks）")
    assert _tree_state(sandbox.service.config.mail_dir) == mail_dir_before, (
        "离线执行写入了原始邮件目录（runtime/mail）")


# ---------------------------------------------------------------------------
# 1. 整机两票：导入 → 确认 → hold → 离线执行 → 重开模板逐票核对
# ---------------------------------------------------------------------------


def test_machine_two_tickets_offline_end_to_end(sandbox: Sandbox):
    sandbox.service.references.store_machines([{
        "model": MACHINE_MODEL,
        "english_name": MACHINE_ENGLISH,
        "hs_code": MACHINE_HS,
        "product_name": MACHINE_PRODUCT,
        "declaration": MACHINE_DECLARATION,
        "destination_scope": "default",
    }], source="acceptance-synthetic", actor="acceptance")

    eml = _write_eml(sandbox, "machines.eml", _machine_eml())
    payload, detail = _import_eml(sandbox, eml)
    assert payload["batch_ready"] is True
    assert detail["preview"]["candidate_count"] == 2
    assert detail["preview"]["unmatched_mail_rows"] == []
    assert detail["preview"]["unmatched_sources"] == []

    tasks = _confirm_all(sandbox, detail)
    assert len(tasks) == 2
    by_invoice = _map_by_invoice(tasks)
    assert set(by_invoice) == {
        ticket["invoice_no"] for ticket in MACHINE_TICKETS}

    for ticket in MACHINE_TICKETS:
        task = by_invoice[ticket["invoice_no"]]
        assert task["execution_hold"] == 1
        hold = sandbox.service.tasks.batch_hold(str(task["task_id"]))
        assert hold is not None and hold["execution_hold"] == 1
        sandbox.service.tasks.update_destination(
            str(task["task_id"]), DESTINATION_NAME, DESTINATION_ALPHA3)

    db_before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")
    tasks_dir_before = _tree_state(sandbox.service.config.tasks_dir)
    mail_dir_before = _tree_state(sandbox.service.config.mail_dir)

    for ticket in MACHINE_TICKETS:
        task = by_invoice[ticket["invoice_no"]]
        snapshot = _task_snapshot(task)
        assert snapshot["candidate"]["candidate_id"] == task["candidate_key"]
        assert snapshot["candidate"]["identity"]["invoice_no"] == [
            ticket["invoice_no"]]
        assert snapshot["candidate"]["identity"]["mso"] == [ticket["mso"]]
        assert snapshot["candidate"]["identity"]["serial"] == [
            ticket["serial"]]
        # 本票快照只含本票自己的一个附件（绝不夹带父邮件或别票材料）。
        assert len(snapshot["assets"]) == 1
        assert snapshot["assets"][0]["filename"] == ticket["attachment"]
        assert snapshot["assets"][0]["sha256"] == hashlib.sha256(
            Path(snapshot["assets"][0]["path"]).read_bytes()).hexdigest()
        assert len(snapshot["mail_rows"]) == 1
        weight_rows = snapshot["candidate"].get("weight_rows") or []
        assert any(
            str(row.get("qty")) == "1"
            and str(row.get("net")) == ticket["net"]
            and str(row.get("gross")) == ticket["gross"]
            for row in weight_rows
        ), weight_rows
        mail_fields = snapshot["mail_rows"][0]["fields"]
        assert mail_fields["mso"] == ticket["mso"]
        assert mail_fields["serial_number"] == ticket["serial"]
        assert mail_fields["total_sales_price"] == ticket["price"]
        assert mail_fields["dealer_code"] == ticket["dealer_code"]
        assert mail_fields["dealer_order"] == ticket["dealer_order"]
        assert mail_fields["order"] == ticket["order"]
        # 快照内工作簿即本票“来源”：输出值必须能追回这些单元格。
        source_book = openpyxl.load_workbook(
            snapshot["assets"][0]["path"], data_only=True)
        source_invoice = source_book["Invoice"]
        assert source_invoice["A1"].value == (
            f"INVOICE NO.: {ticket['invoice_no']}")
        assert source_invoice["B4"].value == ticket["mso"]
        assert source_invoice["C4"].value == ticket["serial"]
        _assert_number(source_invoice["G6"].value, ticket["price"])
        source_packing = source_book["Packing list"]
        assert source_packing["B4"].value == 1
        _assert_number(source_packing["C4"].value, ticket["net"])
        _assert_number(source_packing["D4"].value, ticket["gross"])

        out_dir = sandbox.root / "offline" / f"machine-{ticket['key']}"
        report = _run_offline(sandbox, str(task["task_id"]), out_dir)
        assert report["status"] == "ready", report.get("issues")
        assert report["kind"] == "machine"
        assert report["records"] == 1
        assert report["issues"] == []
        assert report["state_unchanged"] is True
        assert report["task_state_after"]["execution_hold"] is True
        assert report["task_state_after"]["status"] == "RECEIVED"
        assert report["task_state_after"]["version"] == 1
        assert report["business_identity"] == (
            f"{ticket['mso']}|{ticket['invoice_no']}")
        assert report["snapshot"]["trusted_hash"] == task["snapshot_hash"]
        assert report["snapshot"]["verified_hash"] == task["snapshot_hash"]

        artifacts = {
            artifact["filename"]: artifact
            for artifact in report["artifacts"]
        }
        assert set(artifacts) == {"企业物料 上传.xlsx", "SEM发运沟通表 上传.xlsx"}
        for artifact in artifacts.values():
            path = Path(artifact["path"])
            assert path.is_file()
            assert path.resolve().parent == out_dir.resolve()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == (
                artifact["sha256"])

        materials = openpyxl.load_workbook(
            Path(artifacts["企业物料 上传.xlsx"]["path"]), data_only=True)
        material_sheet = materials["企业物料"]
        assert material_sheet["A6"].value == ticket["mso"]
        assert material_sheet["B6"].value == MACHINE_HS
        assert material_sheet["C6"].value == MACHINE_PRODUCT
        assert material_sheet["D6"].value == MACHINE_DECLARATION
        assert material_sheet["E6"].value == "台"       # 申报计量单位
        assert material_sheet["G6"].value == MACHINE_ENGLISH
        assert material_sheet["H6"].value == "成品"
        assert material_sheet["I6"].value == "台"       # 企业计量单位
        _assert_number(material_sheet["L6"].value, 1)    # 法定比例/数量口径
        _assert_number(material_sheet["M6"].value, ticket["net"])
        _assert_number(material_sheet["N6"].value, ticket["net"])
        assert material_sheet["P6"].value == "USD"
        assert material_sheet["Q6"].value == "CHN"
        assert material_sheet["U6"].value == MACHINE_MODEL
        _assert_no_values_after(material_sheet, 6)

        comm = openpyxl.load_workbook(
            Path(artifacts["SEM发运沟通表 上传.xlsx"]["path"]), data_only=True)
        comm_sheet = comm["Sheet1"]
        assert comm_sheet["A3"].value == ticket["dealer_code"]
        assert comm_sheet["C3"].value == ticket["dealer_order"]
        assert comm_sheet["D3"].value == ticket["mso"]
        assert comm_sheet["E3"].value == ticket["mso"]
        assert comm_sheet["F3"].value == f"0{ticket['order']}"
        assert comm_sheet["G3"].value == MACHINE_MODEL
        assert comm_sheet["H3"].value == ticket["serial"]
        _assert_number(comm_sheet["I3"].value, ticket["price"])  # 总金额
        assert comm_sheet["V3"].value == DESTINATION_NAME
        _assert_no_values_after(comm_sheet, 3)

        # 别票标识不得混入本票模板。
        other = next(item for item in MACHINE_TICKETS
                     if item["key"] != ticket["key"])
        forbidden = (
            other["mso"], other["serial"], other["invoice_no"],
            other["dealer_code"], other["order"],
        )
        _assert_no_tokens(material_sheet, forbidden)
        _assert_no_tokens(comm_sheet, forbidden)

        # 同目录重跑必须拒绝覆盖既有输出，且旧报告字节不变。
        report_bytes = (out_dir / "offline_report.json").read_bytes()
        rerun = _cli(
            sandbox, "run-batch-offline", str(task["task_id"]),
            "--output-dir", str(out_dir), "--json")
        assert rerun.returncode != 0
        assert out_dir.joinpath("offline_report.json").read_bytes() == (
            report_bytes)

    _assert_state_untouched(
        sandbox, db_before=db_before, batches_before=batches_before,
        tasks_dir_before=tasks_dir_before, mail_dir_before=mail_dir_before)
    assert not list((sandbox.runtime / "tasks").rglob("*"))


# ---------------------------------------------------------------------------
# 2. 配件两票（Health Care Kit）：导入 → 确认 → 离线执行 → 台账重开核对
# ---------------------------------------------------------------------------


def _kit_expected_rows(ticket: dict) -> list[dict]:
    gross = _num(ticket["base_gross"]) + _num(ticket["tare"])
    rows = []
    for part in ticket["parts"]:
        qty = Decimal(str(part["qty_per_set"])) * Decimal(str(part["sets"]))
        rows.append({
            "part_code": part["part_code"],
            "qty": qty,
            "unit_price": Decimal(str(part["unit_price"])),
            "line_amount": Decimal(str(part["unit_price"])) * qty,
            "net_weight": Decimal(str(part["unit_weight"])) * qty,
            "hs": part["hs"],
            "coo": {"US": "USA", "CN": "CHN"}[part["coo"]],
            "name": part["name"],
            "spec": part["spec"],
            "gross": gross,
            "packages": ticket["packages"],
        })
    return rows


def test_kit_two_tickets_offline_end_to_end(sandbox: Sandbox):
    eml = _write_eml(sandbox, "kits.eml", _kit_eml())
    payload, detail = _import_eml(sandbox, eml)
    assert payload["batch_ready"] is True
    assert detail["preview"]["candidate_count"] == 2
    assert detail["preview"]["unmatched_mail_rows"] == []
    assert detail["preview"]["unmatched_sources"] == []

    tasks = _confirm_all(sandbox, detail)
    assert len(tasks) == 2
    by_invoice = _map_by_invoice(tasks)
    assert set(by_invoice) == {
        ticket["invoice_no"] for ticket in KIT_TICKETS}

    for ticket in KIT_TICKETS:
        task = by_invoice[ticket["invoice_no"]]
        assert task["execution_hold"] == 1
        sandbox.service.tasks.update_destination(
            str(task["task_id"]), DESTINATION_NAME, DESTINATION_ALPHA3)

    db_before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")
    tasks_dir_before = _tree_state(sandbox.service.config.tasks_dir)
    mail_dir_before = _tree_state(sandbox.service.config.mail_dir)

    expected_by_invoice = {
        ticket["invoice_no"]: (ticket, _kit_expected_rows(ticket))
        for ticket in KIT_TICKETS
    }

    for ticket in KIT_TICKETS:
        task = by_invoice[ticket["invoice_no"]]
        snapshot = _task_snapshot(task)
        assert snapshot["candidate"]["candidate_id"] == task["candidate_key"]
        assert snapshot["candidate"]["identity"]["invoice_no"] == [
            ticket["invoice_no"]]
        assert len(snapshot["assets"]) == 1
        assert snapshot["assets"][0]["filename"] == ticket["attachment"]
        source_book = openpyxl.load_workbook(
            snapshot["assets"][0]["path"], data_only=True)
        source_invoice = source_book["Invoice "]
        source_packing = source_book["Packing list"]

        expected = expected_by_invoice[ticket["invoice_no"]][1]
        for index, part in enumerate(expected):
            source_row = 12 + index
            _assert_number(
                source_invoice.cell(source_row, 9).value,
                part["unit_price"])
            _assert_number(
                source_invoice.cell(source_row, 10).value,
                part["line_amount"])
            _assert_number(
                source_invoice.cell(source_row, 12).value,
                part["net_weight"])

        out_dir = sandbox.root / "offline" / f"kit-{ticket['key']}"
        report = _run_offline(sandbox, str(task["task_id"]), out_dir)
        assert report["status"] == "ready", report.get("issues")
        assert report["kind"] == "parts"
        assert report["records"] == len(expected)
        assert report["issues"] == []
        assert report["state_unchanged"] is True
        assert report["task_state_after"]["execution_hold"] is True
        assert report["task_state_after"]["status"] == "RECEIVED"
        assert report["business_identity"] == ticket["invoice_no"]
        assert report["snapshot"]["trusted_hash"] == task["snapshot_hash"]

        assert len(report["artifacts"]) == 1
        artifact = report["artifacts"][0]
        assert artifact["filename"] == "出口台账表体.xlsx"
        artifact_path = Path(artifact["path"])
        assert artifact_path.is_file()
        assert artifact_path.resolve().parent == out_dir.resolve()
        assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == (
            artifact["sha256"])

        ledger = openpyxl.load_workbook(artifact_path, data_only=True)
        sheet = ledger["导入模板"]
        _assert_no_values_after(sheet, 5 + len(expected))
        gross = _num(ticket["base_gross"]) + _num(ticket["tare"])
        for index, part in enumerate(expected):
            row = 6 + index
            assert sheet.cell(row, 2).value == part["part_code"]
            _assert_number(sheet.cell(row, 4).value, part["qty"])
            assert sheet.cell(row, 7).value == "USD"
            _assert_number(sheet.cell(row, 8).value, part["unit_price"])
            _assert_number(sheet.cell(row, 10).value, part["net_weight"])
            if index == 0:
                _assert_number(sheet.cell(row, 11).value, gross)
                _assert_number(sheet.cell(row, 12).value, ticket["packages"])
            else:
                _assert_blank(sheet.cell(row, 11).value)
                _assert_blank(sheet.cell(row, 12).value)
            assert sheet.cell(row, 13).value == part["coo"]
            assert sheet.cell(row, 14).value == DESTINATION_ALPHA3
            assert sheet.cell(row, 20).value == part["hs"]
            assert sheet.cell(row, 21).value == part["name"]
            assert sheet.cell(row, 22).value == part["spec"]
            _assert_blank(sheet.cell(row, 26).value)  # 订单号禁止复制旧值
            if index == 0:
                # 整票字段（毛重/件数/发票号）只写在首行，后续行留空。
                assert sheet.cell(row, 27).value == ticket["invoice_no"]
            else:
                _assert_blank(sheet.cell(row, 27).value)
            # 台账无“总价”列，用 数量×单价 核对源发票行金额（C 已核对
            # 费用行排除与 TOTAL，两个口径必须一致）。
            assert Decimal(str(sheet.cell(row, 8).value)) * Decimal(
                str(sheet.cell(row, 4).value)) == part["line_amount"]

        other = next(item for item in KIT_TICKETS
                     if item["key"] != ticket["key"])
        forbidden = (
            other["invoice_no"], other["mso"], other["serial"],
        ) + tuple(part["part_code"] for part in other["parts"])
        _assert_no_tokens(sheet, forbidden)

    _assert_state_untouched(
        sandbox, db_before=db_before, batches_before=batches_before,
        tasks_dir_before=tasks_dir_before, mail_dir_before=mail_dir_before)


# ---------------------------------------------------------------------------
# 3. 坏输入：不可读工作簿在确认阶段必须阻断，不能默认忽略建票
# ---------------------------------------------------------------------------


def test_broken_workbook_blocks_batch_confirmation(sandbox: Sandbox):
    attachments = tuple(
        (ticket["attachment"], _machine_workbook_bytes(ticket))
        for ticket in MACHINE_TICKETS
    ) + (("broken.xlsx", b"not-a-real-xlsx"),)
    eml = _write_eml(
        sandbox, "broken.eml",
        make_mail(
            sender="tester@example.com",
            subject="合成损坏附件整机票",
            text="见附件。",
            html=_machine_mail_html(),
            attachments=attachments,
        ))
    payload, detail = _import_eml(sandbox, eml)
    assert payload["batch_ready"] is False
    assert detail["preview"]["unmatched_sources"]
    codes = {
        str(issue["code"]) for issue in detail["preview"]["issues"]
        if issue.get("blocking")
    }
    assert "UNSUPPORTED_FILE" in codes
    db_before = _db_state(sandbox)
    with pytest.raises(ValidationError):
        sandbox.service.batches.confirm_and_create(
            detail["batch"]["id"],
            plan_id=detail["plan"]["id"],
            fingerprint=detail["plan"]["source_fingerprint"],
            selected=[
                str(candidate["candidate_id"])
                for candidate in detail["preview"]["candidates"]
            ],
            exclusions={},
            actor="acceptance-confirmer",
        )
    assert _db_state(sandbox) == db_before
    assert sandbox.service.db.scalar(
        "SELECT COUNT(*) FROM tasks") == 0
    assert sandbox.service.db.scalar(
        "SELECT COUNT(*) FROM batch_tasks") == 0
    assert sandbox.service.db.scalar(
        "SELECT COUNT(*) FROM outbox") == 0
    batch_dir = sandbox.runtime / "batches" / detail["batch"]["id"]
    assert not list(batch_dir.rglob("snapshots"))


# ---------------------------------------------------------------------------
# 3. 坏输入：非有限价格可过预览但显式离线执行必须阻断，不产 ready
# ---------------------------------------------------------------------------


def test_non_finite_kit_price_blocks_offline_without_artifacts(
        sandbox: Sandbox):
    ticket = dict(KIT_TICKETS[0])
    workbook_bytes = _kit_workbook_bytes(ticket)
    book = openpyxl.load_workbook(io.BytesIO(workbook_bytes))
    for sheet_name in ("Invoice ", "Invoice  (2)"):
        book[sheet_name]["I12"] = "NaN"
    buffer = io.BytesIO()
    book.save(buffer)
    eml = _write_eml(
        sandbox, "bad-kit.eml",
        make_mail(
            sender="tester@example.com",
            subject="合成坏价格配件票",
            text="见附件。",
            html="<html><body><p>坏价格测试。</p></body></html>",
            attachments=((ticket["attachment"], buffer.getvalue()),),
        ))

    payload, detail = _import_eml(sandbox, eml)
    assert payload["batch_ready"] is True
    tasks = _confirm_all(sandbox, detail)
    assert len(tasks) == 1
    task = tasks[0]
    sandbox.service.tasks.update_destination(
        str(task["task_id"]), DESTINATION_NAME, DESTINATION_ALPHA3)
    db_before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")
    tasks_dir_before = _tree_state(sandbox.service.config.tasks_dir)
    mail_dir_before = _tree_state(sandbox.service.config.mail_dir)

    out_dir = sandbox.root / "offline" / "bad-kit"
    report = _run_offline(sandbox, str(task["task_id"]), out_dir)
    assert report["status"] == "waiting_input"
    # 非法单价所在明细行不得形成记录；同票另一条合法明细仍可解析，
    # 但整票 waiting_input 且零产物，绝不输出准备交付文件。
    assert report["records"] == len(KIT_PARTS_A) - 1
    assert report["artifacts"] == []
    codes = {str(issue["code"]) for issue in report["issues"]}
    assert "NON_FINITE_NUMBER" in codes
    assert "INVALID_DETAIL_ROW" in codes
    assert any(
        "I12" in str(issue.get("message"))
        for issue in report["issues"]
    )
    assert sorted(path.name for path in out_dir.iterdir()) == [
        "offline_report.json"]
    assert not any(path.suffix == ".xlsx" for path in out_dir.iterdir())
    assert report["state_unchanged"] is True
    _assert_state_untouched(
        sandbox, db_before=db_before, batches_before=batches_before,
        tasks_dir_before=tasks_dir_before, mail_dir_before=mail_dir_before)


# ---------------------------------------------------------------------------
# 4. 缺参考：机型库无匹配必须 waiting_input，不得猜机型/HS 或产文件
# ---------------------------------------------------------------------------


def test_missing_machine_reference_blocks_without_artifacts(sandbox: Sandbox):
    ticket = dict(MACHINE_TICKETS[0])
    eml = _write_eml(
        sandbox, "no-reference.eml",
        make_mail(
            sender="tester@example.com",
            subject="合成缺机型参考整机票",
            text="见附件。",
            html=(
                "<html><body><table>"
                "<tr><td>MSO</td><td>Serial Number</td>"
                "<td>Total Sales Price</td><td>Dealer Code</td>"
                "<td>Dealer Order</td><td>Order</td><td>Material</td></tr>"
                f"<tr><td>{ticket['mso']}</td><td>{ticket['serial']}</td>"
                f"<td>{ticket['price']}</td><td>{ticket['dealer_code']}</td>"
                f"<td>{ticket['dealer_order']}</td><td>{ticket['order']}</td>"
                f"<td>{MACHINE_MODEL}</td></tr>"
                "</table></body></html>"
            ),
            attachments=((
                ticket["attachment"], _machine_workbook_bytes(ticket)),),
        ))

    payload, detail = _import_eml(sandbox, eml)
    assert payload["batch_ready"] is True
    tasks = _confirm_all(sandbox, detail)
    assert len(tasks) == 1
    task = tasks[0]
    sandbox.service.tasks.update_destination(
        str(task["task_id"]), DESTINATION_NAME, DESTINATION_ALPHA3)
    db_before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")
    tasks_dir_before = _tree_state(sandbox.service.config.tasks_dir)
    mail_dir_before = _tree_state(sandbox.service.config.mail_dir)

    out_dir = sandbox.root / "offline" / "no-reference"
    report = _run_offline(sandbox, str(task["task_id"]), out_dir)
    assert report["status"] == "waiting_input"
    assert report["records"] == 0
    assert report["artifacts"] == []
    codes = {str(issue["code"]) for issue in report["issues"]}
    assert "UNKNOWN_MODEL" in codes
    assert not any(path.suffix == ".xlsx" for path in out_dir.iterdir())
    assert report["state_unchanged"] is True
    _assert_state_untouched(
        sandbox, db_before=db_before, batches_before=batches_before,
        tasks_dir_before=tasks_dir_before, mail_dir_before=mail_dir_before)


# ---------------------------------------------------------------------------
# 5. 坏输入：确认后快照被替换，离线执行必须在创建输出目录前拒绝
# ---------------------------------------------------------------------------


def test_tampered_snapshot_refused_before_creating_output(sandbox: Sandbox):
    sandbox.service.references.store_machines([{
        "model": MACHINE_MODEL,
        "english_name": MACHINE_ENGLISH,
        "hs_code": MACHINE_HS,
        "product_name": MACHINE_PRODUCT,
        "declaration": MACHINE_DECLARATION,
        "destination_scope": "default",
    }], source="acceptance-synthetic", actor="acceptance")
    ticket = dict(MACHINE_TICKETS[0])
    eml = _write_eml(
        sandbox, "tamper.eml",
        make_mail(
            sender="tester@example.com",
            subject="合成快照篡改整机票",
            text="见附件。",
            html=_machine_mail_html_for((ticket,)),
            attachments=((
                ticket["attachment"], _machine_workbook_bytes(ticket)),),
        ))
    _payload, detail = _import_eml(sandbox, eml)
    tasks = _confirm_all(sandbox, detail)
    assert len(tasks) == 1
    task = tasks[0]
    sandbox.service.tasks.update_destination(
        str(task["task_id"]), DESTINATION_NAME, DESTINATION_ALPHA3)
    db_before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")
    tasks_dir_before = _tree_state(sandbox.service.config.tasks_dir)
    mail_dir_before = _tree_state(sandbox.service.config.mail_dir)

    snapshot = _task_snapshot(task)
    asset_path = Path(snapshot["assets"][0]["path"])
    original = asset_path.read_bytes()
    asset_path.write_bytes(original + b"tamper")
    try:
        out_dir = sandbox.root / "offline" / "tampered"
        result = _cli(
            sandbox, "run-batch-offline", str(task["task_id"]),
            "--output-dir", str(out_dir), "--json")
        assert result.returncode != 0
        assert not out_dir.exists(), "拒绝必须发生在创建任何输出目录之前"
        assert "快照" in (result.stdout + result.stderr)
        assert not any(
            path.suffix == ".xlsx"
            for path in sandbox.root.rglob("*.xlsx")
            if "offline" in path.parts
        )
    finally:
        asset_path.write_bytes(original)

    assert _db_state(sandbox) == db_before
    assert _tree_state(sandbox.runtime / "batches") == batches_before
    assert _tree_state(sandbox.service.config.tasks_dir) == tasks_dir_before
    assert _tree_state(sandbox.service.config.mail_dir) == mail_dir_before
    verify = sandbox.service.batches.verify_batch_task_snapshot(
        str(task["task_id"]))
    assert verify["ok"] is True


def _machine_mail_html_for(tickets: tuple[dict, ...]) -> str:
    columns = (
        "MSO", "Serial Number", "Total Sales Price",
        "Dealer Code", "Dealer Order", "Order", "Material",
    )
    header = "".join(f"<td>{name}</td>" for name in columns)
    rows = "".join(
        "<tr>"
        f"<td>{ticket['mso']}</td>"
        f"<td>{ticket['serial']}</td>"
        f"<td>{ticket['price']}</td>"
        f"<td>{ticket['dealer_code']}</td>"
        f"<td>{ticket['dealer_order']}</td>"
        f"<td>{ticket['order']}</td>"
        f"<td>{MACHINE_MODEL}</td>"
        "</tr>"
        for ticket in tickets
    )
    return (
        "<html><body><table>"
        f"<tr>{header}</tr>{rows}</table></body></html>"
    )


# ---------------------------------------------------------------------------
# 6. 真实三封：隔离 runtime 只读回放，不确认、不产 ready、不改原文件
# ---------------------------------------------------------------------------


def _real_research_cases() -> list[Path]:
    """返回同一研究批次下至少三封样本的目录；不写具体客户/批次名。"""
    if not REAL_RESEARCH_ROOT.is_dir():
        return []
    groups: dict[Path, list[Path]] = {}
    for eml in sorted(REAL_RESEARCH_ROOT.glob("*/*/original.eml")):
        groups.setdefault(eml.parent.parent, []).append(eml.parent)
    complete = [
        sorted(cases) for cases in groups.values() if len(cases) >= 3
    ]
    return complete[-1] if complete else []


def test_real_three_emails_read_only_replay(sandbox: Sandbox):
    cases = _real_research_cases()
    if len(cases) < 3:
        pytest.skip("真实研究邮件不足三封（gitignored 本地材料），跳过只读回放")

    for case_dir in cases:
        case_name = case_dir.name
        eml = case_dir / "original.eml"
        before = _file_fingerprint(eml)
        payload, detail = _import_eml(sandbox, eml)
        after = _file_fingerprint(eml)
        assert after == before, f"{case_name}/original.eml 被改写了"

        assert payload["batch_ready"] in (True, False)
        assert detail["batch"]["status"] == "open"
        assert detail["tasks"] == []
        preview = detail["preview"]
        assert preview is not None
        assert int(preview["candidate_count"]) >= 1
        # 回放绝不替业务确认：所有候选仍是 pending，零 batch_tasks。
        assert all(
            str(row["decision"]) == "pending"
            for row in detail["candidate_rows"]
        ), f"{case_name} 回放中出现已确认候选"
        batch_dir = sandbox.runtime / "batches" / detail["batch"]["id"]
        assert not list(batch_dir.rglob("snapshots")), (
            f"{case_name} 回放生成了已确认快照目录")

    assert sandbox.service.db.scalar(
        "SELECT COUNT(*) FROM tasks") == 0
    assert sandbox.service.db.scalar(
        "SELECT COUNT(*) FROM batch_tasks") == 0
    assert sandbox.service.db.scalar(
        "SELECT COUNT(*) FROM outbox") == 0
    assert not list(sandbox.service.config.tasks_dir.rglob("*.xlsx"))
    assert not list(sandbox.root.rglob("offline_report.json"))
