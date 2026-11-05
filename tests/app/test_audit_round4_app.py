"""追问正文可读性验收：输入结构类问题（新暂停码与来源/附件类）必须给出
工作表/单元格定位、实际缺失/多候选字段与可执行动作，禁止 Python 字面量
或“请联系处理人员”式笼统措辞；同时验证真实解析输出的问题字典经
question_body 渲染后同样可读（不仅断言解析器问题码）。

合成数据说明：全部工作簿为 FAKE/SYN 合成夹具。
"""
from __future__ import annotations

from pathlib import Path

import openpyxl

from cater.core.api import process_job

TASK = {"id": "task_guidance", "version": 1, "kind": "parts"}


# ---------------------------------------------------------------------------
# 合成构造：缺表头的最终发票表（真实触发 FINAL_SHEET_INVALID）/
# 料号行缺单价（真实触发 INVALID_DETAIL_ROW）
# ---------------------------------------------------------------------------

def _book(tmp_path: Path, *, with_coo: bool, with_price: bool) -> Path:
    book = tmp_path / "guidance.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CI-For Dealer (2)"
    ws.cell(10, 5, "INVOICE NO.: SYN-G-0001")
    cols = {"part_code": 2, "hs": 4, "coo": 5, "qty": 6, "unit": 7,
            "price": 8, "amount": 9}
    titles = {"part_code": "Part Code", "hs": "HS Code", "coo": "COO",
              "qty": "QTY", "unit": "UNIT", "price": "UNIT PRICE",
              "amount": "AMOUNT"}
    for key, c in cols.items():
        if key == "coo" and not with_coo:
            continue
        ws.cell(17, c, titles[key])
    ws.cell(18, cols["price"], "FCA SYN PORT IN USD")
    ws.cell(18, cols["amount"] + 1, "U'weight/kg")
    ws.cell(18, cols["amount"] + 2, "T'weight/kg")
    ws.cell(19, cols["part_code"], "SYNP100")
    ws.cell(19, cols["hs"], "8413810000")
    if with_coo:
        ws.cell(19, cols["coo"], "CN")
    ws.cell(19, cols["qty"], 2)
    ws.cell(19, cols["unit"], "PCS")
    if with_price:
        ws.cell(19, cols["price"], 3.5)
    ws.cell(19, cols["amount"], 2)
    ws.cell(19, cols["amount"] + 1, 2.0)
    ws.cell(19, cols["amount"] + 2, 4.0)
    ws.cell(19, 12, "8413810000")
    ws.cell(19, 13, "合成件")
    ws.cell(19, 14, "1|3|合成要素|无")
    pl = wb.create_sheet("Packing List")
    pl.cell(5, 2, "PACKAGE")
    pl.cell(5, 3, "GROSS WEIGHT/KG")
    pl.cell(9, 1, "TOTAL")
    pl.cell(9, 2, 2)
    pl.cell(9, 3, 99.5)
    wb.save(book)
    return book


def _run_core(tmp_path: Path, book: Path) -> dict:
    return process_job({
        "task_id": "TEST-GUIDANCE",
        "kind": "parts",
        "destination": {"name": "多哥", "alpha3": "TGO"},
        "sources": [{"path": str(book), "filename": book.name,
                     "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}],
        "mail": {},
        "references": {"machines": [], "countries": [], "machine_aliases": {}},
        "output_dir": str(tmp_path / "out"),
        "overrides": [],
    })


def _body(service, issues) -> str:
    return service.delivery.question_body(TASK, issues)


# ---------------------------------------------------------------------------
# 端到端：真实解析器产生的阻塞问题 → question_body 可读
# ---------------------------------------------------------------------------

def test_missing_coo_header_question_body_readable(service, tmp_path) -> None:
    resp = _run_core(tmp_path, _book(tmp_path, with_coo=False, with_price=True))
    assert resp["status"] == "waiting_input"
    codes = {i["code"] for i in resp["issues"]}
    assert "FINAL_SHEET_INVALID" in codes
    body = _body(service, resp["issues"])
    assert "FINAL_SHEET_INVALID" not in body
    assert "原产国" in body                      # 缺失字段可读
    assert "['coo']" not in body and "'coo'" not in body   # 无 Python 字面量
    assert "请补齐" in body                       # 可执行动作
    assert "请联系处理人员" not in body


def test_invalid_detail_row_question_body_readable(service, tmp_path) -> None:
    resp = _run_core(tmp_path, _book(tmp_path, with_coo=True, with_price=False))
    assert resp["status"] == "waiting_input"
    detail = [i for i in resp["issues"] if i["code"] == "INVALID_DETAIL_ROW"]
    assert detail, resp["issues"]
    body = _body(service, resp["issues"])
    assert "INVALID_DETAIL_ROW" not in body
    assert "SYNP100" in body
    assert "单价" in body
    assert "unit_price" not in body and "None" not in body
    assert "H19" in body                          # 工作表/单元格定位
    assert "补齐" in body and "重发" in body


# ---------------------------------------------------------------------------
# 逐码验收：问题字典（解析器 Issue.to_dict 形状）→ 可读行
# ---------------------------------------------------------------------------

def _issue(code: str, message: str = "", **kw) -> dict:
    issue = {"code": code, "record_key": None, "field": None,
             "message": message, "sources": [], "required_action": None}
    issue.update(kw)
    return issue


def test_ambiguous_final_sheet_guidance(service) -> None:
    body = _body(service, [_issue(
        "AMBIGUOUS_FINAL_SHEET",
        "存在多个合格的最终发票表：['CI-For Dealer (2)', 'CI-For Dealer (3)']，"
        "无法唯一选定",
        sources=[{"type": "workbook", "filename": "a.xlsx"}],
        required_action="请确认应使用哪张最终调整表")])
    assert "多张合格的最终发票表" in body
    assert "请确认应使用哪张最终调整表" in body  # 明确最终业务表
    assert "CI-For Dealer (2)" in body and "CI-For Dealer (3)" in body
    assert "AMBIGUOUS_FINAL_SHEET" not in body and "[" not in body


def test_packing_totals_invalid_guidance(service) -> None:
    body = _body(service, [_issue(
        "PACKING_TOTALS_INVALID",
        "箱单 Packing List 的 TOTAL 行件数/毛重列无法唯一定位：缺失=[] "
        "多候选=[('qty', [6, 10])] 多表区=[5, 9]",
        sources=[{"type": "workbook", "filename": "a.xlsx",
                  "sheet": "Packing List"}],
        required_action="请确认箱单表头包含唯一一份 PACKAGE 与 GROSS WEIGHT 列")])
    assert "件数/毛重" in body
    assert "Packing List" in body
    assert "数量（第6、10列均命中" in body        # 多候选字段可读
    assert "PACKAGE" in body and "GROSS WEIGHT" in body
    assert "PACKING_TOTALS_INVALID" not in body
    assert "[(" not in body and "[]" not in body


def test_multiple_total_rows_guidance(service) -> None:
    body = _body(service, [_issue(
        "MULTIPLE_TOTAL_ROWS",
        "最终发票表 CI-For Dealer (2) 表头下方存在多张同属一个表块的合计行，"
        "无法确定明细范围与合计锚点",
        sources=[{"type": "workbook", "filename": "a.xlsx",
                  "sheet": "CI-For Dealer (2)"}],
        required_action="请确认本票唯一 TOTAL 行后重发")])
    assert "多张合计（TOTAL）行" in body
    assert "请确认本票唯一 TOTAL 行后重发" in body
    assert "CI-For Dealer (2)" in body
    assert "MULTIPLE_TOTAL_ROWS" not in body
    assert "联系处理人员" not in body


def test_machine_table_invalid_guidance_not_reference_maintenance(service) -> None:
    body = _body(service, [_issue(
        "MACHINE_TABLE_INVALID",
        "业务表 CI 的机器信息表数据区出现重复表头行，紧邻多张表块，"
        "无法唯一确定明细范围，不能猜测放行",
        sources=[{"type": "workbook", "filename": "m.xls", "sheet": "CI"}],
        required_action="请确认机器信息表只保留一张表块，或空行分隔各表块")])
    assert "机器信息表" in body and "CI 表" in body
    assert "空行分隔" in body
    # 输入结构问题不得落入“机型资料库维护”措辞
    assert "资料维护人员" not in body
    assert "MACHINE_TABLE_INVALID" not in body


def test_machine_table_ambiguous_token_translated(service) -> None:
    body = _body(service, [_issue(
        "MACHINE_TABLE_INVALID",
        "业务表 CI 的机器信息表结构校验未通过（ambiguous），不能猜测放行",
        sources=[{"type": "workbook", "filename": "m.xls", "sheet": "CI"}],
        required_action="请确认机器信息表（MODEL/MSO# 等表头）完整且唯一")])
    assert "ambiguous" not in body
    assert "存在多候选" in body
    assert "MODEL/MSO#" in body


def test_ambiguous_weight_table_guidance(service) -> None:
    body = _body(service, [_issue(
        "AMBIGUOUS_WEIGHT_TABLE",
        "多个工作表存在箱单重量表：['PL', 'Sheet2']，无法唯一选定",
        sources=[{"type": "workbook", "filename": "m.xls"}],
        required_action="请确认应使用哪张工作表")])
    assert "多张工作表都存在箱单重量表" in body
    assert "PL" in body and "Sheet2" in body
    assert "AMBIGUOUS_WEIGHT_TABLE" not in body
    assert "资料维护人员" not in body


def test_supplementary_columns_invalid_guidance(service) -> None:
    body = _body(service, [_issue(
        "SUPPLEMENTARY_COLUMNS_INVALID",
        "最终发票表 CI-For Dealer (2) 的无标题补充列（L 起三列）结构校验"
        "未通过：有效HS=0 名称=2 要素=2",
        sources=[{"type": "workbook", "filename": "a.xlsx",
                  "sheet": "CI-For Dealer (2)", "cell": "L17"}],
        required_action="请确认最终调整表 K 列之后依次为 商品编码/中文名/申报要素 后重发")])
    assert "商品编码/中文名/申报要素" in body
    assert "L17" in body
    assert "SUPPLEMENTARY_COLUMNS_INVALID" not in body


def test_missing_source_guidance(service) -> None:
    body = _body(service, [_issue(
        "MISSING_SOURCE", message="缺少配件发票附件",
        required_action="请补充配件发票 xlsx 或 xls 附件")])
    assert "Excel 附件" in body
    assert "请补充配件发票" in body
    assert "缺少业务信息，请补充" not in body  # 不得落入 MISSING_FIELD 笼统措辞


def test_unsupported_file_guidance(service) -> None:
    body = _body(service, [_issue(
        "UNSUPPORTED_FILE", message="附件中未找到配件发票工作簿（CI-For Dealer）",
        required_action="请补充含 CI-For Dealer (2) 的 xlsx 或 xls 附件")])
    assert "附件无法使用" in body
    assert "xlsx" in body and "xls" in body
    assert "CI-For Dealer" in body
    assert "UNSUPPORTED_FILE" not in body


def test_final_sheet_not_found_guidance(service) -> None:
    body = _body(service, [_issue(
        "FINAL_SHEET_NOT_FOUND",
        "未找到含补充资料的最终发票表（如 CI-For Dealer (2)），原表不能自动替代",
        sources=[{"type": "workbook", "filename": "a.xlsx", "sheet": "original"}],
        required_action="请提供含补充资料（K 列之后）的最终调整表")])
    assert "未找到含补充资料列的最终调整表" in body
    assert "原表不能自动替代" in body
    assert "FINAL_SHEET_NOT_FOUND" not in body


# ---------------------------------------------------------------------------
# 既有可读命令不受影响
# ---------------------------------------------------------------------------

def test_existing_destination_and_correction_commands_preserved(service) -> None:
    body = _body(service, [
        {"code": "DESTINATION_MISSING", "field": "destination",
         "message": "No destination", "required_action": None},
        {"code": "FIELD_CONFLICT", "record_key": "BFWVU",
         "field": "total_sales_price",
         "message": "字段 total_sales_price 两来源不一致：68822.84 ≠ 69847.84"},
    ])
    assert "目的国：法国" in body
    assert "修正：BFWVU.total_sales_price=" in body
    assert "68822.84 ≠ 69847.84" in body
