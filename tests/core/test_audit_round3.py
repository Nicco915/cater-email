"""第三轮审计修复验收（协调者 2026-11-05 复核意见）。

覆盖：整机 SERIAL 列缺失不得 KeyError、机器信息表扫描到文件尾把第二张
表块的头/数据行吞成假记录、箱单 TOTAL 行数量/净重空白时合计核对必须
skipped 不得 pass、clean_spec 1|| 窄规范化必须记录来源与可读摘要（保留
内部竖线）。

合成数据说明：本文件全部工作簿/邮件均为 FAKE/SYN 合成夹具。
"""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from cater.core.api import process_job
from cater.core.parsing.machine_wb import parse_machine_workbook
from cater.core.transform import build_parts_rows

from conftest import REPO_ROOT


# ---------------------------------------------------------------------------
# 合成构造器
# ---------------------------------------------------------------------------

def _machine_book(path: Path, *, serial=True, second_block=()):
    """CI 机器信息表（MODEL/MSO/SERIAL 可选）+ PL 重量表的工作簿。

    second_block：表头行之后的额外行（元组 (model, mso, serial) 或
    (title,...) 直接写入 B/C/E 列），用于复现“扫描到 EOF 吞掉第二表块”。
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI"
    ws.cell(2, 2, "MODEL")
    ws.cell(2, 3, "MSO")
    if serial:
        ws.cell(2, 5, "SERIAL #")
    ws.cell(3, 2, "SEMTEST")
    ws.cell(3, 3, "FAKE001")
    if serial:
        ws.cell(3, 5, "SYN000001A")
    r = 4
    for extra in second_block:
        for col, value in zip((2, 3, 5), extra):
            if value is not None:
                ws.cell(r, col, value)
        r += 1
    pl = wb.create_sheet("PL")
    pl.cell(5, 1, "DESCRIPTION OF GOODS")
    pl.cell(5, 4, "QTY")
    pl.cell(5, 6, "NET WEIGHT (KGS)")
    pl.cell(6, 1, "CMOPS MSO#: FAKE001")
    pl.cell(6, 4, 1)
    pl.cell(6, 6, 12000)
    wb.save(path)
    return path


MACHINE_REFS = {"machines": [{
    "model": "SEMTEST", "english_name": "SEMTEST WHEEL LOADER",
    "hs_code": "8429510000", "product_name": "装载机",
    "declaration": "1|0|合成申报要素|无|无|无|无|无|无",
    "destination_scope": "default"}], "countries": [], "machine_aliases": {}}

MACHINE_HTML = ("<html><body><table>"
                "<tr><th>Dealer Code</th><th>Dealer Order</th>"
                "<th>Cust. Purch. Order (MSO#)</th><th>Order</th>"
                "<th>Material</th><th>Serial Number</th>"
                "<th>Total Sales Price</th></tr>"
                "<tr><td>SYN01</td><td>SYN-ORD-1</td><td>FAKE001</td>"
                "<td>06200000101</td><td>SEMTEST</td><td>SYN000001A</td>"
                "<td>50000.00</td></tr>"
                "</table></body></html>")


def _machine_request(output_dir, path, *, task_id="TEST-AUDIT-R3"):
    return {
        "task_id": task_id,
        "kind": "machine",
        "destination": {"name": "巴西", "alpha3": "BRA"},
        "sources": [{"path": str(path), "filename": Path(path).name,
                     "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}],
        "mail": {"html": MACHINE_HTML, "text": None,
                 "message_id": "<synthetic-r3@example.com>"},
        "references": MACHINE_REFS,
        "output_dir": output_dir,
        "overrides": [],
    }


# ---------------------------------------------------------------------------
# 复核(1)：SERIAL 列为可选——缺失时不得 KeyError
# ---------------------------------------------------------------------------

def test_machine_table_without_serial_column_no_keyerror(output_dir, tmp_path):
    book = _machine_book(tmp_path / "no_serial.xlsx", serial=False)
    result = parse_machine_workbook(str(book), book.name)
    assert not result.issues, [i.code for i in result.issues]
    assert len(result.invoice_rows) == 1
    assert result.invoice_rows[0].mso == "FAKE001"
    assert result.invoice_rows[0].loader is None  # 可选列缺失=None，不崩溃

    resp = process_job(_machine_request(output_dir, book,
                                        task_id="TEST-AUDIT-R3-SER"))
    assert resp["status"] == "ready", resp["issues"]


# ---------------------------------------------------------------------------
# 复核(2)：机器信息表扫描不得吞掉表头下方的第二表块
# ---------------------------------------------------------------------------

def test_machine_table_scan_stops_at_repeated_header(output_dir, tmp_path):
    """表头下方紧接一段非完整候选的重复表头 + 数据行：旧实现把
    "MSO NO." 行当 literal 记录吞掉且无问题；必须暂停登记。"""
    book = _machine_book(
        tmp_path / "second_block.xlsx",
        second_block=[("MODEL", "MSO NO.", "SERIAL"),   # 部分重复表头（非候选）
                      ("SEMTEST", "FAKE002", "SYN000002B")])
    result = parse_machine_workbook(str(book), book.name)
    codes = [i.code for i in result.issues]
    assert "MACHINE_TABLE_INVALID" in codes, codes
    # 暂停：不得把重复表头/第二表块数据当成机器记录，也不得部分放行
    msos = [r.mso for r in result.invoice_rows]
    assert msos == []


def test_machine_table_scan_stops_at_blank_gap(output_dir, tmp_path):
    """空行之后的其他表块（合法多块布局）不得被吞成机器记录。"""
    book = _machine_book(
        tmp_path / "blank_gap.xlsx",
        second_block=[(None, None, None),               # 空行 = 表块终止
                      ("ITEM", "FAKE9", "NOTE")])
    result = parse_machine_workbook(str(book), book.name)
    assert not result.issues, [i.code for i in result.issues]
    msos = [r.mso for r in result.invoice_rows]
    assert msos == ["FAKE001"]


# ---------------------------------------------------------------------------
# 复核(3)：TOTAL 行数量/净重空白 → 合计核对 skipped，绝不 pass
# ---------------------------------------------------------------------------

def test_packing_total_blank_qty_net_is_skipped_not_pass(output_dir, tmp_path):
    book = tmp_path / "blank_total.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI"
    ws.cell(2, 2, "MODEL"); ws.cell(2, 3, "MSO"); ws.cell(2, 5, "SERIAL #")
    ws.cell(3, 2, "SEMTEST"); ws.cell(3, 3, "FAKE001"); ws.cell(3, 5, "SYN000001A")
    pl = wb.create_sheet("PL")
    pl.cell(5, 1, "DESCRIPTION OF GOODS")
    pl.cell(5, 4, "QTY")
    pl.cell(5, 6, "NET WEIGHT (KGS)")
    pl.cell(6, 1, "CMOPS MSO#: FAKE001")
    pl.cell(6, 4, 1)
    pl.cell(6, 6, 10)          # 组 net=10 qty=1
    pl.cell(8, 1, "TOTAL")     # TOTAL 行数量/净重空白
    wb.save(book)

    result = parse_machine_workbook(str(book), book.name)
    total_checks = [c for c in result.layout["checks"]
                    if c["rule"] == "MACHINE_PACKING_TOTAL_ROW"]
    assert total_checks, "TOTAL 行存在就必须登记合计核对结果"
    assert total_checks[0]["result"] == "skipped", total_checks
    assert "None" not in total_checks[0]["detail"]

    resp = process_job(_machine_request(output_dir, book,
                                        task_id="TEST-AUDIT-R3-TOT"))
    assert resp["status"] == "ready", resp["issues"]
    checks = [c for c in resp["layout"]["checks"]
              if c["rule"] == "MACHINE_PACKING_TOTAL_ROW"]
    assert checks and checks[0]["result"] == "skipped"


# ---------------------------------------------------------------------------
# 复核(5)：clean_spec 窄规范化必须可报告（来源 + 可读摘要，保留内部竖线）
# ---------------------------------------------------------------------------

class _Index:
    def resolve(self, raw):
        return {"CN": "CHN"}.get(str(raw).strip().upper())


def _fv(value, cell):
    from cater.core.models import FieldValue, Source
    return FieldValue(value, value, Source(type="workbook", filename="CI.xlsx",
                                           sheet="CI", cell=cell))


def _spec_record(spec: str):
    from cater.core.models import PartRecord
    fields = {
        "qty": _fv("2", "F10"),
        "unit_price": _fv("3.5", "H10"),
        "net_weight": _fv("4.0", "K10"),
        "hs_code": _fv("8413810000", "M10"),
        "product_name": _fv("合成件", "N10"),
        "spec": _fv(spec, "O10"),
        "coo": _fv("CN", "E10"),
    }
    return PartRecord(part_code="SYNP100", fields=fields)


def test_clean_spec_normalization_recorded_with_source():
    issues: list = []
    rows, json_records, provenance, normalizations = build_parts_rows(
        [_spec_record("1||3|用于SEM品牌|无")], _Index(),
        {"name": "多哥", "alpha3": "TGO"}, None, None, None,
        None, None, None, issues)
    assert rows[0]["spec"] == "1|3|用于SEM品牌|无"
    assert len(normalizations) == 1
    entry = normalizations[0]
    assert entry["record_key"] == "SYNP100"
    assert entry["field"] == "spec"
    assert entry["before"] == "1||3|用于SEM品牌|无"   # 摘要保留内部竖线
    assert entry["after"] == "1|3|用于SEM品牌|无"
    assert entry["source"]["sheet"] == "CI" and entry["source"]["cell"] == "O10"
    assert "1||3|用于SEM品牌|无" in entry["summary"]


def test_clean_spec_noop_not_recorded():
    issues: list = []
    rows, _, _, normalizations = build_parts_rows(
        [_spec_record("1|3|用于SEM品牌|无")], _Index(),
        {"name": "多哥", "alpha3": "TGO"}, None, None, None,
        None, None, None, issues)
    assert rows[0]["spec"] == "1|3|用于SEM品牌|无"
    assert normalizations == []


def test_clean_spec_normalization_reaches_layout_block(output_dir, tmp_path):
    """端到端：process_job 的 layout 必须携带自动规范化记录（来源+摘要）。"""
    from test_audit_round2 import GOOD_ROWS, _parts_book, _parts_request

    book = _parts_book(tmp_path / "spec_norm.xlsx", header_row=17,
                        rows=GOOD_ROWS)
    wb = openpyxl.load_workbook(book)
    for row in wb["CI-For Dealer (2)"].iter_rows():
        for cell in row:
            if cell.value == "1|3|合成要素|无":
                cell.value = "1||3|合成要素|无"   # 开头多余空项
    wb.save(book)

    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "ready", resp["issues"]
    corrections = resp["layout"].get("corrections") or []
    # GOOD_ROWS 两行的规格型号都被改为 1|| 开头 → 两条自动规范化记录
    assert len(corrections) == 2, corrections
    entry = corrections[0]
    assert entry["record_key"] == "SYNP100" and entry["field"] == "spec"
    assert entry["before"] == "1||3|合成要素|无"
    assert entry["after"] == "1|3|合成要素|无"
    assert entry["source"]["cell"]  # 来源单元格可追溯
    assert "1||3|合成要素|无" in entry["summary"]
