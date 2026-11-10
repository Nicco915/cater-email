"""阶段 4 独立端到端验收：已物化批次的每票目的国确认（全合成材料）。

本文件只验收，不实现、不修改其他 worker 的代码或测试。黑盒口径：只通过
真实用户入口驱动——

- CLI 子进程：``import-batch`` / ``batch-destinations`` /
  ``confirm-batch-destinations`` / ``run-batch-offline``（与操作员同一命令，
  无 .env、清除全部 CATER_* 环境变量）；
- Web TestClient：``POST /batches/{id}/confirm``（阶段 2 既有确认边界）、
  ``GET/POST /batches/{id}/destinations``（阶段 4 新增逐票确认页）。

绝不使用直接 SQL 写入或 ``TaskRepository.update_destination`` 来模拟
“已确认”；数据库仅用于只读核对（与阶段 3 验收同一口径），快照/产物以
字节哈希核对。目的国字段与明确的逐票/批次审计事件是本阶段授权写入，
其余正式状态（status/version/execution_hold/outbox/快照/未选票）必须不变。

覆盖（对应 plan/tasks/stage4/common.md 的验收要求）：

1. 整机两票经 Web 逐票确认不同目的国（FRA/DEU）→ 显式离线执行 → 重开
   SEM发运沟通表 V 列核对最终目的国；报告 ``destination`` 字段可追溯到
   确认事件（revision/confirmed_by/confirmed_at）；别票国家不混入。
2. 配件两票经 CLI 一次批量同国（两个 --task-id + 一个 --country）→
   出口台账 N 列为 FRA；报告 destination 可追溯；修订为真实事件 id。
3. 只确认选中票：未选票目的国保持空、修订 0、零审计事件，离线执行仍
   waiting_input(MISSING_DESTINATION)、零产物。
4. 改国后：新目录离线结果与报告为新值/新修订；旧产物/快照/旧报告
   逐字节不变。
5. 非法确认整体零写入：未知国、跨批次任务、部分非法（全成或全不成）、
   过期修订、伪造修订、逐票国家与统一国家同时提交。
6. Web 安全：无/错 CSRF、跨站/null Origin 一律 403 且零写入；未知批次
   404；批次详情页有逐票确认入口。
7. 未确认目的国时离线执行保持 waiting_input(MISSING_DESTINATION)，
   绝不自动确认或按正文猜国。

所有业务数据均为 ``SYN4*`` 假值；不读取 .env/钥匙串，不联网，不发信。
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
from fastapi.testclient import TestClient
from openpyxl import Workbook

from cater.app.application import AppService
from cater.app.engine import CoreProcessEngine
from cater.app.mail.simulator import SimulatorMailGateway
from cater.app.web.app import create_app
from cater.app.web.security import COOKIE_NAME, CSRF_FIELD

from helpers import make_config, make_mail

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "templates"
BASE_URL = "http://127.0.0.1:8765"

pytestmark = pytest.mark.skipif(
    not (TEMPLATE_DIR / "machine_materials.xlsx").is_file()
    or not (TEMPLATE_DIR / "machine_comm.xlsx").is_file()
    or not (TEMPLATE_DIR / "parts_ledger.xlsx").is_file(),
    reason="仓库模板缺失，端到端验收无法生成模板文件",
)

DESTINATION_EVENT_CODE = "batch_destination_confirmed"

# ---------------------------------------------------------------------------
# 合成业务材料（全部为假数据）
# ---------------------------------------------------------------------------

MACHINE_MODEL = "SYN4LOADER"
MACHINE_HS = "8429510000"
MACHINE_PRODUCT = "合成装载机"
MACHINE_DECLARATION = "1|0|合成装载机|是自推进|100KG|合成品牌|SYN4LOADER|无|无|无"
MACHINE_ENGLISH = "SYNTHETIC LOADER FOUR"

MACHINE_TICKETS = (
    {
        "key": "A",
        "invoice_no": "SYN4-M-INV-A01",
        "mso": "SYN4MA1",
        "serial": "SYN4SN0001",
        "pin": "SYN4PINSYN4SN0001",
        "engine": "SYN4ENG0001",
        "price": "1000",
        "net": "100",
        "gross": "110",
        "dealer_code": "SYN4DCA1",
        "dealer_order": "SYN4DOA1",
        "order": "SYN4-SO-A1",
        "attachment": "machine-a.xlsx",
        "country": "FRA",
        "country_name": "法国",
    },
    {
        "key": "B",
        "invoice_no": "SYN4-M-INV-B02",
        "mso": "SYN4MB2",
        "serial": "SYN4SN0002",
        "pin": "SYN4PINSYN4SN0002",
        "engine": "SYN4ENG0002",
        "price": "2000",
        "net": "200",
        "gross": "220",
        "dealer_code": "SYN4DCB2",
        "dealer_order": "SYN4DOB2",
        "order": "SYN4-SO-B2",
        "attachment": "machine-b.xlsx",
        "country": "DEU",
        "country_name": "德国",
    },
)

KIT_PARTS_A = (
    {
        "kit_no": 9800001, "part_code": "SYN4-P100", "hs": "84219900",
        "coo": "US", "qty_per_set": 4, "sets": 3,
        "unit_price": "28.11", "unit_weight": "0.9",
        "name": "合成滤芯肆", "spec": "1|合成申报要素4A",
    },
    {
        "kit_no": 9800001, "part_code": "SYN4-B200", "hs": "73181510",
        "coo": "CN", "qty_per_set": 40, "sets": 3,
        "unit_price": "1.25", "unit_weight": "0.21",
        "name": "合成螺栓肆", "spec": "1|合成申报要素4B",
    },
)
KIT_PARTS_B = (
    {
        "kit_no": 9800002, "part_code": "SYN4-Q900", "hs": "84821000",
        "coo": "US", "qty_per_set": 4, "sets": 2,
        "unit_price": "30", "unit_weight": "1.0",
        "name": "合成轴承肆", "spec": "1|合成申报要素4C",
    },
    {
        "kit_no": 9800002, "part_code": "SYN4-R800", "hs": "73181510",
        "coo": "CN", "qty_per_set": 10, "sets": 2,
        "unit_price": "2.5", "unit_weight": "0.5",
        "name": "合成垫圈肆", "spec": "1|合成申报要素4D",
    },
)

KIT_TICKETS = (
    {
        "key": "A",
        "invoice_no": "SYN4-K-INV-A01",
        "mso": "SYN4KA",
        "serial": "SYN4KSN0001",
        "order_code": "SYN4QMA",
        "parts": KIT_PARTS_A,
        "sets": 3,
        "packages": 2,
        "base_gross": "100",
        "tare": "10",
        "attachment": "kit-a.xlsx",
    },
    {
        "key": "B",
        "invoice_no": "SYN4-K-INV-B02",
        "mso": "SYN4KB",
        "serial": "SYN4KSN0002",
        "order_code": "SYN4QMB",
        "parts": KIT_PARTS_B,
        "sets": 2,
        "packages": 3,
        "base_gross": "150",
        "tare": "12",
        "attachment": "kit-b.xlsx",
    },
)

KIT_FEE = Decimal("10")
KIT_BULK_COUNTRY = "FRA"
KIT_BULK_COUNTRY_NAME = "法国"


# ---------------------------------------------------------------------------
# 合成工作簿构造（独立实现，不复用被验收 worker 的测试构造器）
# ---------------------------------------------------------------------------


def _workbook_bytes(wb: Workbook) -> bytes:
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _num(value) -> Decimal:
    return Decimal(str(value))


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
    ws.cell(9, 2, "SYN4-PO-0001")
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
        subject="阶段4合成两票整机发票",
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
        subject="阶段4合成两票配件发票",
        text="见附件两套配件发票/箱单。",
        html="<html><body><p>两张配件票，请见附件。</p></body></html>",
        attachments=attachments,
    )


# ---------------------------------------------------------------------------
# 隔离运行时、CLI 子进程与 Web 客户端
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


def _web_client(sandbox: Sandbox) -> TestClient:
    return TestClient(create_app(sandbox.service), base_url=BASE_URL)


def _csrf(client: TestClient) -> str:
    cookie = client.cookies.get(COOKIE_NAME)
    assert cookie
    session = client.app.state.session_signer.load(cookie)
    return str(session.get("csrf"))


def _write_eml(sandbox: Sandbox, name: str, payload: bytes) -> Path:
    path = sandbox.root / name
    path.write_bytes(payload)
    return path


def _import_eml(sandbox: Sandbox, path: Path) -> tuple[dict, dict]:
    result = _cli(sandbox, "import-batch", "--eml", str(path), "--json")
    assert result.returncode == 0, (
        f"import-batch 失败 rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}")
    payload = json.loads(result.stdout)
    detail = sandbox.service.batches.batch_detail(payload["batch_id"])
    return payload, detail


def _store_machine_reference(sandbox: Sandbox) -> None:
    sandbox.service.references.store_machines([{
        "model": MACHINE_MODEL,
        "english_name": MACHINE_ENGLISH,
        "hs_code": MACHINE_HS,
        "product_name": MACHINE_PRODUCT,
        "declaration": MACHINE_DECLARATION,
        "destination_scope": "default",
    }], source="stage4-acceptance-synthetic", actor="stage4-acceptance")


# ---------------------------------------------------------------------------
# 阶段 2 既有确认边界（Web POST /batches/{id}/confirm）：建票挂起
# ---------------------------------------------------------------------------


def _web_confirm_batch(
    sandbox: Sandbox, client: TestClient, detail: dict,
) -> list[dict]:
    preview = detail["preview"]
    assert preview is not None
    assert preview["batch_ready"] is True, preview.get("issues")
    keys = [str(candidate["candidate_id"]) for candidate in preview["candidates"]]
    batch_id = detail["batch"]["id"]
    client.get(f"/batches/{batch_id}")  # 建立会话
    response = client.post(
        f"/batches/{batch_id}/confirm",
        data={
            CSRF_FIELD: _csrf(client),
            "plan_id": detail["plan"]["id"],
            "fingerprint": detail["plan"]["source_fingerprint"],
            "selected": keys,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:500]
    assert "error=" not in response.headers["location"], (
        response.headers["location"])
    tasks = sandbox.service.batches.batch_tasks(batch_id)
    assert len(tasks) == len(keys)
    for task in tasks:
        assert int(task["execution_hold"]) == 1
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


# ---------------------------------------------------------------------------
# 阶段 4 用户入口：CLI 只读视图 / CLI 确认 / Web 逐票确认
# ---------------------------------------------------------------------------


def _cli_destinations(sandbox: Sandbox, batch_id: str) -> dict:
    """CLI batch-destinations（只读），返回解析后的 JSON。"""
    result = _cli(sandbox, "batch-destinations", batch_id, "--json")
    assert result.returncode == 0, (
        f"batch-destinations 失败 rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}")
    return json.loads(result.stdout)


def _cli_confirm_destinations(
    sandbox: Sandbox,
    batch_id: str,
    task_ids: list[str],
    country: str,
    reason: str,
) -> subprocess.CompletedProcess[str]:
    args = [
        "confirm-batch-destinations", batch_id,
        "--country", country, "--reason", reason,
    ]
    for task_id in task_ids:
        args += ["--task-id", task_id]
    return _cli(sandbox, *args, "--json")


def _web_confirm_destinations(
    client: TestClient,
    batch_id: str,
    *,
    plan_id: str,
    reference_version: int,
    updates: list[dict],
    reason: str,
    bulk_country: str = "",
    csrf: str | None = None,
    origin: str | None = None,
    extra: dict | None = None,
) -> object:
    """按渲染页面的真实表单契约 POST /batches/{id}/destinations。

    每张选票一组对齐字段：selected=task_id（重复键）、country_<task_id>、
    revision_<task_id>；批次级字段 plan_id/reference_version/bulk_country/
    reason；CSRF 令牌来自既有会话。注意：本环境的 httpx 只把 dict 数据
    （列表值表示重复键）编码为 urlencoded 表单，list-of-tuples 会被当作
    原始 content，切勿改回元组列表。
    """
    token = _csrf(client) if csrf is None else csrf
    data: dict[str, object] = {
        CSRF_FIELD: token,
        "plan_id": plan_id,
        "reference_version": str(reference_version),
        "reason": reason,
        "bulk_country": bulk_country,
        "selected": [str(update["task_id"]) for update in updates],
    }
    for update in updates:
        task_id = str(update["task_id"])
        data[f"country_{task_id}"] = str(update.get("country", ""))
        data[f"revision_{task_id}"] = str(update.get("revision", 0))
    if extra:
        data.update(extra)
    headers = {"Origin": origin} if origin is not None else {}
    return client.post(
        f"/batches/{batch_id}/destinations",
        data=data, headers=headers, follow_redirects=False,
    )


def _assert_redirect_success(response, batch_id: str) -> None:
    assert response.status_code == 303, response.text[:500]
    location = response.headers["location"]
    assert "error=" not in location, location
    assert f"/batches/{batch_id}/destinations" in location


def _assert_redirect_rejected(response) -> None:
    assert response.status_code == 303, response.text[:500]
    assert "error=" in response.headers["location"], (
        response.headers["location"])


# ---------------------------------------------------------------------------
# 只读核对工具（数据库只读、文件字节哈希）
# ---------------------------------------------------------------------------


def _db_state(sandbox: Sandbox) -> dict[str, list[dict]]:
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


def _destination_events(sandbox: Sandbox, task_id: str) -> list[dict]:
    rows = sandbox.service.db.fetchall(
        "SELECT * FROM task_events WHERE task_id = ? AND code = ? ORDER BY id",
        (task_id, DESTINATION_EVENT_CODE),
    )
    return [dict(row) for row in rows]


def _destination_revision(sandbox: Sandbox, task_id: str) -> int:
    events = _destination_events(sandbox, task_id)
    return int(events[-1]["id"]) if events else 0


def _assert_only_destination_writes(
    before: dict[str, list[dict]],
    after: dict[str, list[dict]],
    *,
    changed_task_ids: set[str],
) -> None:
    """除“所选票目的国 + 明确审计事件”外，正式数据不得有任何变化。"""
    for table, before_rows in before.items():
        after_rows = after[table]
        if table == "tasks":
            assert len(after_rows) == len(before_rows), "tasks 行数变了"
            for old, new in zip(before_rows, after_rows):
                assert old["id"] == new["id"]
                if old["id"] in changed_task_ids:
                    allowed = {
                        "destination_name", "destination_alpha3", "updated_at"}
                    changed = {k for k in old if old[k] != new[k]}
                    assert changed <= allowed, (
                        f"tasks {old['id']} 出现非授权字段变化：{changed}")
                    for key in (
                        "status", "version", "next_retry_at", "deleted_at",
                        "archived_at",
                    ):
                        assert old[key] == new[key], (
                            f"tasks {old['id']} 的 {key} 被改动")
                else:
                    assert old == new, f"未选票 tasks {old['id']} 被改动"
        elif table == "task_versions":
            assert len(after_rows) == len(before_rows), (
                "task_versions 行数变了（不得产生新版本）")
            for old, new in zip(before_rows, after_rows):
                if old["task_id"] in changed_task_ids:
                    changed = {k for k in old if old[k] != new[k]}
                    assert changed <= {
                        "destination_name", "destination_alpha3"}, (
                        f"task_versions 出现非授权字段变化：{changed}")
                else:
                    assert old == new, "未选票 task_versions 被改动"
        elif table == "task_events":
            assert after_rows[: len(before_rows)] == before_rows, (
                "既有 task_events 被改写")
            new_rows = after_rows[len(before_rows):]
            for row in new_rows:
                assert row["code"] == DESTINATION_EVENT_CODE, (
                    f"出现非目的国审计事件：{row['code']}")
                assert row["task_id"] in changed_task_ids, (
                    "审计事件落在未选票上")
            assert len(new_rows) == len(changed_task_ids), (
                "每张改值票必须恰好新增一条逐票审计事件")
        elif table == "batch_events":
            assert after_rows[: len(before_rows)] == before_rows, (
                "既有 batch_events 被改写")
            for row in after_rows[len(before_rows):]:
                assert row["action"] == DESTINATION_EVENT_CODE, (
                    f"出现非目的国批次事件：{row['action']}")
        else:
            assert after_rows == before_rows, f"{table} 被改动（非授权写入）"
    assert after["outbox"] == [], "outbox 必须保持为空"
    assert after["batch_tasks"] == before["batch_tasks"], (
        "execution_hold/批次关联不得改变")


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


def _offline_run_isolated(sandbox: Sandbox, task_id: str, out_dir: Path) -> dict:
    """显式离线执行并断言执行前后正式状态/快照/生产目录逐字节不变。"""
    db_before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")
    tasks_dir_before = _tree_state(sandbox.service.config.tasks_dir)
    mail_dir_before = _tree_state(sandbox.service.config.mail_dir)
    report = _run_offline(sandbox, task_id, out_dir)
    _assert_state_untouched(
        sandbox, db_before=db_before, batches_before=batches_before,
        tasks_dir_before=tasks_dir_before, mail_dir_before=mail_dir_before)
    return report


def _cell_texts(worksheet) -> str:
    return "\n".join(
        str(cell.value)
        for row in worksheet.iter_rows()
        for cell in row
        if cell.value is not None
    )


def _comm_sheet(report: dict, out_dir: Path):
    artifacts = {item["filename"]: item for item in report["artifacts"]}
    artifact = artifacts["SEM发运沟通表 上传.xlsx"]
    path = Path(artifact["path"])
    assert path.is_file()
    assert path.resolve().parent == out_dir.resolve()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    return openpyxl.load_workbook(path, data_only=True)["Sheet1"]


def _ledger_sheet(report: dict, out_dir: Path):
    artifacts = {item["filename"]: item for item in report["artifacts"]}
    artifact = artifacts["出口台账表体.xlsx"]
    path = Path(artifact["path"])
    assert path.is_file()
    assert path.resolve().parent == out_dir.resolve()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    return openpyxl.load_workbook(path, data_only=True)["导入模板"]


def _assert_report_destination(
    report: dict,
    *,
    alpha3: str,
    name: str,
    revision: int,
    confirmed_by: str,
) -> None:
    destination = report.get("destination")
    assert isinstance(destination, dict), (
        f"报告缺 destination 追溯字段：{sorted(report)}")
    assert destination["alpha3"] == alpha3
    assert destination["name"] == name
    assert destination["revision"] == revision
    assert destination["confirmed_by"] == confirmed_by
    assert destination["confirmed_at"], "报告缺确认时间"


# ---------------------------------------------------------------------------
# 1. 整机两票：Web 逐票确认不同国家 → 离线执行 → 重开模板核对
# ---------------------------------------------------------------------------


def test_machine_two_tickets_web_confirm_different_countries(sandbox: Sandbox):
    _store_machine_reference(sandbox)
    eml = _write_eml(sandbox, "machines.eml", _machine_eml())
    payload, detail = _import_eml(sandbox, eml)
    assert payload["batch_ready"] is True
    assert detail["preview"]["candidate_count"] == 2
    batch_id = detail["batch"]["id"]

    with _web_client(sandbox) as client:
        tasks = _web_confirm_batch(sandbox, client, detail)
    by_invoice = _map_by_invoice(tasks)
    task_a = by_invoice[MACHINE_TICKETS[0]["invoice_no"]]
    task_b = by_invoice[MACHINE_TICKETS[1]["invoice_no"]]
    id_a, id_b = str(task_a["task_id"]), str(task_b["task_id"])

    # 建票后未确认目的国：两票为空、修订 0、仍挂起（不自动确认）。
    view = _cli_destinations(sandbox, batch_id)
    rows = {str(row["task_id"]): row for row in view["rows"]}
    assert set(rows) == {id_a, id_b}
    for task_id in (id_a, id_b):
        assert rows[task_id]["destination_alpha3"] in (None, "")
        assert rows[task_id]["revision"] == 0
        assert rows[task_id]["confirmed_by"] is None
        assert int(rows[task_id]["execution_hold"]) == 1
        assert rows[task_id]["task_status"] == "RECEIVED"
    plan_id = str(view["plan_id"])
    reference_version = int(view["reference_version"])

    db_before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")

    reason = "阶段4验收：客户要求两票分发法国/德国（合成）"
    with _web_client(sandbox) as client:
        page = client.get(f"/batches/{batch_id}/destinations")
        assert page.status_code == 200
        # 页面按真实表单契约渲染：逐票国家选择 + 修订隐藏字段 + 统一国家。
        assert f'name="country_{id_a}"' in page.text
        assert f'name="country_{id_b}"' in page.text
        assert f'name="revision_{id_a}"' in page.text
        assert f'name="revision_{id_b}"' in page.text
        assert 'name="bulk_country"' in page.text
        assert "法国（FRA）" in page.text
        assert "德国（DEU）" in page.text
        response = _web_confirm_destinations(
            client, batch_id,
            plan_id=plan_id, reference_version=reference_version,
            updates=[
                {"task_id": id_a, "country": "FRA", "revision": 0},
                {"task_id": id_b, "country": "DEU", "revision": 0},
            ],
            reason=reason,
        )
        _assert_redirect_success(response, batch_id)

    # 授权写入仅限所选票目的国与审计事件；hold/状态/版本/outbox/快照不变。
    _assert_only_destination_writes(
        db_before, _db_state(sandbox), changed_task_ids={id_a, id_b})
    assert _tree_state(sandbox.runtime / "batches") == batches_before

    revision_a = _destination_revision(sandbox, id_a)
    revision_b = _destination_revision(sandbox, id_b)
    assert revision_a > 0 and revision_b > 0 and revision_a != revision_b

    # 逐票审计：前后值、理由、参考版本、选择范围齐全（只读核对）。
    for task_id, alpha3, revision in (
        (id_a, "FRA", revision_a), (id_b, "DEU", revision_b),
    ):
        events = _destination_events(sandbox, task_id)
        assert len(events) == 1
        assert int(events[0]["id"]) == revision
        assert events[0]["actor"] == "web-admin"
        event_detail = json.loads(events[0]["detail_json"])
        assert event_detail["previous_destination_alpha3"] in (None, "")
        assert event_detail["destination_alpha3"] == alpha3
        assert event_detail["reason"] == reason
        assert set(event_detail["selection_scope"]) == {id_a, id_b}

    # CLI 只读视图反映确认结果（确认人 web-admin，修订为真实事件 id）。
    view = _cli_destinations(sandbox, batch_id)
    rows = {str(row["task_id"]): row for row in view["rows"]}
    assert rows[id_a]["destination_alpha3"] == "FRA"
    assert rows[id_a]["destination_name"] == "法国"
    assert int(rows[id_a]["revision"]) == revision_a
    assert rows[id_a]["confirmed_by"] == "web-admin"
    assert rows[id_b]["destination_alpha3"] == "DEU"
    assert rows[id_b]["destination_name"] == "德国"
    assert int(rows[id_b]["revision"]) == revision_b

    # 显式离线执行：报告 destination 可追溯；重开沟通表核对最终目的国。
    for ticket, task_id, revision in (
        (MACHINE_TICKETS[0], id_a, revision_a),
        (MACHINE_TICKETS[1], id_b, revision_b),
    ):
        out_dir = sandbox.root / "offline" / f"machine-{ticket['key']}"
        report = _offline_run_isolated(sandbox, task_id, out_dir)
        assert report["status"] == "ready", report.get("issues")
        assert report["kind"] == "machine"
        assert report["state_unchanged"] is True
        assert report["task_state_after"]["execution_hold"] is True
        assert report["task_state_after"]["status"] == "RECEIVED"
        assert report["task_state_after"]["version"] == 1
        _assert_report_destination(
            report,
            alpha3=ticket["country"], name=ticket["country_name"],
            revision=revision, confirmed_by="web-admin",
        )
        comm = _comm_sheet(report, out_dir)
        assert comm["A3"].value == ticket["dealer_code"]
        assert comm["H3"].value == ticket["serial"]
        assert comm["V3"].value == ticket["country_name"]
        # 别票国家与标识绝不混入本票产物。
        other = next(
            item for item in MACHINE_TICKETS
            if item["key"] != ticket["key"])
        text = _cell_texts(comm)
        for token in (
            other["country_name"], other["country"],
            other["mso"], other["serial"], other["invoice_no"],
        ):
            assert token not in text, f"本票沟通表混入别票标识：{token}"


# ---------------------------------------------------------------------------
# 2. 配件两票：CLI 批量同国（两个 --task-id 一个 --country）→ 台账核对
# ---------------------------------------------------------------------------


def test_kit_two_tickets_cli_confirm_bulk_same_country(sandbox: Sandbox):
    eml = _write_eml(sandbox, "kits.eml", _kit_eml())
    payload, detail = _import_eml(sandbox, eml)
    assert payload["batch_ready"] is True
    batch_id = detail["batch"]["id"]
    with _web_client(sandbox) as client:
        tasks = _web_confirm_batch(sandbox, client, detail)
    by_invoice = _map_by_invoice(tasks)
    id_a = str(by_invoice[KIT_TICKETS[0]["invoice_no"]]["task_id"])
    id_b = str(by_invoice[KIT_TICKETS[1]["invoice_no"]]["task_id"])

    # 只读视图：确认前两票皆空、修订 0。
    view = _cli_destinations(sandbox, batch_id)
    rows = {str(row["task_id"]): row for row in view["rows"]}
    assert all(rows[tid]["revision"] == 0 for tid in (id_a, id_b))

    db_before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")

    reason = "阶段4验收：配件两票同发法国（合成）"
    result = _cli_confirm_destinations(
        sandbox, batch_id, [id_a, id_b], KIT_BULK_COUNTRY, reason)
    assert result.returncode == 0, (
        f"confirm-batch-destinations 失败 rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}")
    confirmed = {
        item["task_id"]: item for item in json.loads(result.stdout)["confirmed"]
    }
    assert set(confirmed) == {id_a, id_b}

    _assert_only_destination_writes(
        db_before, _db_state(sandbox), changed_task_ids={id_a, id_b})
    assert _tree_state(sandbox.runtime / "batches") == batches_before

    revision_a = _destination_revision(sandbox, id_a)
    revision_b = _destination_revision(sandbox, id_b)
    assert revision_a > 0 and revision_b > 0 and revision_a != revision_b
    assert int(confirmed[id_a]["revision"]) == revision_a
    assert int(confirmed[id_b]["revision"]) == revision_b
    assert confirmed[id_a]["destination_alpha3"] == KIT_BULK_COUNTRY
    assert confirmed[id_a]["confirmed_by"] == "cli"

    for ticket, task_id, revision in (
        (KIT_TICKETS[0], id_a, revision_a),
        (KIT_TICKETS[1], id_b, revision_b),
    ):
        out_dir = sandbox.root / "offline" / f"kit-{ticket['key']}"
        report = _offline_run_isolated(sandbox, task_id, out_dir)
        assert report["status"] == "ready", report.get("issues")
        assert report["kind"] == "parts"
        assert report["records"] == len(ticket["parts"])
        assert report["state_unchanged"] is True
        _assert_report_destination(
            report,
            alpha3=KIT_BULK_COUNTRY, name=KIT_BULK_COUNTRY_NAME,
            revision=revision, confirmed_by="cli",
        )
        sheet = _ledger_sheet(report, out_dir)
        for index, part in enumerate(ticket["parts"]):
            row = 6 + index
            assert sheet.cell(row, 2).value == part["part_code"]
            assert sheet.cell(row, 14).value == KIT_BULK_COUNTRY  # 最终目的国
        assert sheet.cell(6, 27).value == ticket["invoice_no"]
        # 别票料号不混入本票台账。
        other = next(
            item for item in KIT_TICKETS if item["key"] != ticket["key"])
        text = _cell_texts(sheet)
        for token in (
            other["invoice_no"], other["mso"],
            *(part["part_code"] for part in other["parts"]),
        ):
            assert token not in text, f"本票台账混入别票标识：{token}"


# ---------------------------------------------------------------------------
# 3. 只确认选中票：未选票保持空白/修订 0/零审计，离线仍缺国阻断
# ---------------------------------------------------------------------------


def test_partial_selection_leaves_unselected_untouched(sandbox: Sandbox):
    _store_machine_reference(sandbox)
    eml = _write_eml(sandbox, "machines.eml", _machine_eml())
    _payload, detail = _import_eml(sandbox, eml)
    batch_id = detail["batch"]["id"]
    with _web_client(sandbox) as client:
        tasks = _web_confirm_batch(sandbox, client, detail)
    by_invoice = _map_by_invoice(tasks)
    id_a = str(by_invoice[MACHINE_TICKETS[0]["invoice_no"]]["task_id"])
    id_b = str(by_invoice[MACHINE_TICKETS[1]["invoice_no"]]["task_id"])

    db_before = _db_state(sandbox)
    result = _cli_confirm_destinations(
        sandbox, batch_id, [id_a], "FRA", "阶段4验收：仅确认首票（合成）")
    assert result.returncode == 0, result.stderr

    # 只写选中票；未选票 B 完全不变：空目的国、修订 0、零审计事件。
    _assert_only_destination_writes(
        db_before, _db_state(sandbox), changed_task_ids={id_a})
    task_b = sandbox.service.tasks.get(id_b)
    assert task_b["destination_name"] is None
    assert task_b["destination_alpha3"] is None
    assert _destination_events(sandbox, id_b) == []
    view = _cli_destinations(sandbox, batch_id)
    rows = {str(row["task_id"]): row for row in view["rows"]}
    assert rows[id_b]["destination_alpha3"] in (None, "")
    assert rows[id_b]["revision"] == 0
    assert rows[id_a]["destination_alpha3"] == "FRA"
    assert int(rows[id_a]["revision"]) == _destination_revision(sandbox, id_a)

    # 选中票离线 ready；未选票离线仍 waiting_input(MISSING_DESTINATION)、
    # 零产物，且两次执行都不改正式状态。
    out_a = sandbox.root / "offline" / "partial-a"
    report_a = _offline_run_isolated(sandbox, id_a, out_a)
    assert report_a["status"] == "ready", report_a.get("issues")
    _assert_report_destination(
        report_a, alpha3="FRA", name="法国",
        revision=_destination_revision(sandbox, id_a), confirmed_by="cli")
    assert _comm_sheet(report_a, out_a)["V3"].value == "法国"

    out_b = sandbox.root / "offline" / "partial-b"
    report_b = _offline_run_isolated(sandbox, id_b, out_b)
    assert report_b["status"] == "waiting_input"
    codes = {str(issue["code"]) for issue in report_b["issues"]}
    assert "MISSING_DESTINATION" in codes
    assert report_b["artifacts"] == []
    assert not any(path.suffix == ".xlsx" for path in out_b.iterdir())
    assert report_b["state_unchanged"] is True


# ---------------------------------------------------------------------------
# 4. 改国后：新目录新值可追溯，旧产物/快照/旧报告逐字节不变
# ---------------------------------------------------------------------------


def test_change_country_new_output_old_artifacts_byte_identical(
        sandbox: Sandbox):
    _store_machine_reference(sandbox)
    eml = _write_eml(sandbox, "machines.eml", _machine_eml())
    _payload, detail = _import_eml(sandbox, eml)
    batch_id = detail["batch"]["id"]
    with _web_client(sandbox) as client:
        tasks = _web_confirm_batch(sandbox, client, detail)
    by_invoice = _map_by_invoice(tasks)
    ticket = MACHINE_TICKETS[0]
    id_a = str(by_invoice[ticket["invoice_no"]]["task_id"])

    result = _cli_confirm_destinations(
        sandbox, batch_id, [id_a], "FRA", "阶段4验收：首次确认法国（合成）")
    assert result.returncode == 0, result.stderr
    revision_first = _destination_revision(sandbox, id_a)
    assert revision_first > 0

    out_first = sandbox.root / "offline" / "change-first"
    report_first = _offline_run_isolated(sandbox, id_a, out_first)
    assert report_first["status"] == "ready", report_first.get("issues")
    _assert_report_destination(
        report_first, alpha3="FRA", name="法国",
        revision=revision_first, confirmed_by="cli")
    assert _comm_sheet(report_first, out_first)["V3"].value == "法国"

    # 冻结旧现场：旧目录全部文件字节、快照文件、批次目录。
    old_files = {
        path.name: path.read_bytes() for path in sorted(out_first.iterdir())}
    snapshot_file = Path(by_invoice[ticket["invoice_no"]]["snapshot_path"])
    snapshot_before = _file_fingerprint(snapshot_file)
    batches_before = _tree_state(sandbox.runtime / "batches")
    db_before = _db_state(sandbox)

    # 通过 Web 改国：携带当前修订（第一次确认的事件 id）。
    view = _cli_destinations(sandbox, batch_id)
    rows = {str(row["task_id"]): row for row in view["rows"]}
    assert int(rows[id_a]["revision"]) == revision_first
    with _web_client(sandbox) as client:
        client.get(f"/batches/{batch_id}/destinations")
        response = _web_confirm_destinations(
            client, batch_id,
            plan_id=str(view["plan_id"]),
            reference_version=int(view["reference_version"]),
            updates=[{
                "task_id": id_a, "country": "DEU",
                "revision": revision_first,
            }],
            reason="阶段4验收：客户改发德国（合成）",
        )
        _assert_redirect_success(response, batch_id)

    _assert_only_destination_writes(
        db_before, _db_state(sandbox), changed_task_ids={id_a})
    revision_second = _destination_revision(sandbox, id_a)
    assert revision_second > revision_first
    events = _destination_events(sandbox, id_a)
    assert len(events) == 2
    change_detail = json.loads(events[-1]["detail_json"])
    assert change_detail["previous_destination_alpha3"] == "FRA"
    assert change_detail["destination_alpha3"] == "DEU"
    assert change_detail["expected_revision"] == revision_first

    # 新目录离线执行：报告与沟通表为新值/新修订。
    out_second = sandbox.root / "offline" / "change-second"
    report_second = _offline_run_isolated(sandbox, id_a, out_second)
    assert report_second["status"] == "ready", report_second.get("issues")
    _assert_report_destination(
        report_second, alpha3="DEU", name="德国",
        revision=revision_second, confirmed_by="web-admin")
    assert _comm_sheet(report_second, out_second)["V3"].value == "德国"

    # 旧产物/旧报告/快照/批次目录逐字节不变（离线不回写任何旧现场）。
    for name, payload in old_files.items():
        assert (out_first / name).read_bytes() == payload, (
            f"旧输出文件被改写：{name}")
    assert _file_fingerprint(snapshot_file) == snapshot_before, (
        "改国后快照文件被改写")
    assert _tree_state(sandbox.runtime / "batches") == batches_before


# ---------------------------------------------------------------------------
# 5. 非法确认：整体零写入（未知国/跨批次/部分非法/过期与伪造修订）
# ---------------------------------------------------------------------------


def test_invalid_confirmations_roll_back_everything(sandbox: Sandbox):
    _store_machine_reference(sandbox)
    eml_one = _write_eml(sandbox, "batch-one.eml", _machine_eml())
    _payload_one, detail_one = _import_eml(sandbox, eml_one)
    batch_one = detail_one["batch"]["id"]
    # 第二批次：同一封合成邮件改 Message-ID 后再导入（make_mail 每次新 id）。
    eml_two = _write_eml(sandbox, "batch-two.eml", _machine_eml())
    _payload_two, detail_two = _import_eml(sandbox, eml_two)
    batch_two = detail_two["batch"]["id"]
    assert batch_one != batch_two

    with _web_client(sandbox) as client:
        tasks_one = _web_confirm_batch(sandbox, client, detail_one)
        tasks_two = _web_confirm_batch(sandbox, client, detail_two)
    by_one = _map_by_invoice(tasks_one)
    by_two = _map_by_invoice(tasks_two)
    id_a = str(by_one[MACHINE_TICKETS[0]["invoice_no"]]["task_id"])
    id_cross = str(by_two[MACHINE_TICKETS[0]["invoice_no"]]["task_id"])

    before = _db_state(sandbox)
    batches_before = _tree_state(sandbox.runtime / "batches")

    # (a) 未知国家：拒绝且零写入。
    result = _cli_confirm_destinations(
        sandbox, batch_one, [id_a], "ZZZ", "阶段4验收：未知国（合成）")
    assert result.returncode != 0
    assert _db_state(sandbox) == before

    # (b) 跨批次任务：拒绝且零写入。
    result = _cli_confirm_destinations(
        sandbox, batch_one, [id_cross], "FRA", "阶段4验收：跨批次（合成）")
    assert result.returncode != 0
    assert _db_state(sandbox) == before

    # (c) 部分非法（本批一票 + 跨批次一票）：全成或全不成，本批票也不写。
    result = _cli_confirm_destinations(
        sandbox, batch_one, [id_a, id_cross], "FRA",
        "阶段4验收：部分非法（合成）")
    assert result.returncode != 0
    assert _db_state(sandbox) == before
    assert _destination_events(sandbox, id_a) == []

    assert _tree_state(sandbox.runtime / "batches") == batches_before

    # (d) 合法确认 A→FRA 成功（Web），作为过期/伪造修订的基线。
    view = _cli_destinations(sandbox, batch_one)
    with _web_client(sandbox) as client:
        client.get(f"/batches/{batch_one}/destinations")
        response = _web_confirm_destinations(
            client, batch_one,
            plan_id=str(view["plan_id"]),
            reference_version=int(view["reference_version"]),
            updates=[{"task_id": id_a, "country": "FRA", "revision": 0}],
            reason="阶段4验收：基线确认法国（合成）",
        )
        _assert_redirect_success(response, batch_one)
    revision = _destination_revision(sandbox, id_a)
    assert revision > 0
    mid = _db_state(sandbox)

    # (e) 过期修订：沿用旧表单（revision=0）改发德国 → 拒绝，A 保持法国。
    with _web_client(sandbox) as client:
        client.get(f"/batches/{batch_one}/destinations")
        stale = _web_confirm_destinations(
            client, batch_one,
            plan_id=str(view["plan_id"]),
            reference_version=int(view["reference_version"]),
            updates=[{"task_id": id_a, "country": "DEU", "revision": 0}],
            reason="阶段4验收：过期修订重放（合成）",
        )
        _assert_redirect_rejected(stale)
    assert _db_state(sandbox) == mid

    # (f) 伪造未来修订 999999 → 拒绝且零写入。
    with _web_client(sandbox) as client:
        client.get(f"/batches/{batch_one}/destinations")
        forged = _web_confirm_destinations(
            client, batch_one,
            plan_id=str(view["plan_id"]),
            reference_version=int(view["reference_version"]),
            updates=[{"task_id": id_a, "country": "DEU", "revision": 999999}],
            reason="阶段4验收：伪造修订（合成）",
        )
        _assert_redirect_rejected(forged)
    assert _db_state(sandbox) == mid

    # (g) 同一票同时提交逐票国家与统一国家 → 明确拒绝，不悄悄二选一。
    with _web_client(sandbox) as client:
        client.get(f"/batches/{batch_one}/destinations")
        ambiguous = _web_confirm_destinations(
            client, batch_one,
            plan_id=str(view["plan_id"]),
            reference_version=int(view["reference_version"]),
            updates=[{
                "task_id": id_a, "country": "DEU", "revision": revision,
            }],
            bulk_country="FRA",
            reason="阶段4验收：歧义提交（合成）",
        )
        _assert_redirect_rejected(ambiguous)
    assert _db_state(sandbox) == mid

    # 授权变化只有 (d) 一笔：A→FRA；跨批次票全程未被触碰。
    _assert_only_destination_writes(
        before, _db_state(sandbox), changed_task_ids={id_a})
    assert _destination_events(sandbox, id_cross) == []
    task_cross = sandbox.service.tasks.get(id_cross)
    assert task_cross["destination_alpha3"] is None
    assert _tree_state(sandbox.runtime / "batches") == batches_before


# ---------------------------------------------------------------------------
# 6. Web 安全：CSRF / Origin 防护与入口
# ---------------------------------------------------------------------------


def test_web_destinations_csrf_origin_and_entry(sandbox: Sandbox):
    _store_machine_reference(sandbox)
    eml = _write_eml(sandbox, "machines.eml", _machine_eml())
    _payload, detail = _import_eml(sandbox, eml)
    batch_id = detail["batch"]["id"]
    with _web_client(sandbox) as client:
        tasks = _web_confirm_batch(sandbox, client, detail)
    id_a = str(_map_by_invoice(tasks)[MACHINE_TICKETS[0]["invoice_no"]]["task_id"])
    view = _cli_destinations(sandbox, batch_id)
    before = _db_state(sandbox)

    with _web_client(sandbox) as client:
        detail_page = client.get(f"/batches/{batch_id}")
        assert detail_page.status_code == 200
        # 已物化批次详情页提供逐票目的国确认入口。
        assert f"/batches/{batch_id}/destinations" in detail_page.text

        page = client.get(f"/batches/{batch_id}/destinations")
        assert page.status_code == 200
        missing = client.get("/batches/batch-nope/destinations")
        assert missing.status_code == 404

        base = dict(
            plan_id=str(view["plan_id"]),
            reference_version=int(view["reference_version"]),
            updates=[{"task_id": id_a, "country": "FRA", "revision": 0}],
            reason="阶段4验收：安全防护（合成）",
        )
        # 无 CSRF / 错 CSRF / 跨站 Origin / null Origin：一律 403 且零写入。
        no_csrf = _web_confirm_destinations(
            client, batch_id, csrf="", **base)
        assert no_csrf.status_code == 403
        wrong_csrf = _web_confirm_destinations(
            client, batch_id, csrf="forged-token", **base)
        assert wrong_csrf.status_code == 403
        evil_origin = _web_confirm_destinations(
            client, batch_id, origin="http://evil.example.com", **base)
        assert evil_origin.status_code == 403
        null_origin = _web_confirm_destinations(
            client, batch_id, origin="null", **base)
        assert null_origin.status_code == 403

    assert _db_state(sandbox) == before, "安全拦截不应产生任何写入"
    assert _destination_events(sandbox, id_a) == []


# ---------------------------------------------------------------------------
# 7. 未确认目的国：离线执行如实 waiting_input，绝不自动确认/猜国
# ---------------------------------------------------------------------------


def test_missing_destination_blocks_offline_without_guessing(sandbox: Sandbox):
    _store_machine_reference(sandbox)
    eml = _write_eml(sandbox, "machines.eml", _machine_eml())
    _payload, detail = _import_eml(sandbox, eml)
    with _web_client(sandbox) as client:
        tasks = _web_confirm_batch(sandbox, client, detail)
    task = tasks[0]
    task_id = str(task["task_id"])

    # 邮件正文/候选材料不含受控目的国输入；系统不得自动确认。
    assert _destination_events(sandbox, task_id) == []
    current = sandbox.service.tasks.get(task_id)
    assert current["destination_alpha3"] is None

    out_dir = sandbox.root / "offline" / "no-destination"
    report = _offline_run_isolated(sandbox, task_id, out_dir)
    assert report["status"] == "waiting_input"
    codes = {str(issue["code"]) for issue in report["issues"]}
    assert "MISSING_DESTINATION" in codes
    assert report["artifacts"] == []
    assert sorted(path.name for path in out_dir.iterdir()) == [
        "offline_report.json"]
    assert report["state_unchanged"] is True
    assert report["task_state_after"]["execution_hold"] is True
    assert report["task_state_after"]["status"] == "RECEIVED"
    # 报告如实记录“未确认”：destination 追溯字段为空值而不是猜测。
    destination = report.get("destination") or {}
    assert destination.get("alpha3") in (None, "")
    assert destination.get("revision") == 0
    assert destination.get("confirmed_by") is None
    # 离线执行后仍未产生任何确认（不自动写目的国/审计）。
    assert _destination_events(sandbox, task_id) == []
    assert sandbox.service.tasks.get(task_id)["destination_alpha3"] is None
