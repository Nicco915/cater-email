"""第二轮独立审计修复验收（协调者 2026-11-05 审计意见 1/2/3/4/5/6/7/8）。

覆盖：COO 缺失结构化暂停（非 KeyError）、料号行缺数量/单价阻塞（费用行
不误伤）、HS 单侧缺失登记 skipped（不冒称通过）、多候选表头区/首区歧义
拒绝（不静默落到后表）、有界全有效区扫描（深表头>200 行）与语义 TOTAL
锚定（多块合计不误判、同块多合计暂停）、结构指纹只含定位信息（同模板新
一票同指纹）、机器解析线程化 header_aliases。

合成数据说明：本文件全部工作簿/邮件均为 FAKE/SYN 合成夹具。
"""
from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from cater.core.api import process_job
from cater.core.parsing.headers import merged_aliases
from cater.core.parsing.machine_wb import parse_machine_workbook

from conftest import REPO_ROOT

FIXTURES = Path(__file__).parent / "fixtures"
FLEXIBLE_MACHINE_XLS = FIXTURES / "flexible_machine.xls"


# ---------------------------------------------------------------------------
# 合成构造器
# ---------------------------------------------------------------------------

def _write_invoice_sheet(ws, *, header_row, coo=True, hs_titled="HS Code",
                         qty_titles=("QTY",), rows=(), extra_fee_row=True):
    """在 ws 上写一个合成最终发票表块；rows = (part_code, qty, price, net, supp_hs)。"""
    cols = {"part_code": 2, "hs": 4, "coo": 5, "qty": 6, "unit": 7,
            "price": 8, "amount": 9}
    titles = {"part_code": "Part Code", "hs": hs_titled, "coo": "COO",
              "qty": qty_titles[0], "unit": "UNIT", "price": "UNIT PRICE",
              "amount": "AMOUNT"}
    for key, c in cols.items():
        if key == "coo" and not coo:
            continue
        ws.cell(header_row, c, titles[key])
    for i, extra in enumerate(qty_titles[1:]):
        ws.cell(header_row, 10 + i, extra)  # 重复数量列 → 多候选
    sub = header_row + 1
    ws.cell(sub, cols["price"], "FCA SYN PORT IN USD")
    ws.cell(sub, cols["amount"] + 1, "U'weight/kg")
    ws.cell(sub, cols["amount"] + 2, "T'weight/kg")
    supp = cols["amount"] + 3
    r = sub + 1
    for row_data in rows:
        part_code, qty, price, net, supp_hs = row_data[:5]
        titled_hs = row_data[5] if len(row_data) > 5 else "8413810000"
        ws.cell(r, cols["part_code"], part_code)
        if titled_hs is not None:
            ws.cell(r, cols["hs"], titled_hs)
        if coo:
            ws.cell(r, cols["coo"], "CN")
        ws.cell(r, cols["qty"], qty)
        ws.cell(r, cols["unit"], "PCS")
        if price is not None:
            ws.cell(r, cols["price"], price)
        ws.cell(r, cols["amount"], qty if isinstance(qty, (int, float)) else None)
        ws.cell(r, cols["amount"] + 1, 2.0)
        if net is not None:
            ws.cell(r, cols["amount"] + 2, net)
        if supp_hs is not None:
            ws.cell(r, supp, supp_hs)
        ws.cell(r, supp + 1, "合成件")
        ws.cell(r, supp + 2, "1|3|合成要素|无")
        r += 1
    if extra_fee_row:
        ws.cell(r, 1, "APPLY TO CUSTOMS CHARGE")  # 显式费用行（无料号）
        ws.cell(r, cols["price"], 50.0)
        r += 1
    return r  # 下一个可用行


def _write_packing_sheet(ws, *, header_row=5, total_row=9, cols=(2, 3),
                         totals=(2, 99.5), second_total=None):
    """合成箱单：PACKAGE / GROSS WEIGHT 列 + TOTAL 行。"""
    pkg_col, gross_col = cols
    ws.cell(header_row, pkg_col, "PACKAGE")
    ws.cell(header_row, gross_col, "GROSS WEIGHT/KG")
    ws.cell(total_row, 1, "TOTAL")
    ws.cell(total_row, pkg_col, totals[0])
    ws.cell(total_row, gross_col, totals[1])
    if second_total:
        r2, vals = second_total
        ws.cell(r2, 1, "Total")
        ws.cell(r2, pkg_col, vals[0])
        ws.cell(r2, gross_col, vals[1])


def _parts_book(path: Path, **invoice_kwargs):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(10, 5, "INVOICE NO.: SYN-AUDIT-0001")
    _write_invoice_sheet(ws, **invoice_kwargs)
    pl = wb.create_sheet("Packing List")
    _write_packing_sheet(pl)
    wb.save(path)
    return path


def _parts_request(output_dir, path):
    return {
        "task_id": "TEST-AUDIT-2",
        "kind": "parts",
        "destination": {"name": "多哥", "alpha3": "TGO"},
        "sources": [{"path": str(path), "filename": Path(path).name,
                     "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}],
        "mail": {},
        "references": {"machines": [], "countries": [], "machine_aliases": {}},
        "output_dir": output_dir,
        "overrides": [],
    }


GOOD_ROWS = [("SYNP100", 2, 3.5, 4.0, "8413810000"),
             ("SYNP101", 1, 9.99, 2.0, "8413810000")]


# ---------------------------------------------------------------------------
# 审计(2)：COO 缺失 → 结构化暂停，禁止 KeyError 技术故障
# ---------------------------------------------------------------------------

def test_absent_coo_header_pauses_structured(output_dir, tmp_path):
    book = _parts_book(tmp_path / "no_coo.xlsx", header_row=17, coo=False,
                       rows=GOOD_ROWS)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "waiting_input"
    issue = next(i for i in resp["issues"] if i["code"] == "FINAL_SHEET_INVALID")
    assert "'coo'" in issue["message"] or "coo" in issue["message"]
    assert resp["artifacts"] == []


# ---------------------------------------------------------------------------
# 审计(3)：料号行缺数量/单价 → 阻塞暂停；费用行不误伤
# ---------------------------------------------------------------------------

def test_missing_price_on_part_row_blocks(output_dir, tmp_path):
    rows = [("SYNP200", 2, None, 4.0, "8413810000"),  # 缺单价
            ("SYNP201", 1, 9.99, 2.0, "8413810000")]
    book = _parts_book(tmp_path / "no_price.xlsx", header_row=17, rows=rows)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "waiting_input"
    issue = next(i for i in resp["issues"] if i["code"] == "INVALID_DETAIL_ROW")
    assert issue["record_key"] == "SYNP200"
    assert issue["field"] == "unit_price"
    # 该行未纳入输出：有效记录只剩一条，且总净重不受丢行影响而“凑平”
    assert len(resp["records"]) == 1
    assert resp["records"][0]["record_key"] == "SYNP201"
    # 显式费用行（无料号）不产生问题
    assert not any(i["code"] == "INVALID_DETAIL_ROW" and "CUSTOMS" in
                   str(i.get("record_key") or "") for i in resp["issues"])


def test_invalid_qty_text_on_part_row_blocks(output_dir, tmp_path):
    rows = [("SYNP210", "ABC", 3.5, None, "8413810000")]  # 数量不可解析
    book = _parts_book(tmp_path / "bad_qty.xlsx", header_row=17, rows=rows)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "waiting_input"
    issue = next(i for i in resp["issues"] if i["code"] == "INVALID_DETAIL_ROW")
    assert issue["record_key"] == "SYNP210"
    assert issue["field"] == "qty"


# ---------------------------------------------------------------------------
# 审计(4)：HS 只有一侧有值 → 登记 skipped，绝不冒称通过
# ---------------------------------------------------------------------------

def test_hs_check_skipped_when_one_side_missing(output_dir, tmp_path):
    # SYNP300 表头 HS 列为空（补充列有值）：输出仍取补充列（ready），
    # 但两侧比对未执行 → 该行 HS 核对必须登记 skipped，不得冒称通过。
    rows = [("SYNP300", 1, 3.5, 2.0, "8413810000", None),
            ("SYNP301", 1, 4.5, 2.0, "8413810000")]
    book = _parts_book(tmp_path / "hs_half.xlsx", header_row=17, rows=rows)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "ready", resp["issues"]
    hs_checks = [c for c in resp["layout"]["checks"]
                 if c["rule"] == "PARTS_HS_TITLED_VS_SUPP"]
    by_key = {c["record_key"]: c for c in hs_checks}
    assert by_key["SYNP300"]["result"] == "skipped"
    assert by_key["SYNP301"]["result"] == "pass"
    # skipped 的一行不得出现 pass 记录
    assert not any(c["record_key"] == "SYNP300" and c["result"] == "pass"
                   for c in hs_checks)


# ---------------------------------------------------------------------------
# 审计(5)：多候选表头区必须枚举并拒绝，禁止静默落到后一张表
# ---------------------------------------------------------------------------

def test_second_business_table_in_same_sheet_pauses(output_dir, tmp_path):
    book = tmp_path / "two_tables.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(10, 5, "INVOICE NO.: SYN-AUDIT-0002")
    next_row = _write_invoice_sheet(ws, header_row=17, rows=GOOD_ROWS)
    ws.cell(next_row + 1, 1, "")  # 空行分隔
    _write_invoice_sheet(ws, header_row=next_row + 2, rows=GOOD_ROWS,
                         extra_fee_row=False)
    _write_packing_sheet(wb.create_sheet("Packing List"))
    wb.save(book)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "waiting_input"
    issue = next(i for i in resp["issues"] if i["code"] == "FINAL_SHEET_INVALID")
    assert "多个表头区" in issue["message"]


def test_ambiguous_first_region_not_bypassed_by_later_table(output_dir, tmp_path):
    """首区数量列多候选：必须暂停，不得静默采用后面干净的表。"""
    book = tmp_path / "ambig_first.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(10, 5, "INVOICE NO.: SYN-AUDIT-0003")
    next_row = _write_invoice_sheet(ws, header_row=17,
                                    qty_titles=("QTY", "TOTAL QTY"),
                                    rows=GOOD_ROWS)
    ws.cell(next_row + 1, 1, "")
    _write_invoice_sheet(ws, header_row=next_row + 2, rows=GOOD_ROWS,
                         extra_fee_row=False)
    _write_packing_sheet(wb.create_sheet("Packing List"))
    wb.save(book)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "waiting_input"
    issue = next(i for i in resp["issues"] if i["code"] == "FINAL_SHEET_INVALID")
    assert "多候选" in issue["message"]
    assert resp["records"] == []  # 没有静默解析任何一张表


def test_machine_two_tables_in_one_sheet_pause(output_dir, tmp_path):
    book = tmp_path / "machine_two.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI"
    ws.cell(8, 1, "ITEM#"); ws.cell(8, 2, "MODEL"); ws.cell(8, 3, "MSO")
    ws.cell(9, 2, "SEMTEST"); ws.cell(9, 3, "FAKE001")
    ws.cell(13, 1, "ITEM#"); ws.cell(13, 2, "MODEL"); ws.cell(13, 3, "MSO")
    ws.cell(14, 2, "SEMOLD"); ws.cell(14, 3, "FAKEH9")
    pl = wb.create_sheet("PL")
    pl.cell(5, 1, "DESCRIPTION OF GOODS"); pl.cell(5, 4, "QTY")
    pl.cell(5, 6, "NET WEIGHT (KGS)")
    pl.cell(6, 1, "CMOPS MSO#: FAKE001"); pl.cell(6, 4, 1); pl.cell(6, 6, 12000)
    wb.save(book)
    result = parse_machine_workbook(str(book), "machine_two.xlsx")
    assert any(i.code == "MACHINE_TABLE_INVALID" for i in result.issues)
    assert result.invoice_rows == []


# ---------------------------------------------------------------------------
# 审计(6)：有界全有效区扫描（深表头）+ 语义 TOTAL 锚定
# ---------------------------------------------------------------------------

def test_header_beyond_row_200_parses(output_dir, tmp_path):
    book = _parts_book(tmp_path / "deep.xlsx", header_row=280, rows=GOOD_ROWS)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "ready", resp["issues"]
    assert len(resp["records"]) == 2
    assert resp["layout"]["parts_invoice"]["header_row"] == 280


def test_total_row_position_shift_regression(output_dir, tmp_path):
    """TOTAL 行随明细行数移动后仍语义锚定；列整体右移同样可定位。"""
    book = tmp_path / "shifted_total.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(10, 5, "INVOICE NO.: SYN-AUDIT-0004")
    _write_invoice_sheet(ws, header_row=17, rows=GOOD_ROWS * 3)
    pl = wb.create_sheet("Packing List")
    _write_packing_sheet(pl, header_row=9, total_row=22, cols=(4, 7),
                         totals=(6, 300.25))
    wb.save(book)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "ready", resp["issues"]
    row = resp["records"][0]["row"]
    assert row["packages"] == "6"
    assert row["gross_weight"] == "300.25"


def test_multiple_totals_same_block_pause(output_dir, tmp_path):
    """同一表块内两张合计行（无空行分隔）→ 暂停，不猜测取哪张。"""
    book = tmp_path / "two_totals.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(10, 5, "INVOICE NO.: SYN-AUDIT-0005")
    _write_invoice_sheet(ws, header_row=17, rows=GOOD_ROWS)
    pl = wb.create_sheet("Packing List")
    # TOTAL 行后紧跟第二张合计行（中间无空行）→ 同块多合计
    _write_packing_sheet(pl, total_row=9, second_total=(10, (2, 50.0)))
    wb.save(book)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "MULTIPLE_TOTAL_ROWS" for i in resp["issues"])


def test_second_block_total_after_blank_gap_ok(output_dir, tmp_path):
    """空行之后其他表块自己的 Total 不影响本表 TOTAL 锚定（旧样例形态）。"""
    book = tmp_path / "second_block_ok.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(10, 5, "INVOICE NO.: SYN-AUDIT-0006")
    _write_invoice_sheet(ws, header_row=17, rows=GOOD_ROWS)
    pl = wb.create_sheet("Packing List")
    _write_packing_sheet(pl, total_row=9, second_total=(12, (2, 50.0)))
    wb.save(book)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "ready", resp["issues"]
    assert resp["records"][0]["row"]["gross_weight"] == "99.5"


# ---------------------------------------------------------------------------
# 审计(7)：结构指纹只含定位信息——同模板新一票（值/条数不同）同指纹
# ---------------------------------------------------------------------------

def test_fingerprint_stable_across_values_and_record_counts(output_dir, tmp_path):
    book_a = _parts_book(tmp_path / "fa.xlsx", header_row=17, rows=GOOD_ROWS)
    many = [("SYNP%d" % (500 + i), i + 1, float(i) + 0.5, float(2 * (i + 1)),
             "8413810000")
            for i in range(6)]
    book_b = _parts_book(tmp_path / "fb.xlsx", header_row=17, rows=many)
    resp_a = process_job(_parts_request(output_dir, book_a))
    resp_b = process_job(_parts_request(output_dir, book_b))
    assert resp_a["status"] == "ready" and resp_b["status"] == "ready"
    assert resp_a["layout"]["fingerprint"] == resp_b["layout"]["fingerprint"]
    # checks 证据（含真实值）仍随布局保留，供追溯
    assert any("net" in str(c.get("detail", "")).lower() or "净重" in str(c.get("detail", ""))
               for c in resp_a["layout"]["checks"])


def test_fingerprint_changes_only_on_real_structure_change(output_dir, tmp_path):
    moved = _parts_book(tmp_path / "moved.xlsx", header_row=21, rows=GOOD_ROWS)
    base = _parts_book(tmp_path / "base.xlsx", header_row=17, rows=GOOD_ROWS)
    resp_base = process_job(_parts_request(output_dir, base))
    resp_moved = process_job(_parts_request(output_dir, moved))
    assert resp_moved["layout"]["fingerprint"] != resp_base["layout"]["fingerprint"]
    # 结构投影本身不含任何明细值
    projection = json.dumps(resp_base["layout"]["structure"],
                            ensure_ascii=False, sort_keys=True)
    assert "SYNP100" not in projection


def test_machine_fingerprint_stable_across_mso_and_weight(output_dir, tmp_path):
    """同一 xls 模板结构、不同 MSO/净重 → 相同指纹（真实回归场景）。"""
    import xlrd
    import xlwt
    book = xlrd.open_workbook(str(FLEXIBLE_MACHINE_XLS))
    out = tmp_path / "renamed_mso.xls"
    wb = xlwt.Workbook()
    for name in book.sheet_names():
        src = book.sheet_by_name(name)
        dst = wb.add_sheet(name)
        for r in range(src.nrows):
            for c in range(src.ncols):
                v = src.cell_value(r, c)
                if isinstance(v, str):
                    v = v.replace("FAKE001", "FAKE777").replace("FAKE002", "FAKE778")
                    v = v.replace("SYN000001A", "SYN000099A").replace(
                        "SYN000002B", "SYN000098B")
                if name == "PL" and c == 15 and r == 29:  # 净重列数值
                    continue
                if v != "":
                    dst.write(r, c, v)
    wb.save(str(out))
    html_a = ("<html><body><table>"
              "<tr><th>Dealer Code</th><th>Dealer Order</th>"
              "<th>Cust. Purch. Order (MSO#)</th><th>Order</th><th>Material</th>"
              "<th>Serial Number</th><th>Total Sales Price</th></tr>"
              "<tr><td>SYN01</td><td>SYN-ORD-1</td><td>FAKE001</td>"
              "<td>06200000101</td><td>SEMTEST</td><td>SYN000001A</td>"
              "<td>50000.00</td></tr>"
              "<tr><td>SYN01</td><td>SYN-ORD-2</td><td>FAKE002</td>"
              "<td>06200000201</td><td>SEMTEST</td><td>SYN000002B</td>"
              "<td>50000.00</td></tr>"
              "</table></body></html>")
    html_b = (html_a.replace("FAKE001", "FAKE777").replace("FAKE002", "FAKE778")
              .replace("SYN000001A", "SYN000099A")
              .replace("SYN000002B", "SYN000098B"))

    def run(source, html):
        return process_job({
            "task_id": "TEST-AUDIT-FP",
            "kind": "machine",
            "destination": {"name": "巴西", "alpha3": "BRA"},
            "sources": [{"path": str(source), "filename": Path(source).name,
                         "mime_type": "application/vnd.ms-excel"}],
            "mail": {"html": html, "text": None,
                     "message_id": "<synthetic-fp@example.com>"},
            "references": {"machines": [{
                "model": "SEMTEST", "english_name": "SEMTEST WHEEL LOADER",
                "hs_code": "8429510000", "product_name": "装载机",
                "declaration": "1|0|合成申报要素|无|无|无|无|无|无",
                "destination_scope": "default"}], "countries": [],
                "machine_aliases": {}},
            "output_dir": output_dir,
            "overrides": [],
        })

    resp_a = run(FLEXIBLE_MACHINE_XLS, html_a)
    resp_b = run(out, html_b)
    assert resp_a["status"] == "ready", resp_a["issues"]
    assert resp_b["status"] == "ready", resp_b["issues"]
    assert resp_a["layout"]["fingerprint"] == resp_b["layout"]["fingerprint"]


# ---------------------------------------------------------------------------
# 审计(8)：机器解析线程化 header_aliases
# ---------------------------------------------------------------------------

def test_machine_request_header_aliases_applied(output_dir, tmp_path):
    book = tmp_path / "aliased_machine.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI"
    ws.cell(8, 1, "ITEM#"); ws.cell(8, 2, "Type"); ws.cell(8, 3, "MSO")
    ws.cell(9, 2, "SEMTEST"); ws.cell(9, 3, "FAKE001")
    pl = wb.create_sheet("PL")
    pl.cell(5, 1, "DESCRIPTION OF GOODS"); pl.cell(5, 4, "QTY")
    pl.cell(5, 6, "NET WEIGHT (KGS)")
    pl.cell(6, 1, "CMOPS MSO#: FAKE001"); pl.cell(6, 4, 1); pl.cell(6, 6, 12000)
    wb.save(book)
    html = ("<html><body><table>"
            "<tr><th>Dealer Code</th><th>Dealer Order</th>"
            "<th>Cust. Purch. Order (MSO#)</th><th>Order</th><th>Material</th>"
            "<th>Serial Number</th><th>Total Sales Price</th></tr>"
            "<tr><td>SYN01</td><td>SYN-ORD-1</td><td>FAKE001</td>"
            "<td>06200000101</td><td>SEMTEST</td>"
            "<td>SYN000001A</td><td>50000.00</td></tr>"
            "</table></body></html>")
    request = {
        "task_id": "TEST-AUDIT-ALIAS",
        "kind": "machine",
        "destination": {"name": "巴西", "alpha3": "BRA"},
        "sources": [{"path": str(book), "filename": book.name,
                     "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}],
        "mail": {"html": html, "text": None,
                 "message_id": "<synthetic-alias@example.com>"},
        "references": {"machines": [{
            "model": "SEMTEST", "english_name": "SEMTEST WHEEL LOADER",
            "hs_code": "8429510000", "product_name": "装载机",
            "declaration": "1|0|合成申报要素|无|无|无|无|无|无",
            "destination_scope": "default"}], "countries": [], "machine_aliases": {}},
        "output_dir": output_dir,
        "overrides": [],
        # 未配置别名时 "Type" 不是 MODEL 的别名 → 机器表定位失败暂停；
        # 配置后必须能解析（回归：_process_machine 曾丢弃请求别名）。
        "header_aliases": {"model": ["Type"]},
    }
    resp = process_job(request)
    assert resp["status"] == "ready", resp["issues"]
    assert resp["records"][0]["fields"]["material"] == "SEMTEST"

    del request["header_aliases"]
    resp_no_alias = process_job(request)
    assert resp_no_alias["status"] == "waiting_input"
