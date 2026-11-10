"""已确认整机单票快照解析（machine_confirmed）合成验收。

全部数据为合成/去标识化值；不读取真实邮件、不生成任务、不联网。
覆盖：新式 KG 表头、旧 BIFF CI/PL（无 MSO、无正文重量）、单机/多机、
同票分文件、缺 MSO 唯一序列补认、同 MSO/序列号交叉冲突、重复机器、
多重量区/TOTAL 冲突、数量与重量不匹配、PIN/engine/serial 核对、
价格只取正文行、目的国不推断、不泄漏其他票、来源/哈希/区域契约拒绝。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest

from cater.core.models import Source
from cater.core.parsing.machine_confirmed import parse_confirmed_machine_sources
from cater.core.reconcile import reconcile_machines

try:  # 仅用于生成临时旧版 xls 夹具（运行期解析依赖 xlrd）
    import xlwt
except ImportError:  # pragma: no cover - 环境缺生成器时跳过旧 xls 用例
    xlwt = None

MACHINE_HEADER = ["ITEM#", "MODEL", "LOADER #", "PIN#", "ENGINE #", "MSO #"]
MACHINE_COLUMNS = {"model": "B", "serial": "C", "pin": "D",
                   "engine": "E", "mso": "F"}
WEIGHT_HEADER = ["MARKS", "PART NAME", "QTY.", "NET WEIGHT (KG)",
                 "GROSS WEIGHT (KG)"]
WEIGHT_COLUMNS = {"description": "B", "qty": "C", "net": "D", "gross": "E"}

MAIL_DEFAULTS = {
    "dealer_code": "SYN01",
    "dealer_order": "SYNORD-1",
    "order": "0620000001",
    "material": "SEM655D",
    "total_sales_price": "50,000.00",
    "region": "CA",
    "ship_to_party": "SYN01",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sources(*paths: Path) -> list:
    return [{"path": str(path), "filename": Path(path).name,
             "sha256": _sha(path)} for path in paths]


def _ref(path: Path, regions: list) -> dict:
    return {"sha256": _sha(path), "filenames": [Path(path).name],
            "regions": regions}


def _candidate(refs: list, identity: dict, mail_row_refs=None,
               kind: str = "machine") -> dict:
    return {
        "candidate_id": "cand-synthetic",
        "kind": kind,
        "identity": identity,
        "source_refs": refs,
        "mail_row_refs": list(mail_row_refs or []),
        "issues": [],
        "needs_confirmation": False,
    }


def _mail_ref(mso: str, serial: str, *, table_index: int = 0, row: int = 2,
              **extra) -> dict:
    fields = dict(MAIL_DEFAULTS)
    fields.update({"mso": mso, "serial_number": serial})
    fields.update({key: str(value) for key, value in extra.items()})
    return {"table_index": table_index, "row": row, "mso": mso,
            "serial": serial, "fields": fields}


def _mail_objects(candidate: dict) -> list:
    rows = []
    for ref in candidate["mail_row_refs"]:
        rows.append(SimpleNamespace(
            fields=dict(ref.get("fields") or {}),
            source=Source(type="mail_html",
                          table_index=ref.get("table_index"),
                          row=ref.get("row"))))
    return rows


def _reconcile(result, candidate: dict):
    return reconcile_machines(
        _mail_objects(candidate), result.invoice_rows,
        result.packing_rows, result.weight_groups)


def _codes(result) -> list:
    return [issue.code for issue in result.issues]


def _blocking(result) -> list:
    return [issue for issue in result.issues if issue.blocking]


def _checks(result, rule: str) -> list:
    return [check for check in result.layout["checks"]
            if check["rule"] == rule]


# ---------------------------------------------------------------------------
# 合成工作簿构建
# ---------------------------------------------------------------------------


def _write_machine_sheet(ws, invoice_no: str, machines: list,
                         header_row: int = 18) -> None:
    ws["A5"] = "COMMERCIAL INVOICE"
    ws["A6"] = "TO: SYNTHETIC CONSIGNEE, MALTA"
    ws["A7"] = ("SHIPPED FROM SYNTHETIC PORT, CHINA TO SYNTHETIC "
                "DESTINATION BY SEA")
    ws["E6"] = f"INVOICE NO.: {invoice_no}"
    for index, value in enumerate(MACHINE_HEADER, 1):
        ws.cell(header_row, index, value)
    for offset, machine in enumerate(machines):
        row = header_row + 1 + offset
        ws.cell(row, 1, offset + 1)
        ws.cell(row, 2, machine["model"])
        ws.cell(row, 3, machine["serial"])
        ws.cell(row, 4, machine["pin"])
        ws.cell(row, 5, machine["engine"])
        ws.cell(row, 6, machine.get("mso"))


def _write_weight_sheet(ws, invoice_no: str, weight_rows: list,
                        total: dict, header_row: int = 8,
                        total_row: int | None = None) -> int:
    ws["A5"] = "PACKING LIST"
    ws["C6"] = f"INVOICE NO.: {invoice_no}"
    for index, value in enumerate(WEIGHT_HEADER, 1):
        ws.cell(header_row, index, value)
    for offset, row in enumerate(weight_rows):
        out_row = header_row + 1 + offset
        ws.cell(out_row, 1, "N/M")
        ws.cell(out_row, 2, row["label"])
        ws.cell(out_row, 3, row["qty"])
        ws.cell(out_row, 4, row["net"])
        ws.cell(out_row, 5, row["gross"])
    out_total = total_row or header_row + 1 + len(weight_rows)
    ws.cell(out_total, 1, "TOTAL")
    ws.cell(out_total, 3, total["qty"])
    ws.cell(out_total, 4, total["net"])
    ws.cell(out_total, 5, total["gross"])
    return out_total


def _new_xlsx(path: Path, machines: list, weight_rows: list, total: dict,
              invoice_no: str = "SYN-INV-1", *,
              amount: float | None = None,
              include_machine: bool = True,
              include_weight: bool = True,
              machine_sheet: str = "Invoice",
              weight_sheet: str = "Packing list") -> dict:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    if include_machine:
        _write_machine_sheet(wb.create_sheet(machine_sheet), invoice_no,
                             machines)
        if amount is not None:
            wb[machine_sheet]["G11"] = amount
    total_row = None
    if include_weight:
        ws = wb.create_sheet(weight_sheet)
        total_row = _write_weight_sheet(ws, invoice_no, weight_rows, total)
    wb.save(path)
    regions = []
    if include_machine:
        regions.append({
            "sheet": machine_sheet, "role": "machine_table",
            "header_row": 18, "columns": dict(MACHINE_COLUMNS),
            "data_first_row": 19,
            "data_last_row": 18 + len(machines),
        })
    if include_weight:
        regions.append({
            "sheet": weight_sheet, "role": "weight_table",
            "header_row": 8, "subheader_row": None,
            "columns": dict(WEIGHT_COLUMNS), "total_row": total_row,
        })
    return {"path": path, "regions": regions}


def _identity(machines: list, invoice_no: str) -> dict:
    return {
        "invoice_no": [invoice_no],
        "mso": [machine["mso"] for machine in machines],
        "serial": [machine["serial"] for machine in machines],
    }


def _machine(mso: str, serial: str, *, model: str = "SEM655D",
             pin: str | None = None, engine: str | None = None) -> dict:
    return {"model": model, "serial": serial,
            "pin": pin if pin is not None else f"SEM00655K{serial}",
            "engine": engine if engine is not None else f"1226S{serial[-4:]}",
            "mso": mso}


def _build_old_xls(path: Path, *, invoice_no: str = "SYN-OLD-1",
                   model: str = "SEM655D", serial: str = "S5515230",
                   pin: str | None = None,
                   engine: str = "1226J019443",
                   net: float = 16900, qty: int = 1,
                   serial_cell: bool = True) -> dict:
    if xlwt is None:  # pragma: no cover
        pytest.skip("xlwt 未安装，无法生成合成 .xls 夹具")
    pin = pin if pin is not None else f"SEM00655H{serial}"
    book = xlwt.Workbook()
    ci = book.add_sheet("CI")
    ci.write(9, 5, f"INVOICE NO.: {invoice_no}")       # F10
    for col, title in ((0, "ITEM#"), (1, "MODEL"), (2, "YEAR MODEL"),
                       (3, "ENGINE NO"), (4, "PIN NUMBER"),
                       (5, "DEALER ORDER"), (6, "CONTAINER NO.")):
        ci.write(29, col, title)                       # 第 30 行表头
    ci.write(30, 0, 1.0)
    ci.write(30, 1, model)
    ci.write(30, 3, engine)
    ci.write(30, 4, pin)
    if serial_cell:
        ci.write(30, 5, serial)                        # DEALER ORDER 实为序列号
    ci.write(30, 9, net)                               # J31 旁侧重量，不读
    pl = book.add_sheet("PL")
    pl.write(8, 5, f"INVOICE NO.: {invoice_no}")       # F9
    pl.write(16, 0, "MARK")
    pl.write(16, 2, "DESCRIPTION OF GOODS")
    pl.write(16, 3, "QTY")
    pl.write(16, 5, "WEIGHT/KGS")
    pl.write(17, 5, "GROSS")                           # F18
    pl.write(17, 6, "NET")                             # G18
    pl.write(18, 2, f"{model} WHEEL LOADER, 3 CUM GP PS BUCKET")
    pl.write(18, 3, float(qty))
    pl.write(18, 5, float(net))
    pl.write(18, 6, float(net))
    pl.write(19, 0, "TOTAL")
    pl.write(19, 3, float(qty))
    pl.write(19, 5, float(net))
    pl.write(19, 6, float(net))
    book.save(str(path))
    return {
        "path": path,
        "regions": [
            {"sheet": "CI", "role": "machine_info_partial",
             "header_row": 30,
             "columns": {"model": "B", "engine": "D", "pin": "E"},
             "rows": 1},
            {"sheet": "PL", "role": "weight_table",
             "header_row": 17, "subheader_row": 18,
             "columns": {"description": "C", "qty": "D", "gross": "F",
                         "net": "G"},
             "total_row": 20},
        ],
    }


# ---------------------------------------------------------------------------
# 新式 KG 表头 / 单机 / 价格与目的国边界
# ---------------------------------------------------------------------------


def test_new_style_kg_single_machine_ready(tmp_path):
    machines = [_machine("MHZ01", "S5515001")]
    path = tmp_path / "CI&PL--SYN--M1.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17200, "gross": 17200}],
                      {"qty": 1, "net": 17200, "gross": 17200},
                      invoice_no="SYN-INV-1", amount=999999)
    candidate = _candidate([_ref(path, built["regions"])],
                           _identity(machines, "SYN-INV-1"),
                           [_mail_ref("MHZ01", "S5515001")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])

    assert not _blocking(result), [i.to_dict() for i in _blocking(result)]
    assert result.invoice_no == "SYN-INV-1"
    assert len(result.invoice_rows) == 1
    row = result.invoice_rows[0]
    assert (row.mso, row.model, row.loader) == (
        "MHZ01", "SEM655D", "S5515001")
    assert row.pin == "SEM00655KS5515001"
    assert row.engine == "1226S5001"
    assert row.source.type == "workbook"
    assert (row.source.sheet, row.source.cell) == ("Invoice", "C19")
    assert len(result.weight_groups) == 1
    group = result.weight_groups[0]
    assert group.msos == ["MHZ01"] and group.qty == 1
    assert group.net_weight == Decimal("17200")
    assert group.gross_weight == Decimal("17200")
    assert (group.source.sheet, group.source.cell) == ("Packing list", "B9")
    assert _checks(result, "MACHINE_CONFIRMED_PIN_SERIAL")[0]["result"] == "pass"
    assert _checks(result, "MACHINE_CONFIRMED_DESTINATION_NOT_INFERRED")[0][
        "result"] == "pass"
    destination_detail = _checks(
        result, "MACHINE_CONFIRMED_DESTINATION_NOT_INFERRED")[0]["detail"]
    assert "推断" in destination_detail
    assert "目的国由调用方" in destination_detail
    assert "未读取" in _checks(
        result, "MACHINE_CONFIRMED_PRICE_SOURCE")[0]["detail"]

    # 价格只来自正文行（50,000.00），发票合计 999999 不得混入
    reconciled = _reconcile(result, candidate)
    assert not [issue for issue in reconciled.issues if issue.blocking]
    assert len(reconciled.records) == 1
    fields = reconciled.records[0].fields
    assert fields["total_sales_price"].value == Decimal("50000.00")
    assert fields["net_weight"].value == Decimal("17200")
    assert fields["pin"].value == "SEM00655KS5515001"
    assert fields["engine"].value == "1226S5001"


def test_reconcile_price_cannot_come_from_invoice_total(tmp_path):
    """即使发票 G11 合计极大，reconcile 的机器价格仍是正文 Total Sales Price。"""
    machines = [_machine("PRICE01", "S5515100")]
    path = tmp_path / "price.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17000, "gross": 17000}],
                      {"qty": 1, "net": 17000, "gross": 17000},
                      invoice_no="SYN-PRICE", amount=1234567)
    candidate = _candidate([_ref(path, built["regions"])],
                           _identity(machines, "SYN-PRICE"),
                           [_mail_ref("PRICE01", "S5515100",
                                      total_sales_price="12,345.67")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    reconciled = _reconcile(result, candidate)
    price = reconciled.records[0].fields["total_sales_price"]
    assert price.value == Decimal("12345.67")
    assert price.source.type == "mail_html"


# ---------------------------------------------------------------------------
# 旧 BIFF CI/PL：无 MSO、无正文重量
# ---------------------------------------------------------------------------


@pytest.mark.skipif(xlwt is None, reason="xlwt 未安装，跳过旧 xls 夹具")
def test_old_biff_ci_pl_mso_from_mail_and_weight_from_pl(tmp_path):
    path = tmp_path / "CI+PL---1X655D.xls"
    built = _build_old_xls(path)
    candidate = _candidate(
        [_ref(path, built["regions"])],
        {"invoice_no": ["SYN-OLD-1"], "mso": ["BITOE"],
         "serial": ["S5515230"]},
        [_mail_ref("BITOE", "S5515230")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])

    assert not _blocking(result), [i.to_dict() for i in _blocking(result)]
    assert result.invoice_no == "SYN-OLD-1"
    assert len(result.invoice_rows) == 1
    row = result.invoice_rows[0]
    assert row.mso == "BITOE"                    # mail_rows 唯一序列补认
    assert row.loader == "S5515230"
    assert row.pin == "SEM00655HS5515230"
    assert row.engine == "1226J019443"
    assert row.source.type == "workbook"         # 来源保持工作簿单元格
    assert (row.source.sheet, row.source.cell) == ("CI", "F31")
    mso_checks = _checks(result, "MACHINE_CONFIRMED_MSO_FROM_MAIL")
    assert mso_checks and mso_checks[0]["result"] == "pass"
    assert "F31" in mso_checks[0]["detail"]
    assert len(result.weight_groups) == 1
    group = result.weight_groups[0]
    assert group.msos == ["BITOE"]
    assert group.net_weight == Decimal("16900")
    assert group.gross_weight == Decimal("16900")
    assert (group.source.sheet, group.source.cell) == ("PL", "C19")

    reconciled = _reconcile(result, candidate)
    assert not [issue for issue in reconciled.issues if issue.blocking]
    fields = reconciled.records[0].fields
    assert fields["net_weight"].value == Decimal("16900")
    assert fields["total_sales_price"].value == Decimal("50000.00")


@pytest.mark.skipif(xlwt is None, reason="xlwt 未安装，跳过旧 xls 夹具")
def test_old_biff_accepts_mail_row_objects(tmp_path):
    path = tmp_path / "old-objects.xls"
    built = _build_old_xls(path, serial="S5515231", invoice_no="SYN-OLD-2")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        {"invoice_no": ["SYN-OLD-2"], "mso": ["BITOE"],
         "serial": ["S5515231"]},
        [_mail_ref("BITOE", "S5515231")])
    mail_objects = _mail_objects(candidate)
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, mail_objects)
    assert not _blocking(result)
    assert result.invoice_rows[0].mso == "BITOE"


@pytest.mark.skipif(xlwt is None, reason="xlwt 未安装，跳过旧 xls 夹具")
def test_old_biff_conflicting_mail_mso_rejected(tmp_path):
    path = tmp_path / "old-conflict.xls"
    built = _build_old_xls(path)
    candidate = _candidate(
        [_ref(path, built["regions"])],
        {"invoice_no": ["SYN-OLD-1"], "mso": ["BITOE"],
         "serial": ["S5515230"]},
        [_mail_ref("BITOE", "S5515230"),
         _mail_ref("OTHER", "S5515230")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert any(issue.code == "MSO_CONFLICT" for issue in result.issues)
    assert _blocking(result)


# ---------------------------------------------------------------------------
# 多机 / 同票分文件
# ---------------------------------------------------------------------------


def test_multi_machine_group_by_mso(tmp_path):
    machines = [_machine("MM01", "S5516001", engine="ENG-01"),
                _machine("MM02", "S5516002", engine="ENG-02")]
    path = tmp_path / "multi.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D--MM01/MM02", "qty": 2,
          "net": 20000, "gross": 20000}],
        {"qty": 2, "net": 20000, "gross": 20000},
        invoice_no="SYN-MULTI")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-MULTI"),
        [_mail_ref("MM01", "S5516001"),
         _mail_ref("MM02", "S5516002", row=3)])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert not _blocking(result)
    assert [row.mso for row in result.invoice_rows] == ["MM01", "MM02"]
    assert len(result.weight_groups) == 1
    group = result.weight_groups[0]
    assert group.msos == ["MM01", "MM02"] and group.qty == 2

    reconciled = _reconcile(result, candidate)
    assert not [issue for issue in reconciled.issues if issue.blocking]
    assert {record.mso for record in reconciled.records} == {"MM01", "MM02"}
    for record in reconciled.records:
        assert record.fields["net_weight"].value == Decimal("10000")


def test_same_ticket_split_ci_and_pl_files(tmp_path):
    machines = [_machine("SPLIT01", "S5516100")]
    ci_path = tmp_path / "split-CI.xlsx"
    pl_path = tmp_path / "split-PL.xlsx"
    ci = _new_xlsx(ci_path, machines, [], {}, invoice_no="SYN-SPLIT",
                   include_weight=False)
    pl = _new_xlsx(pl_path, [], [{"label": "SEM655D WHEEL LOADER",
                                  "qty": 1, "net": 18000, "gross": 18000}],
                   {"qty": 1, "net": 18000, "gross": 18000},
                   invoice_no="SYN-SPLIT",
                   include_machine=False, machine_sheet="Unused",
                   weight_sheet="Packing list")
    candidate = _candidate(
        [_ref(ci_path, ci["regions"]), _ref(pl_path, pl["regions"])],
        _identity(machines, "SYN-SPLIT"),
        [_mail_ref("SPLIT01", "S5516100")])
    result = parse_confirmed_machine_sources(
        _sources(ci_path, pl_path), candidate, candidate["mail_row_refs"])
    assert not _blocking(result)
    assert result.invoice_no == "SYN-SPLIT"
    assert len(result.invoice_rows) == 1
    assert result.invoice_rows[0].mso == "SPLIT01"
    assert len(result.weight_groups) == 1
    assert result.weight_groups[0].msos == ["SPLIT01"]
    assert result.weight_groups[0].net_weight == Decimal("18000")
    reconciled = _reconcile(result, candidate)
    assert not [issue for issue in reconciled.issues if issue.blocking]


# ---------------------------------------------------------------------------
# 缺 MSO 的唯一序列补认 / 冲突拒绝
# ---------------------------------------------------------------------------


def test_missing_mso_filled_by_unique_serial(tmp_path):
    machines = [_machine("", "S5516200", pin="SEM00655KS5516200")]
    path = tmp_path / "unique-serial.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 16500, "gross": 16500}],
                      {"qty": 1, "net": 16500, "gross": 16500},
                      invoice_no="SYN-UNIQUE")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        {"invoice_no": ["SYN-UNIQUE"], "mso": ["UNIQ01"],
         "serial": ["S5516200"]},
        [_mail_ref("UNIQ01", "S5516200")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert not _blocking(result)
    assert result.invoice_rows[0].mso == "UNIQ01"
    check = _checks(result, "MACHINE_CONFIRMED_MSO_FROM_MAIL")
    assert check and check[0]["result"] == "pass"
    # 机器行来源仍是工作簿，而不是被改写成邮件来源
    assert result.invoice_rows[0].source.type == "workbook"
    assert result.weight_groups[0].msos == ["UNIQ01"]


def test_missing_mso_conflicting_mail_rows_rejected(tmp_path):
    machines = [_machine("", "S5516300")]
    path = tmp_path / "unique-conflict.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 16000, "gross": 16000}],
                      {"qty": 1, "net": 16000, "gross": 16000},
                      invoice_no="SYN-UQ-CONFLICT")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        {"invoice_no": ["SYN-UQ-CONFLICT"], "mso": ["UQ01"],
         "serial": ["S5516300"]},
        [_mail_ref("UQ01", "S5516300"),
         _mail_ref("UQ02", "S5516300", row=3)])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "MSO_CONFLICT" in _codes(result)
    assert _blocking(result)
    assert result.invoice_rows == []


def test_missing_mso_without_mail_uses_confirmed_identity(tmp_path):
    machines = [_machine("", "S5516350")]
    path = tmp_path / "identity-fallback.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 16000, "gross": 16000}],
                      {"qty": 1, "net": 16000, "gross": 16000},
                      invoice_no="SYN-ID-FALLBACK")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        {"invoice_no": ["SYN-ID-FALLBACK"], "mso": ["ID01"],
         "serial": ["S5516350"]}, [])
    result = parse_confirmed_machine_sources(_sources(path), candidate, [])
    assert not _blocking(result), [i.to_dict() for i in _blocking(result)]
    assert result.invoice_rows[0].mso == "ID01"


def test_mail_mso_serial_cross_conflict_rejected(tmp_path):
    machines = [_machine("X01", "S5516401", engine="ENG-X1"),
                _machine("X02", "S5516402", engine="ENG-X2")]
    path = tmp_path / "cross.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D--X01/X02", "qty": 2,
          "net": 20000, "gross": 20000}],
        {"qty": 2, "net": 20000, "gross": 20000},
        invoice_no="SYN-CROSS")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-CROSS"),
        [_mail_ref("X01", "S5516402"),
         _mail_ref("X02", "S5516401", row=3)])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "MSO_SERIAL_CROSS" in _codes(result)
    assert _blocking(result)
    # 不得静默把正文行的交叉身份回写到机器行
    assert {(row.mso, row.loader) for row in result.invoice_rows} == {
        ("X01", "S5516401"), ("X02", "S5516402")}


# ---------------------------------------------------------------------------
# 重复机器 / PIN / engine 核对
# ---------------------------------------------------------------------------


def test_duplicate_serial_and_conflicting_machine_rows(tmp_path):
    machines = [
        {"model": "SEM655D", "serial": "S5516500",
         "pin": "SEM00655KS5516500", "engine": "ENG-DUP1", "mso": "DUP01"},
        {"model": "SEM655D", "serial": "S5516500",
         "pin": "SEM00655KS5516500", "engine": "ENG-DUP2", "mso": "DUP02"},
    ]
    path = tmp_path / "duplicate-serial.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17000, "gross": 17000}],
                      {"qty": 1, "net": 17000, "gross": 17000},
                      invoice_no="SYN-DUP")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        {"invoice_no": ["SYN-DUP"], "mso": ["DUP01", "DUP02"],
         "serial": ["S5516500"]},
        [_mail_ref("DUP01", "S5516500")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "DUPLICATE_SERIAL" in _codes(result)
    assert "MACHINE_ROW_CONFLICT" in _codes(result)
    assert _blocking(result)


def test_pin_and_engine_cross_checks(tmp_path):
    machines = [
        {"model": "SEM655D", "serial": "S5516601",
         "pin": "SEM00655KS5516602",          # PIN 内嵌的是另一台序列号
         "engine": "ENG-COMMON", "mso": "PE01"},
        {"model": "SEM655D", "serial": "S5516602",
         "pin": "SEM00655KS5516602",
         "engine": "ENG-COMMON", "mso": "PE02"},
    ]
    path = tmp_path / "pin-engine.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D--PE01/PE02", "qty": 2,
          "net": 20000, "gross": 20000}],
        {"qty": 2, "net": 20000, "gross": 20000},
        invoice_no="SYN-PIN")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-PIN"),
        [_mail_ref("PE01", "S5516601"),
         _mail_ref("PE02", "S5516602", row=3)])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "SERIAL_CONFLICT" in _codes(result)
    assert "ENGINE_DUPLICATE" in _codes(result)
    assert _blocking(result)
    pin_checks = _checks(result, "MACHINE_CONFIRMED_PIN_SERIAL")
    assert {check["result"] for check in pin_checks} == {"fail", "pass"}


# ---------------------------------------------------------------------------
# 重量区 / TOTAL / 数量核对
# ---------------------------------------------------------------------------


def test_multiple_weight_regions_same_machine_conflict(tmp_path):
    machines = [_machine("W01", "S5516701")]
    ci_path = tmp_path / "weight-ci.xlsx"
    pl1_path = tmp_path / "weight-pl1.xlsx"
    pl2_path = tmp_path / "weight-pl2.xlsx"
    ci = _new_xlsx(ci_path, machines, [], {}, invoice_no="SYN-WEIGHT",
                   include_weight=False)
    pl1 = _new_xlsx(pl1_path, [], [{"label": "SEM655D WHEEL LOADER",
                                    "qty": 1, "net": 17000, "gross": 17000}],
                    {"qty": 1, "net": 17000, "gross": 17000},
                    invoice_no="SYN-WEIGHT", include_machine=False,
                    weight_sheet="Packing list A")
    pl2 = _new_xlsx(pl2_path, [], [{"label": "SEM655D WHEEL LOADER",
                                    "qty": 1, "net": 17200, "gross": 17200}],
                    {"qty": 1, "net": 17200, "gross": 17200},
                    invoice_no="SYN-WEIGHT", include_machine=False,
                    weight_sheet="Packing list B")
    candidate = _candidate(
        [_ref(ci_path, ci["regions"]), _ref(pl1_path, pl1["regions"]),
         _ref(pl2_path, pl2["regions"])],
        _identity(machines, "SYN-WEIGHT"),
        [_mail_ref("W01", "S5516701")])
    result = parse_confirmed_machine_sources(
        _sources(ci_path, pl1_path, pl2_path), candidate,
        candidate["mail_row_refs"])
    assert "WEIGHT_GROUP_CONFLICT" in _codes(result)
    assert _blocking(result)
    assert len(result.weight_groups) == 1
    # 不按文件顺序改写：保留的是确定性的第一份来源，但冲突已阻断
    assert result.weight_groups[0].net_weight in (
        Decimal("17000"), Decimal("17200"))


def test_multiple_weight_regions_identical_is_visible_non_blocking(tmp_path):
    machines = [_machine("W02", "S5516702")]
    ci_path = tmp_path / "dup-ci.xlsx"
    pl1_path = tmp_path / "dup-pl1.xlsx"
    pl2_path = tmp_path / "dup-pl2.xlsx"
    ci = _new_xlsx(ci_path, machines, [], {}, invoice_no="SYN-DUP-W",
                   include_weight=False)
    rows = [{"label": "SEM655D WHEEL LOADER", "qty": 1,
             "net": 17000, "gross": 17000}]
    total = {"qty": 1, "net": 17000, "gross": 17000}
    pl1 = _new_xlsx(pl1_path, [], rows, total, invoice_no="SYN-DUP-W",
                    include_machine=False, weight_sheet="Packing list A")
    pl2 = _new_xlsx(pl2_path, [], rows, total, invoice_no="SYN-DUP-W",
                    include_machine=False, weight_sheet="Packing list B")
    candidate = _candidate(
        [_ref(ci_path, ci["regions"]), _ref(pl1_path, pl1["regions"]),
         _ref(pl2_path, pl2["regions"])],
        _identity(machines, "SYN-DUP-W"),
        [_mail_ref("W02", "S5516702")])
    result = parse_confirmed_machine_sources(
        _sources(ci_path, pl1_path, pl2_path), candidate,
        candidate["mail_row_refs"])
    duplicates = [issue for issue in result.issues
                  if issue.code == "WEIGHT_GROUP_DUPLICATE"]
    assert duplicates and not duplicates[0].blocking
    assert not _blocking(result)
    assert len(result.weight_groups) == 1


def test_weight_total_qty_and_net_mismatch(tmp_path):
    machines = [_machine("WT01", "S5516801")]
    path = tmp_path / "total-mismatch.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17200, "gross": 17200}],
                      {"qty": 2, "net": 18000, "gross": 18000},
                      invoice_no="SYN-TOTAL")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-TOTAL"),
        [_mail_ref("WT01", "S5516801")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "WEIGHT_TOTAL_MISMATCH" in _codes(result)
    assert _blocking(result)
    total_checks = _checks(result, "MACHINE_PACKING_TOTAL_ROW")
    assert total_checks and total_checks[0]["result"] == "fail"


def test_multiple_total_rows_rejected(tmp_path):
    machines = [_machine("MT01", "S5516901")]
    path = tmp_path / "multi-total.xlsx"
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _write_machine_sheet(wb.create_sheet("Invoice"), "SYN-MT", machines)
    ws = wb.create_sheet("Packing list")
    ws["C6"] = "INVOICE NO.: SYN-MT"
    for index, value in enumerate(WEIGHT_HEADER, 1):
        ws.cell(8, index, value)
    ws.cell(9, 1, "N/M")
    ws.cell(9, 2, "SEM655D WHEEL LOADER")
    ws.cell(9, 3, 1)
    ws.cell(9, 4, 17200)
    ws.cell(9, 5, 17200)
    ws.cell(10, 1, "TOTAL")
    ws.cell(10, 3, 1)
    ws.cell(10, 4, 17200)
    ws.cell(10, 5, 17200)
    ws.cell(11, 1, "N/M")
    ws.cell(11, 2, "SEM655D WHEEL LOADER")
    ws.cell(11, 3, 1)
    ws.cell(11, 4, 17300)
    ws.cell(11, 5, 17300)
    ws.cell(12, 1, "TOTAL")
    ws.cell(12, 3, 1)
    ws.cell(12, 4, 17300)
    ws.cell(12, 5, 17300)
    wb.save(path)
    regions = [
        {"sheet": "Invoice", "role": "machine_table", "header_row": 18,
         "columns": dict(MACHINE_COLUMNS), "data_first_row": 19,
         "data_last_row": 19},
        {"sheet": "Packing list", "role": "weight_table", "header_row": 8,
         "columns": dict(WEIGHT_COLUMNS), "total_row": 10},
    ]
    candidate = _candidate([_ref(path, regions)],
                           _identity(machines, "SYN-MT"),
                           [_mail_ref("MT01", "S5516901")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "MULTIPLE_TOTAL_ROWS" in _codes(result)
    assert _blocking(result)
    assert result.weight_groups == []


def test_weight_group_qty_mismatch_blocks_group(tmp_path):
    machines = [_machine("QG01", "S5517001", engine="ENG-Q1"),
                _machine("QG02", "S5517002", engine="ENG-Q2")]
    path = tmp_path / "qty-mismatch.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D--QG01/QG02", "qty": 1,
          "net": 20000, "gross": 20000}],
        {"qty": 1, "net": 20000, "gross": 20000},
        invoice_no="SYN-QTY")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-QTY"),
        [_mail_ref("QG01", "S5517001"),
         _mail_ref("QG02", "S5517002", row=3)])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "WEIGHT_GROUP_QTY_MISMATCH" in _codes(result)
    assert _blocking(result)
    assert result.weight_groups == []


def test_weight_without_mso_multiple_machines_ambiguous(tmp_path):
    machines = [_machine("AM01", "S5517101", engine="ENG-A1"),
                _machine("AM02", "S5517102", engine="ENG-A2")]
    path = tmp_path / "ambiguous-weight.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 20000, "gross": 20000}],
                      {"qty": 1, "net": 20000, "gross": 20000},
                      invoice_no="SYN-AMB")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-AMB"),
        [_mail_ref("AM01", "S5517101"),
         _mail_ref("AM02", "S5517102", row=3)])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    ambiguous = [issue for issue in result.issues
                 if issue.code == "WEIGHT_GROUP_AMBIGUOUS"]
    assert ambiguous and ambiguous[0].blocking
    assert "不按行号或平均分配猜测" in ambiguous[0].message
    assert result.weight_groups == []


def test_weight_without_mso_unique_model_mapping(tmp_path):
    machines = [_machine("UM01", "S5517201", model="SEM655D",
                         engine="ENG-U1"),
                _machine("UM02", "S5517202", model="SEM512",
                         engine="ENG-U2")]
    path = tmp_path / "unique-model-weight.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
          "net": 17200, "gross": 17200},
         {"label": "SEM512 SOIL COMPACTOR", "qty": 1,
          "net": 12000, "gross": 12000}],
        {"qty": 2, "net": 29200, "gross": 29200},
        invoice_no="SYN-UM")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-UM"),
        [_mail_ref("UM01", "S5517201"),
         _mail_ref("UM02", "S5517202", row=3)])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert not _blocking(result), [i.to_dict() for i in _blocking(result)]
    groups = {tuple(group.msos): group for group in result.weight_groups}
    assert set(groups) == {("UM01",), ("UM02",)}
    assert groups[("UM01",)].net_weight == Decimal("17200")
    assert groups[("UM02",)].net_weight == Decimal("12000")
    unique_checks = _checks(result, "MACHINE_CONFIRMED_WEIGHT_UNIQUE_MAP")
    assert len(unique_checks) == 2
    assert all(check["result"] == "pass" for check in unique_checks)


# ---------------------------------------------------------------------------
# 不泄漏其他票 / 来源契约
# ---------------------------------------------------------------------------


def test_other_ticket_assets_and_mail_rows_are_not_parsed(
        tmp_path, monkeypatch):
    import cater.core.parsing.machine_confirmed as machine_confirmed

    machine_a = _machine("SA01", "S5517301")
    machine_b = _machine("SB02", "S5517302")
    path_a = tmp_path / "ticket-A.xlsx"
    path_b = tmp_path / "ticket-B.xlsx"
    built_a = _new_xlsx(path_a, [machine_a],
                        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                          "net": 17000, "gross": 17000}],
                        {"qty": 1, "net": 17000, "gross": 17000},
                        invoice_no="SYN-A")
    built_b = _new_xlsx(path_b, [machine_b],
                        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                          "net": 18000, "gross": 18000}],
                        {"qty": 1, "net": 18000, "gross": 18000},
                        invoice_no="SYN-B")
    candidate = _candidate(
        [_ref(path_a, built_a["regions"])],
        _identity([machine_a], "SYN-A"),
        [_mail_ref("SA01", "S5517301")])

    opened = []
    original = machine_confirmed.load_workbook_pair

    def spy(path):
        opened.append(Path(path).name)
        return original(path)

    monkeypatch.setattr(machine_confirmed, "load_workbook_pair", spy)
    result = parse_confirmed_machine_sources(
        _sources(path_a, path_b), candidate,
        [_mail_ref("SA01", "S5517301"),
         _mail_ref("SB02", "S5517302", row=3)])
    assert opened == [path_a.name]
    assert len(result.invoice_rows) == 1
    assert result.invoice_rows[0].mso == "SA01"
    assert {group.msos[0] for group in result.weight_groups} == {"SA01"}
    assert result.invoice_no == "SYN-A"
    messages = " ".join(issue.message for issue in result.issues)
    assert "SB02" not in messages and "S5517302" not in messages
    assert not _blocking(result)  # 其他票材料只留可见审计，不影响本票
    assert "SOURCE_OUTSIDE_CANDIDATE" in _codes(result)
    assert "MAIL_ROW_OUTSIDE_CANDIDATE" in _codes(result)
    # 其他票的附件没有进入 layout 的来源记录
    layout_text = str(result.layout)
    assert "ticket-B.xlsx" not in layout_text


def test_source_hash_mismatch_rejected(tmp_path):
    machines = [_machine("SH01", "S5517401")]
    path = tmp_path / "hash.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17000, "gross": 17000}],
                      {"qty": 1, "net": 17000, "gross": 17000},
                      invoice_no="SYN-HASH")
    candidate = _candidate([_ref(path, built["regions"])],
                           _identity(machines, "SYN-HASH"),
                           [_mail_ref("SH01", "S5517401")])
    tampered = [{"path": str(path), "filename": path.name,
                 "sha256": "0" * 64}]
    result = parse_confirmed_machine_sources(
        tampered, candidate, candidate["mail_row_refs"])
    assert "SOURCE_HASH_MISMATCH" in _codes(result)
    assert result.invoice_rows == [] and result.weight_groups == []


def test_missing_source_and_region_rejections(tmp_path):
    machines = [_machine("MS01", "S5517501")]
    path = tmp_path / "missing.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17000, "gross": 17000}],
                      {"qty": 1, "net": 17000, "gross": 17000},
                      invoice_no="SYN-MISS")
    candidate = _candidate([_ref(path, built["regions"])],
                           _identity(machines, "SYN-MISS"),
                           [_mail_ref("MS01", "S5517501")])
    missing = parse_confirmed_machine_sources(
        [], candidate, candidate["mail_row_refs"])
    assert "CONFIRMED_SOURCE_MISSING" in _codes(missing)

    bad_sheet = _candidate([{**_ref(path, built["regions"]),
                             "regions": [dict(built["regions"][0],
                                              sheet="NoSuchSheet")]}],
                           _identity(machines, "SYN-MISS"),
                           [_mail_ref("MS01", "S5517501")])
    bad = parse_confirmed_machine_sources(
        _sources(path), bad_sheet, bad_sheet["mail_row_refs"])
    assert "CONFIRMED_REGION_INVALID" in _codes(bad)

    no_refs = _candidate([], _identity(machines, "SYN-MISS"), [])
    invalid = parse_confirmed_machine_sources(
        _sources(path), no_refs, [])
    assert "CONFIRMED_CANDIDATE_INVALID" in _codes(invalid)

    wrong_kind = _candidate([_ref(path, built["regions"])],
                            _identity(machines, "SYN-MISS"),
                            [_mail_ref("MS01", "S5517501")],
                            kind="parts")
    kind_result = parse_confirmed_machine_sources(
        _sources(path), wrong_kind, wrong_kind["mail_row_refs"])
    assert "CANDIDATE_KIND_MISMATCH" in _codes(kind_result)


def test_invoice_no_conflict_across_files(tmp_path):
    machines = [_machine("IN01", "S5517601")]
    path_a = tmp_path / "invoice-A.xlsx"
    path_b = tmp_path / "invoice-B.xlsx"
    built_a = _new_xlsx(path_a, machines,
                        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                          "net": 17000, "gross": 17000}],
                        {"qty": 1, "net": 17000, "gross": 17000},
                        invoice_no="SYN-INV-A")
    built_b = _new_xlsx(path_b, machines, [], {},
                        invoice_no="SYN-INV-B", include_weight=False)
    candidate = _candidate(
        [_ref(path_a, built_a["regions"]), _ref(path_b, built_b["regions"])],
        {"invoice_no": ["SYN-INV-A"], "mso": ["IN01"],
         "serial": ["S5517601"]},
        [_mail_ref("IN01", "S5517601")])
    result = parse_confirmed_machine_sources(
        _sources(path_a, path_b), candidate, candidate["mail_row_refs"])
    assert "INVOICE_NO_CONFLICT" in _codes(result)
    assert result.invoice_no is None
    assert _blocking(result)


def test_machine_row_conflict_across_files_not_file_order(tmp_path):
    machines_a = [_machine("CF01", "S5517701",
                           pin="SEM00655KS5517701", engine="ENG-CF1")]
    machines_b = [_machine("CF01", "S5517701",
                           pin="SEM00655KS5517701", engine="ENG-CF2")]
    path_a = tmp_path / "conflict-A.xlsx"
    path_b = tmp_path / "conflict-B.xlsx"
    built_a = _new_xlsx(path_a, machines_a, [], {},
                        invoice_no="SYN-CF", include_weight=False)
    built_b = _new_xlsx(path_b, machines_b, [], {},
                        invoice_no="SYN-CF", include_weight=False)
    candidate = _candidate(
        [_ref(path_a, built_a["regions"]), _ref(path_b, built_b["regions"])],
        {"invoice_no": ["SYN-CF"], "mso": ["CF01"],
         "serial": ["S5517701"]},
        [_mail_ref("CF01", "S5517701")])
    result = parse_confirmed_machine_sources(
        _sources(path_a, path_b), candidate, candidate["mail_row_refs"])
    assert "MACHINE_ROW_CONFLICT" in _codes(result)
    assert _blocking(result)
    conflict = [issue for issue in result.issues
                if issue.code == "MACHINE_ROW_CONFLICT"][0]
    assert "未按文件顺序" in conflict.message
    # 不因文件顺序静默选定；冲突时保留确定性首行但状态阻断
    assert len(result.invoice_rows) == 1


def test_weight_region_missing_required_column(tmp_path):
    machines = [_machine("WC01", "S5517801")]
    path = tmp_path / "weight-columns.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17000, "gross": 17000}],
                      {"qty": 1, "net": 17000, "gross": 17000},
                      invoice_no="SYN-WCOL")
    regions = [built["regions"][0],
               {**built["regions"][1],
                "columns": {"description": "B", "qty": "C"}}]
    candidate = _candidate([_ref(path, regions)],
                           _identity(machines, "SYN-WCOL"),
                           [_mail_ref("WC01", "S5517801")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "WEIGHT_TABLE_INVALID" in _codes(result)
    assert result.weight_groups == []
    assert _blocking(result)


@pytest.mark.skipif(xlwt is None, reason="xlwt 未安装，跳过旧 xls 夹具")
def test_serial_embedded_only_in_pin_is_located(tmp_path):
    path = tmp_path / "pin-only.xls"
    built = _build_old_xls(path, serial="S5515232",
                           invoice_no="SYN-PIN-ONLY", serial_cell=False)
    candidate = _candidate(
        [_ref(path, built["regions"])],
        {"invoice_no": ["SYN-PIN-ONLY"], "mso": ["BITOE"],
         "serial": ["S5515232"]},
        [_mail_ref("BITOE", "S5515232")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert not _blocking(result), [i.to_dict() for i in _blocking(result)]
    row = result.invoice_rows[0]
    assert row.loader == "S5515232"
    assert (row.source.sheet, row.source.cell) == ("CI", "E31")


def test_snapshot_stored_filename_prefix_keeps_original_provenance(tmp_path):
    machines = [_machine("PF01", "S5517901")]
    path = tmp_path / "Ticket-original.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17000, "gross": 17000}],
                      {"qty": 1, "net": 17000, "gross": 17000},
                      invoice_no="SYN-PF")
    candidate = _candidate([_ref(path, built["regions"])],
                           _identity(machines, "SYN-PF"),
                           [_mail_ref("PF01", "S5517901")])
    stored = tmp_path / f"{_sha(path)[:16]}_Ticket-original.xlsx"
    stored.write_bytes(path.read_bytes())
    sources = [{"path": str(stored),
                "filename": stored.name, "sha256": _sha(stored)}]
    result = parse_confirmed_machine_sources(
        sources, candidate, candidate["mail_row_refs"])
    assert not _blocking(result)
    assert result.invoice_rows[0].source.filename == "Ticket-original.xlsx"
    assert result.weight_groups[0].source.filename == "Ticket-original.xlsx"


def test_ci_text_net_weight_cross_check(tmp_path):
    machines = [_machine("CN01", "S5518001")]
    path = tmp_path / "ci-net.xlsx"
    built = _new_xlsx(path, machines,
                      [{"label": "SEM655D WHEEL LOADER", "qty": 1,
                        "net": 17000, "gross": 17000}],
                      {"qty": 1, "net": 17000, "gross": 17000},
                      invoice_no="SYN-CN")
    wb = openpyxl.load_workbook(path)
    wb["Invoice"]["A12"] = "NET WEIGHT: 16500 KG"
    wb.save(path)
    # 重新计算候选 sha（文件字节已变化）
    candidate = _candidate([_ref(path, built["regions"])],
                           _identity(machines, "SYN-CN"),
                           [_mail_ref("CN01", "S5518001")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "WEIGHT_MISMATCH" in _codes(result)
    assert _blocking(result)
    check = _checks(result, "MACHINE_CI_TEXT_VS_PL_NET")
    assert check and check[0]["result"] == "fail"


# ---------------------------------------------------------------------------
# 审核返工：数量文本必须完整、有限、非负、整数，不得截取任意数字
# ---------------------------------------------------------------------------

_INVALID_QTY_TEXTS = [
    "",
    "   ",
    "1.5 UNITS",
    "1.50 UNITS",
    "-1 UNIT",
    "bad 1 text",
    "1 2",
    "1,5 UNITS",
    "1 UNIT extra",
    "1 UNIT (1 PACKAGE)",
    "NaN",
    "Infinity",
    "-Infinity",
    "sNaN",
    "1e3",
    "1E+3",
    "1.5",
    "1.0.0",
    "++1",
    "1;2",
]


@pytest.mark.parametrize("bad_qty", _INVALID_QTY_TEXTS)
def test_invalid_quantity_text_never_creates_weight_group(tmp_path, bad_qty):
    """以前 ``_QTY_RE.search`` 会把 1.5/-1/bad 1 都截成 1；现在必须阻断。"""
    machines = [_machine("MHZ01", "S5515001")]
    path = tmp_path / "bad-qty.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": bad_qty,
          "net": 17200, "gross": 17200}],
        {"qty": 1, "net": 17200, "gross": 17200},
        invoice_no="SYN-BAD-QTY")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-BAD-QTY"),
        [_mail_ref("MHZ01", "S5515001")])

    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])

    assert _blocking(result), (bad_qty, [i.to_dict() for i in result.issues])
    assert result.weight_groups == []
    assert any(issue.code in ("WEIGHT_GROUP_INCOMPLETE",
                              "WEIGHT_TABLE_INVALID",
                              "WEIGHT_GROUP_QTY_MISMATCH")
               for issue in result.issues)
    # 源坐标指向数量单元格 C9，而不是只给一个含糊的整表来源
    bad_sources = [source
                   for issue in result.issues
                   if issue.code == "WEIGHT_GROUP_INCOMPLETE"
                   for source in issue.sources]
    assert any(source.cell == "C9" for source in bad_sources)


def test_zero_quantity_is_left_to_group_rules(tmp_path):
    """零本身可解析（非负整数），但现有组规则 qty == MSO 数必须拒绝它。"""
    machines = [_machine("ZERO01", "S5515002")]
    path = tmp_path / "zero-qty.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": "0",
          "net": 17200, "gross": 17200}],
        {"qty": 0, "net": 17200, "gross": 17200},
        invoice_no="SYN-ZERO")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-ZERO"),
        [_mail_ref("ZERO01", "S5515002")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "WEIGHT_GROUP_QTY_MISMATCH" in _codes(result)
    assert _blocking(result)
    assert result.weight_groups == []


@pytest.mark.parametrize("valid_qty", [
    1,
    1.0,
    "1",
    "1.0",
    "1.",
    "+1",
    "01",
    " 1 ",
    "1 UNIT",
    "1 units",
    "1 PCS",
    "1 piece",
    "1 SET",
])
def test_valid_quantity_values_still_build_group(tmp_path, valid_qty):
    machines = [_machine("OKQ01", "S5515003")]
    path = tmp_path / "valid-qty.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": valid_qty,
          "net": 17200, "gross": 17200}],
        {"qty": valid_qty, "net": 17200, "gross": 17200},
        invoice_no="SYN-OK-QTY")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-OK-QTY"),
        [_mail_ref("OKQ01", "S5515003")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert not _blocking(result), [i.to_dict() for i in _blocking(result)]
    assert len(result.weight_groups) == 1
    assert result.weight_groups[0].qty == 1
    total_checks = _checks(result, "MACHINE_PACKING_TOTAL_ROW")
    assert total_checks and total_checks[0]["result"] == "pass"


@pytest.mark.parametrize("valid_qty", [
    2,
    2.0,
    "2",
    "2.0",
    "2 UNITS",
    "2 units",
    "2 PCS",
    "2 SETS",
])
def test_valid_two_unit_quantity_multi_machine(tmp_path, valid_qty):
    machines = [_machine("MQ01", "S5515004", engine="ENG-M1"),
                _machine("MQ02", "S5515005", engine="ENG-M2")]
    path = tmp_path / "valid-two-qty.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D--MQ01/MQ02", "qty": valid_qty,
          "net": 20000, "gross": 20000}],
        {"qty": valid_qty, "net": 20000, "gross": 20000},
        invoice_no="SYN-OK-TWO")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-OK-TWO"),
        [_mail_ref("MQ01", "S5515004"),
         _mail_ref("MQ02", "S5515005", row=3)])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert not _blocking(result), [i.to_dict() for i in _blocking(result)]
    assert len(result.weight_groups) == 1
    assert result.weight_groups[0].qty == 2
    assert result.weight_groups[0].msos == ["MQ01", "MQ02"]


def test_non_finite_net_and_gross_in_detail_are_structured(tmp_path):
    machines = [_machine("NF01", "S5515006")]
    path = tmp_path / "nonfinite-detail.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
          "net": "NaN", "gross": "Infinity"}],
        {"qty": 1, "net": "NaN", "gross": "Infinity"},
        invoice_no="SYN-NF-DETAIL")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-NF-DETAIL"),
        [_mail_ref("NF01", "S5515006")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert _blocking(result)
    assert result.weight_groups == []
    sources = [source for issue in result.issues
               if issue.code == "WEIGHT_GROUP_INCOMPLETE"
               for source in issue.sources]
    assert any(source.cell == "D9" for source in sources)
    assert any(source.cell == "E9" for source in sources)
    assert "NaN" in " ".join(issue.message for issue in result.issues)


def test_non_finite_total_values_are_structured_not_arithmetic_error(tmp_path):
    machines = [_machine("NF02", "S5515007")]
    path = tmp_path / "nonfinite-total.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
          "net": 17200, "gross": 17200}],
        {"qty": 1, "net": "Infinity", "gross": "NaN"},
        invoice_no="SYN-NF-TOTAL")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-NF-TOTAL"),
        [_mail_ref("NF02", "S5515007")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "WEIGHT_TOTAL_MISMATCH" in _codes(result)
    assert _blocking(result)
    total_sources = [source for issue in result.issues
                     if issue.code == "WEIGHT_TOTAL_MISMATCH"
                     for source in issue.sources]
    assert any(source.cell == "D10" for source in total_sources)
    assert any(source.cell == "E10" for source in total_sources)


def test_numeric_non_finite_and_huge_values_do_not_raise():
    """直接数值来源（xls 错误单元格/极端 Decimal）也必须结构化拒绝。"""
    from cater.core.parsing.machine_confirmed import (_parse_decimal,
                                                      _parse_qty)

    non_finite = [float("nan"), float("inf"), float("-inf"),
                  Decimal("NaN"), Decimal("Infinity"),
                  Decimal("-Infinity"), Decimal("sNaN")]
    for value in non_finite:
        qty, qty_error = _parse_qty(value)
        assert qty is None and qty_error, value
        number, number_error = _parse_decimal(value)
        assert number is None and number_error, value

    for value in (Decimal("1E+1000000"), "1E+1000000"):
        qty, qty_error = _parse_qty(value)
        assert qty is None and qty_error, value
        number, number_error = _parse_decimal(value)
        assert number is None and number_error, value


@pytest.mark.parametrize("bad_qty", [1.5, -1, -1.0, 0.5, True])
def test_invalid_numeric_quantity_cell_never_creates_group(tmp_path, bad_qty):
    """Excel 数值单元格同样不能截断小数/丢负号；bool 也不是数量。"""
    machines = [_machine("NQ01", "S5515008")]
    path = tmp_path / "bad-numeric-qty.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": bad_qty,
          "net": 17200, "gross": 17200}],
        {"qty": 1, "net": 17200, "gross": 17200},
        invoice_no="SYN-NQ")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-NQ"),
        [_mail_ref("NQ01", "S5515008")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert _blocking(result)
    assert result.weight_groups == []


@pytest.mark.parametrize("bad_total_qty", [
    "1.5 UNITS", "-1 UNIT", "bad 1 text", "NaN", "Infinity", "1e3",
])
def test_invalid_total_quantity_uses_same_strict_rules(tmp_path,
                                                       bad_total_qty):
    """明细与 TOTAL 共用同一数量校验；TOTAL 非法必须阻断并给 C10 坐标。"""
    machines = [_machine("TQ01", "S5515009")]
    path = tmp_path / "bad-total-qty.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
          "net": 17200, "gross": 17200}],
        {"qty": bad_total_qty, "net": 17200, "gross": 17200},
        invoice_no="SYN-TQ")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-TQ"),
        [_mail_ref("TQ01", "S5515009")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "WEIGHT_TOTAL_MISMATCH" in _codes(result)
    assert _blocking(result)
    total_sources = [source for issue in result.issues
                     if issue.code == "WEIGHT_TOTAL_MISMATCH"
                     for source in issue.sources]
    assert any(source.cell == "C10" for source in total_sources)
    assert result.weight_groups == []


@pytest.mark.parametrize("huge_value", ["1E+1000000", "1e100", "1E-1000000"])
def test_extreme_finite_weight_values_are_structured(tmp_path, huge_value):
    """有限但极端指数的净重同样拒绝，避免求和/比较触发 Decimal Overflow。"""
    machines = [_machine("HV01", "S5515010")]
    path = tmp_path / "huge-weight.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
          "net": huge_value, "gross": 17200}],
        {"qty": 1, "net": 17200, "gross": 17200},
        invoice_no="SYN-HV")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-HV"),
        [_mail_ref("HV01", "S5515010")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert _blocking(result)
    assert result.weight_groups == []
    sources = [source for issue in result.issues
               if issue.code == "WEIGHT_GROUP_INCOMPLETE"
               for source in issue.sources]
    assert any(source.cell == "D9" for source in sources)


def test_extreme_finite_total_weight_is_structured(tmp_path):
    machines = [_machine("HV02", "S5515011")]
    path = tmp_path / "huge-total.xlsx"
    built = _new_xlsx(
        path, machines,
        [{"label": "SEM655D WHEEL LOADER", "qty": 1,
          "net": 17200, "gross": 17200}],
        {"qty": 1, "net": "1E+1000000", "gross": 17200},
        invoice_no="SYN-HV-T")
    candidate = _candidate(
        [_ref(path, built["regions"])],
        _identity(machines, "SYN-HV-T"),
        [_mail_ref("HV02", "S5515011")])
    result = parse_confirmed_machine_sources(
        _sources(path), candidate, candidate["mail_row_refs"])
    assert "WEIGHT_TOTAL_MISMATCH" in _codes(result)
    assert _blocking(result)
    total_sources = [source for issue in result.issues
                     if issue.code == "WEIGHT_TOTAL_MISMATCH"
                     for source in issue.sources]
    assert any(source.cell == "D10" for source in total_sources)
