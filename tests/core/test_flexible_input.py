"""多结构输入解析验收（plan/tasks/claude-flexible-input.md）。

覆盖：真实新配件样本、真实新整机 xls（配合显式合成邮件）、合成 xls 多结构
样例（深表头/双行表头/历史表排除）、表与列移动、双行表头、多候选拒绝、
错列数字巧合、zip 炸弹防护、xls 签名保护、布局上报与指纹。
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import openpyxl
import pytest

from cater.core.api import process_job
from cater.core.parsing.headers import merged_aliases, normalize_header
from cater.core.parsing.workbook import (UnsupportedSourceError,
                                         load_workbook_pair)
from cater.core.parsing.machine_wb import parse_machine_workbook
from cater.core.parsing.parts import parse_parts_workbook

from conftest import INPUT_PARTS_NEW, REPO_ROOT

FIXTURES = Path(__file__).parent / "fixtures"
FLEXIBLE_MACHINE_XLS = FIXTURES / "flexible_machine.xls"

# ---------------------------------------------------------------------------
# 合成业务邮件（明确为合成夹具，不代表真实新邮件已通过）
# ---------------------------------------------------------------------------

def _synthetic_mail_html(msos, model="SEMTEST", serials=None, price="50000.00"):
    serials = serials or [f"SYN{i:04d}X" for i in range(len(msos))]
    header = ("<tr><th>Dealer Code</th><th>Dealer Order</th>"
              "<th>Cust. Purch. Order (MSO#)</th><th>Order</th><th>Material</th>"
              "<th>Serial Number</th><th>Total Sales Price</th></tr>")
    rows = []
    for i, mso in enumerate(msos):
        rows.append(
            f"<tr><td>SYN01</td><td>SYNORD-{i}</td><td>{mso}</td>"
            f"<td>0620000{i}01</td><td>{model}</td><td>{serials[i]}</td>"
            f"<td>{price}</td></tr>")
    return ("<html><body><table>" + header + "".join(rows) +
            "</table></body></html>")


def _machine_references(model="SEMTEST"):
    return [{
        "model": model,
        "english_name": f"{model} WHEEL LOADER",
        "hs_code": "8429510000",
        "product_name": "装载机",
        "declaration": "1|0|合成申报要素|无|无|无|无|无|无",
        "destination_scope": "default",
    }]


def _machine_request(output_dir, source_path, html, *, kind="machine"):
    return {
        "task_id": "TEST-FLEX-MACHINE",
        "kind": kind,
        "destination": {"name": "巴西", "alpha3": "BRA"},
        "sources": [{"path": str(source_path), "filename": Path(source_path).name,
                     "mime_type": "application/vnd.ms-excel"}],
        "mail": {"html": html, "text": None, "message_id": "<synthetic-flex@example.com>"},
        "references": {"machines": _machine_references(), "countries": [],
                       "machine_aliases": {}},
        "output_dir": output_dir,
        "overrides": [],
    }


# ---------------------------------------------------------------------------
# 真实新配件样本（只读提取）
# ---------------------------------------------------------------------------

def test_new_parts_sample_ready(parts_request_new):
    resp = process_job(parts_request_new)
    assert resp["status"] == "ready", resp["issues"]
    assert len(resp["records"]) == 24
    identity = resp["business_identity"]
    assert identity == "CQLTRA261104HCB-BGVTG"


def test_new_parts_sample_values(parts_request_new):
    resp = process_job(parts_request_new)
    rows = {r["record_key"]: r["row"] for r in resp["records"]}
    first = rows["5184406"]
    assert first["qty"] == "8"
    assert first["unit_price"] == "25.63"     # 最终单价 H 列（费用已分摊）
    assert first["net_weight"] == "21.76"
    assert first["hs_code"] == "8421999090"
    assert first["product_name"] == "空气滤芯"
    assert first["gross_weight"] == "215"     # 箱单 K79
    assert first["packages"] == "10"          # 箱单 H79
    assert first["invoice_no"] == "CQLTRA261104HCB-BGVTG"
    assert rows["6123959"]["qty"] == "24"
    assert rows["6123959"]["net_weight"] == "35.04"
    # 输出不含原表中间计算列：行字段固定且可追溯
    assert set(first) >= {"part_code", "qty", "unit_price", "net_weight", "coo",
                          "destination", "hs_code", "product_name", "spec",
                          "currency", "order_no"}


def test_new_parts_spec_cleaning(parts_request_new):
    """开头 1||3| 与 1||不适合进口报关 各删一个空项；后续竖线空项保持。"""
    resp = process_job(parts_request_new)
    rows = {r["record_key"]: r["row"] for r in resp["records"]}
    assert rows["5184406"]["spec"].startswith("1|3|用于SEM品牌")
    assert rows["5186593"]["spec"].startswith("1|不适合进口报关|碳素钢")
    # 已经是单竖线的不动
    assert rows["5536268"]["spec"].startswith("1|3|动力传动")
    # 中部 || 空项保持（不通用 collapse）
    assert "山工品牌、SEM品牌||无型号" in rows["5217301"]["spec"]


def test_new_parts_layout_and_provenance(parts_request_new):
    resp = process_job(parts_request_new)
    layout = resp["layout"]
    assert layout["kind"] == "parts"
    assert layout["fingerprint"]
    assert layout["parts_invoice"]["sheet"] == "CI-For Dealer (2)"
    assert layout["parts_invoice"]["header_row"] == 17
    assert layout["parts_invoice"]["subheader_row"] == 18
    assert layout["parts_invoice"]["columns"]["qty"] == "F"
    assert layout["parts_invoice"]["columns"]["unit_price"] == "H"
    assert layout["packing_total"]["columns"]["packages"] == "H"
    assert layout["packing_total"]["columns"]["gross_weight"] == "K"
    assert layout["packing_total"]["total_row"] == 79
    rules = {c["rule"] for c in layout["checks"]}
    assert "PARTS_WEIGHT_QTY_X_UNIT" in rules
    assert "PARTS_DETAIL_NET_TOTAL" in rules
    assert all(c["result"] == "pass" for c in layout["checks"])
    # 指纹可复现
    again = process_job(parts_request_new)
    assert again["layout"]["fingerprint"] == layout["fingerprint"]


# ---------------------------------------------------------------------------
# 真实新整机 xls（只读提取 + 显式合成邮件隔离端到端）
# ---------------------------------------------------------------------------

def test_new_machine_xls_real_sample(output_dir):
    source = REPO_ROOT / "input" / "整机" / "CI PL ---2X656D BIHWE-WF.xls"
    if not source.is_file():
        pytest.skip("real machine xls sample not available locally")
    # 合成邮件（用户确认实际邮件提供完整业务表；本测试邮件为合成夹具）
    html = ("<html><body><table>"
            "<tr><th>Dealer Code</th><th>Dealer Order</th>"
            "<th>Cust. Purch. Order (MSO#)</th><th>Order</th><th>Material</th>"
            "<th>Serial Number</th><th>Total Sales Price</th></tr>"
            "<tr><td>SOT01</td><td>SYN-ORD-1</td><td>BIHWE</td><td>0621234501</td>"
            "<td>SEM656D</td><td>S5307023</td><td>52000.00</td></tr>"
            "<tr><td>SOT01</td><td>SYN-ORD-2</td><td>BIHWF</td><td>0621234502</td>"
            "<td>SEM656D</td><td>S5307024</td><td>52000.00</td></tr>"
            "</table></body></html>")
    request = _machine_request(output_dir, source, html)
    request["references"] = {
        "machines": [{
            "model": "SEM656D",
            "english_name": "SEM656D WHEEL LOADER",
            "hs_code": "8429510000",
            "product_name": "轮式装载机",
            "declaration": "1|0|合成申报要素|无|无|无|无|无|无",
            "destination_scope": "default",
        }],
        "countries": [], "machine_aliases": {},
    }
    resp = process_job(request)
    assert resp["status"] == "ready", resp["issues"]
    assert len(resp["records"]) == 2
    rec = {r["record_key"]: r["fields"] for r in resp["records"]}
    assert rec["BIHWE"]["serial_number"] == "S5307023"
    assert rec["BIHWF"]["serial_number"] == "S5307024"
    assert rec["BIHWE"]["material"] == "SEM656D"
    assert rec["BIHWE"]["net_weight"] == "17300"      # 34600÷2
    assert rec["BIHWE"]["total_sales_price"] == "52000.00"
    # 发票单价 47141.75 绝不可冒充 Total Sales Price
    for fields in rec.values():
        assert fields["total_sales_price"] != "47141.75"
    # 历史 sheet（BL/Sheet2/Sheet1/MACHINE DISASSEMBLY DETAILS）不混入
    assert set(rec) == {"BIHWE", "BIHWF"}
    assert resp["business_identity"] == "BIHWE|BIHWF|CQL-SOM261103M"
    # 布局：CI 深表头 56、PL 双行表头
    layout = resp["layout"]
    assert layout["machine_table"][0]["sheet"] == "CI"
    assert layout["machine_table"][0]["header_row"] == 56
    assert layout["weight_table"][0]["sheet"] == "PL"
    assert layout["weight_table"][0]["subheader_row"] == 29
    assert all(c["result"] == "pass" for c in layout["checks"])
    # 输出两份整机文件
    assert len(resp["artifacts"]) == 2


# ---------------------------------------------------------------------------
# 合成 xls 多结构样例（结构变化与历史表排除）
# ---------------------------------------------------------------------------

def test_flexible_machine_xls_synthetic(output_dir):
    html = _synthetic_mail_html(["FAKE001", "FAKE002"],
                                serials=["SYN000001A", "SYN000002B"])
    resp = process_job(_machine_request(output_dir, FLEXIBLE_MACHINE_XLS, html))
    assert resp["status"] == "ready", resp["issues"]
    rec = {r["record_key"]: r["fields"] for r in resp["records"]}
    # 历史 Sheet2 的 FAKEH1 不得混入
    assert set(rec) == {"FAKE001", "FAKE002"}
    assert rec["FAKE001"]["net_weight"] == "12000"    # 24000÷2
    assert rec["FAKE001"]["serial_number"] == "SYN000001A"
    assert rec["FAKE001"]["total_sales_price"] == "50000.00"
    layout = resp["layout"]
    assert layout["machine_table"][0]["header_row"] == 56
    assert layout["sheets"] == ["CI", "PL"]


def test_flexible_machine_layout_fingerprint_changes_with_structure(output_dir, tmp_path):
    html = _synthetic_mail_html(["FAKE001", "FAKE002"],
                                serials=["SYN000001A", "SYN000002B"])
    first = process_job(_machine_request(output_dir, FLEXIBLE_MACHINE_XLS, html))
    # 复制并把 CI 表头下移两行 → 结构指纹必须变化
    import xlrd
    book = xlrd.open_workbook(str(FLEXIBLE_MACHINE_XLS))
    out = tmp_path / "moved.xls"
    import xlwt
    wb = xlwt.Workbook()
    for name in book.sheet_names():
        src = book.sheet_by_name(name)
        dst = wb.add_sheet(name)
        shift = 2 if name == "CI" else 0
        for r in range(src.nrows):
            for c in range(src.ncols):
                v = src.cell_value(r, c)
                if v != "":
                    dst.write(r + shift, c, v)
    wb.save(str(out))
    second = process_job(_machine_request(str(tmp_path / "out2"), out, html))
    assert second["status"] == "ready", second["issues"]
    assert (second["layout"]["fingerprint"]
            != first["layout"]["fingerprint"])
    assert second["layout"]["machine_table"][0]["header_row"] == 58


# ---------------------------------------------------------------------------
# 统一工作簿接口安全与格式边界
# ---------------------------------------------------------------------------

def test_workbook_reader_rejects_bad_xls_signature(tmp_path):
    fake = tmp_path / "fake.xls"
    fake.write_bytes(b"PK\x03\x04 not an ole file at all")
    with pytest.raises(UnsupportedSourceError):
        load_workbook_pair(str(fake))


def test_workbook_reader_rejects_zip_bomb_members(tmp_path):
    """成员数超限的 xlsx 必须拒绝（不解压）。"""
    bomb = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(2100):
            zf.writestr(f"xl/part{i}.xml", "<x/>")
    with pytest.raises(UnsupportedSourceError):
        load_workbook_pair(str(bomb))


def test_workbook_reader_rejects_non_workbook(tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    with pytest.raises(UnsupportedSourceError):
        load_workbook_pair(str(note))


def test_xls_formula_semantics():
    """xls 无公式信息：cell 级读取直接返回值（不触发缓存缺失误报）。"""
    wb, wf = load_workbook_pair(str(FLEXIBLE_MACHINE_XLS))
    assert wb is wf  # xls 的 formula 视图即 values 视图
    ws = wb["CI"]
    assert ws.cell(57, 2).value == "SEMTEST"
    assert ws.cell(57, 2).coordinate == "B57"
    assert wb.sheetnames == ["CI", "PL", "Sheet2", "BL "]


# ---------------------------------------------------------------------------
# 语义列定位单元行为：规范化、多候选、可配置别名
# ---------------------------------------------------------------------------

def test_header_normalization_variants():
    assert normalize_header("U'weight/kg") == normalize_header("u' weight / KG")
    assert normalize_header("NET WEIGHT (KGS)") == "netweightkgs"
    assert normalize_header("SERIAL #") == "serial"
    assert normalize_header("QTY.") == "qty"
    assert normalize_header("Cust. Purch. Order (MSO#)") == "custpurchordermso"
    assert normalize_header(None) == ""


def test_aliases_configurable():
    aliases = merged_aliases({"unit_price": ["Final Price", "结算价"]})
    assert "finalprice" in aliases["unit_price"]
    assert "unitprice" in aliases["unit_price"]  # 默认保留
    assert "结算价".casefold() in aliases["unit_price"]


# ---------------------------------------------------------------------------
# 结构变化合成样例：移动列/行、双行表头、多候选拒绝、错列数字巧合
# ---------------------------------------------------------------------------

def _build_parts_workbook(path: Path, *, header_row=17, col_shift=0,
                          double_header=True, dup_price=False,
                          rows=3, decoy_coincidence=False):
    """构造合成配件最终发票表（数据全合成）。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(10, 5, "INVOICE NO.:  \nSYN-TEST-0002")
    col = {"part_code": 2, "hs": 4, "coo": 5, "qty": 6, "unit": 7,
           "price": 8, "amount": 9}
    col = {k: v + col_shift for k, v in col.items()}
    for key, c in col.items():
        titles = {"part_code": "Part Code", "hs": "HS Code", "coo": "COO",
                  "qty": "QTY", "unit": "UNIT", "price": "UNIT PRICE",
                  "amount": "AMOUNT"}
        ws.cell(header_row, c, titles[key])
    sub_row = header_row + 1 if double_header else None
    # 与真实样例一致：重量列在 AMOUNT 之后，绝不与单价/金额列重叠
    unit_w_col = col["amount"] + 1
    net_w_col = col["amount"] + 2
    if sub_row:
        ws.cell(sub_row, col["price"], "FCA SYN PORT IN USD")
        ws.cell(sub_row, unit_w_col, "U'weight/kg")
        ws.cell(sub_row, net_w_col, "T'weight/kg")
    if dup_price:
        ws.cell(header_row, net_w_col + 1, "Unit Price")  # 第二个单价列
    if decoy_coincidence:
        ws.cell(header_row, net_w_col + 1, "REMARK")  # 无关列（带表头，数值巧合）
    start = (sub_row or header_row) + 1
    unit_w, price = 2.0, 3.5
    for i in range(rows):
        r = start + i
        ws.cell(r, col["part_code"], f"SYNPART{i:03d}")
        ws.cell(r, col["hs"], "8413810000")
        ws.cell(r, col["coo"], "CN")
        ws.cell(r, col["qty"], 1)
        ws.cell(r, col["unit"], "PCS")
        ws.cell(r, col["price"], price)
        ws.cell(r, col["amount"], price * 1)  # qty=1 → 金额与单价巧合相等
        ws.cell(r, unit_w_col, unit_w)
        ws.cell(r, net_w_col, unit_w * 1)
        if decoy_coincidence:
            ws.cell(r, net_w_col + 1, unit_w)  # 数值巧合的无关列
    supp = net_w_col + 1 if not decoy_coincidence else net_w_col + 2
    for i in range(rows):
        r = start + i
        ws.cell(r, supp, "8413810000")
        ws.cell(r, supp + 1, "合成泵")
        ws.cell(r, supp + 2, "1||3|合成要素|无")
    ws.cell(start + rows, 1, "TOTAL (IN WORDS): SAY USD ...")
    ws.cell(start + rows, net_w_col, unit_w * rows)
    pl = wb.create_sheet("Packing List")
    pl.cell(17, 8, "PACKAGE")
    pl.cell(17, 11, "GROSS WEIGHT/KG")
    pl.cell(20, 1, "TOTAL")
    pl.cell(20, 8, 2)
    pl.cell(20, 11, 99.5)
    wb.save(path)


def _parts_request(output_dir, path, kind="parts"):
    return {
        "task_id": "TEST-FLEX-PARTS",
        "kind": kind,
        "destination": {"name": "多哥", "alpha3": "TGO"},
        "sources": [{"path": str(path), "filename": Path(path).name,
                     "mime_type": "application/xlsx"}],
        "mail": {},
        "references": {"machines": [], "countries": [], "machine_aliases": {}},
        "output_dir": output_dir,
        "overrides": [],
    }


def test_parts_moved_columns_and_rows(output_dir, tmp_path):
    """表头与数据整体移动（插入列/行）后仍语义定位。"""
    book = tmp_path / "moved.xlsx"
    _build_parts_workbook(book, header_row=21, col_shift=3)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "ready", resp["issues"]
    assert len(resp["records"]) == 3
    row = resp["records"][0]["row"]
    assert row["unit_price"] == "3.5"
    assert row["net_weight"] == "2"
    layout = resp["layout"]
    assert layout["parts_invoice"]["header_row"] == 21
    assert layout["parts_invoice"]["columns"]["qty"] != "F"  # 已移动
    prov = next(p for p in resp["provenance"]
                if p["field"] == "unit_price")
    assert prov["source"]["cell"].startswith(
        layout["parts_invoice"]["columns"]["unit_price"])


def test_parts_double_row_header_and_coincidence(output_dir, tmp_path):
    """双行表头解析正确；qty=1 时 AMOUNT 与单价巧合相等也不得错列。"""
    book = tmp_path / "double.xlsx"
    _build_parts_workbook(book, decoy_coincidence=True)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "ready", resp["issues"]
    rec = resp["records"][0]
    assert rec["row"]["unit_price"] == "3.5"
    assert rec["row"]["net_weight"] == "2"
    # 字段来源定位在语义列而非数值巧合列
    price_src = next(p for p in resp["provenance"] if p["field"] == "unit_price")
    assert price_src["source"]["cell"].startswith("H")
    net_src = next(p for p in resp["provenance"] if p["field"] == "net_weight")
    assert net_src["source"]["cell"].startswith("K")  # T'weight/kg = 总净重列


def test_parts_multiple_price_candidates_pause(output_dir, tmp_path):
    """同一表头行出现两个单价语义列 → 多候选拒绝，不猜测。"""
    book = tmp_path / "dup.xlsx"
    _build_parts_workbook(book, dup_price=True)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "waiting_input"
    assert resp["artifacts"] == []
    assert any(i["code"] == "FINAL_SHEET_INVALID" for i in resp["issues"])


def test_parts_alias_override_selects_configured_header(output_dir, tmp_path):
    """表头别名可配置：用自定义别名定位单价列。"""
    book = tmp_path / "aliased.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(8, 3, "INVOICE NO.: SYN-TEST-0003")
    ws.cell(10, 1, "PO")
    ws.cell(10, 2, "Part Code")
    ws.cell(10, 3, "HS Code")
    ws.cell(10, 4, "COO")
    ws.cell(10, 5, "QTY")
    ws.cell(10, 6, "Final Price")   # 主表头行自定义别名
    ws.cell(11, 7, "U'weight/kg")
    ws.cell(11, 8, "T'weight/kg")
    ws.cell(12, 2, "SYNP900")
    ws.cell(12, 3, "8413810000")
    ws.cell(12, 4, "CN")
    ws.cell(12, 5, 2)
    ws.cell(12, 6, 9.99)
    ws.cell(12, 7, 0.5)
    ws.cell(12, 8, 1.0)
    ws.cell(12, 9, "8413810000")
    ws.cell(12, 10, "合成阀")
    ws.cell(12, 11, "1|3|合成要素|无")
    pl = wb.create_sheet("Packing List")
    pl.cell(5, 2, "PACKAGE")
    pl.cell(5, 3, "GROSS WEIGHT/KG")
    pl.cell(8, 1, "TOTAL")
    pl.cell(8, 2, 1)
    pl.cell(8, 3, 9.9)
    wb.save(book)
    request = _parts_request(output_dir, book)
    request["header_aliases"] = {"unit_price": ["Final Price"]}
    resp = process_job(request)
    assert resp["status"] == "ready", resp["issues"]
    assert resp["records"][0]["row"]["unit_price"] == "9.99"


def test_parts_configurable_alias_not_guessed_without_config(output_dir, tmp_path):
    """未配置的未知表头不放行（禁止仅凭启发式猜测）。"""
    book = tmp_path / "unknown.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(8, 3, "INVOICE NO.: SYN-TEST-0004")
    ws.cell(10, 2, "Part Code")
    ws.cell(10, 5, "QTY")
    ws.cell(10, 6, "Settle Amount")   # 未配置别名，不得猜测为单价
    ws.cell(11, 7, "U'weight/kg")
    ws.cell(11, 8, "T'weight/kg")
    ws.cell(12, 2, "SYNP901")
    ws.cell(12, 5, 1)
    ws.cell(12, 6, 7.77)
    ws.cell(12, 7, 0.3)
    ws.cell(12, 8, 0.3)
    ws.cell(12, 9, "8413810000")
    ws.cell(12, 10, "合成件")
    ws.cell(12, 11, "1|3|合成|无")
    pl = wb.create_sheet("Packing List")
    pl.cell(5, 2, "PACKAGE")
    pl.cell(5, 3, "GROSS WEIGHT/KG")
    pl.cell(8, 1, "TOTAL")
    pl.cell(8, 2, 1)
    pl.cell(8, 3, 5.5)
    wb.save(book)
    resp = process_job(_parts_request(output_dir, book))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "FINAL_SHEET_INVALID" for i in resp["issues"])


def test_machine_ambiguous_content_tables_pause(output_dir, tmp_path):
    """无业务表名时多个结构候选必须暂停（历史表排除的另一面：不猜测）。"""
    import xlrd
    import xlwt
    book = xlrd.open_workbook(str(FLEXIBLE_MACHINE_XLS))
    out = tmp_path / "ambiguous.xls"
    wb = xlwt.Workbook()
    for name in ("Data A", "Data B"):  # 两个非业务名表都含机器信息表
        src = book.sheet_by_name("CI")
        dst = wb.add_sheet(name)
        for r in range(src.nrows):
            for c in range(src.ncols):
                v = src.cell_value(r, c)
                if v != "":
                    dst.write(r, c, v)
    wb.save(str(out))
    result = parse_machine_workbook(str(out), "ambiguous.xls")
    assert any(i.code == "AMBIGUOUS_MACHINE_TABLE" for i in result.issues)


def test_machine_historical_sheets_excluded(output_dir):
    """Sheet2/BL 的历史机器表即使结构合法也不参与（名称辅助优先）。"""
    result = parse_machine_workbook(str(FLEXIBLE_MACHINE_XLS), "flexible_machine.xls")
    msos = {m.mso for m in result.invoice_rows}
    assert "FAKEH1" not in msos
    assert not any(i.code in ("AMBIGUOUS_MACHINE_TABLE", "MACHINE_TABLE_INVALID")
                   for i in result.issues)
