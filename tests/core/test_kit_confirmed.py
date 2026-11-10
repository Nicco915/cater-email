"""已确认配件 Health Care Kit 解析测试（第三阶段 C）。

覆盖：
- 合成清洁数据：两发票页一致、费用行排除、上下两段 TOTAL、皮重已含、
  重复料号、精度、完整转换到既有 transform；
- 冲突/阻断：两发票页差价口径、辅助金额列、BL 旧票号、错票来源、
  缺 HS/中文品名/申报要素、行价×量/净重不符、公式缓存缺失、
  未确认区域/区域坐标漂移、未授权资产；
- 真实四票（本机存在 input/ 只读源时）：保留现有冲突，不从预览事实
  填明细。

真实数据目录被 .gitignore 忽略；缺失时相关用例 skip，不复制进 fixture。
"""
from __future__ import annotations

import copy
import hashlib
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
from openpyxl import Workbook

from cater.core.references import CountryIndex
from cater.core.transform import build_parts_rows

INVOICE_SHEET = "Invoice "
INVOICE2_SHEET = "Invoice  (2)"
PACKING_SHEET = "Packing list"
BL_SHEET = "BL"

DEFAULT_PARTS = [
    {
        "kit_no": 9900001,
        "part_code": "SYN-P100",
        "hs": "84219900",
        "coo": "US",
        "qty_per_set": 4,
        "sets": 3,
        "unit_price": "28.11",
        "unit_weight": "0.9",
        "name": "合成滤芯",
        "spec": "1|合成申报要素A",
    },
    {
        "kit_no": 9900001,
        "part_code": "SYN-B200",
        "hs": "73181510",
        "coo": "CN",
        "qty_per_set": 40,
        "sets": 3,
        "unit_price": "1.25",
        "unit_weight": "0.21",
        "name": "合成螺栓",
        "spec": "1|合成申报要素B",
    },
]


# ---------------------------------------------------------------------------
# 合成工作簿构造
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_confirmed_module():
    """不把已确认模块留在 sys.modules，避免污染全局导入断言测试。"""
    yield
    import sys
    sys.modules.pop("cater.core.parsing.kit_confirmed", None)


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _write_invoice_sheet(ws, parts: list[dict], invoice_no: str, *,
                         fee_amount="10", aux_total=None,
                         row_overrides: dict | None = None,
                         blank_fields: dict | None = None) -> tuple:
    """按真实 Health Care Kit 发票页结构写一张合成表。

    返回 (line_amount, line_net)，供箱单/断言使用。
    """
    ws["G6"] = f"INVOICE NO.: {invoice_no}\n\nPRICE EFF DATE: NOV 03, 2026"
    headers = ["Health Care Kit No", "CHN HS Code", "COO",
               "DESCRIPTION OF GOODS", None, "Qty/SET", "SET QTY",
               "TOTAL Qty", "U'Price", "T'Price"]
    for col, value in enumerate(headers, 1):
        ws.cell(10, col).value = value
    ws["I11"] = "IN USD"
    ws["K11"] = "单件净重"
    ws["L11"] = "总净重"
    ws["Q11"] = "HS"
    ws["R11"] = "中文品名"
    ws["S11"] = "申报要素"

    row = 12
    line_amount = Decimal("0")
    line_net = Decimal("0")
    overrides = row_overrides or {}
    blanks = blank_fields or {}
    for index, part in enumerate(parts):
        qty = _dec(part["qty_per_set"]) * _dec(part["sets"])
        price = _dec(part["unit_price"])
        total = price * qty
        net = _dec(part["unit_weight"]) * qty
        override = overrides.get(index, {})
        if "total_price" in override:
            total = _dec(override["total_price"])
        if "net_weight" in override:
            net = _dec(override["net_weight"])
        blank = blanks.get(index, ())
        name = None if "name" in blank else part["name"]
        hs = None if "hs" in blank else part["hs"]
        coo = None if "coo" in blank else part["coo"]
        ws.cell(row, 1).value = part["kit_no"]
        ws.cell(row, 2).value = hs
        ws.cell(row, 3).value = coo
        ws.cell(row, 4).value = part["part_code"]
        ws.cell(row, 5).value = "SYNTHETIC DESCRIPTION"
        ws.cell(row, 6).value = part["qty_per_set"]
        ws.cell(row, 7).value = part["sets"]
        ws.cell(row, 8).value = int(qty)
        ws.cell(row, 9).value = float(price)
        ws.cell(row, 10).value = float(total)
        ws.cell(row, 11).value = float(_dec(part["unit_weight"]))
        ws.cell(row, 12).value = float(net)
        ws.cell(row, 17).value = hs
        ws.cell(row, 18).value = name
        ws.cell(row, 19).value = part["spec"]
        line_amount += total
        line_net += net
        row += 1

    if fee_amount is not None:
        fee = _dec(fee_amount)
        ws.cell(row, 1).value = "APPLY TO CUTOMS CHARGE"
        ws.cell(row, 8).value = 1
        ws.cell(row, 9).value = float(fee)
        ws.cell(row, 10).value = float(fee)
        line_amount += fee
        row += 1

    total_row = row
    ws.cell(total_row, 1).value = (
        "TOTAL AMOUNT (IN WORDS): SAY SYNTHETIC US DOLLARS")
    ws.cell(total_row, 10).value = float(line_amount)
    ws.cell(total_row, 12).value = float(line_net)
    if aux_total is not None:
        ws.cell(total_row, 16).value = float(_dec(aux_total))
    return line_amount, line_net


def _write_packing_sheet(ws, parts: list[dict], *, sets: int,
                         packages: int, tare="10",
                         gross_total=None, parts_net_total=None,
                         include_parts: bool = True,
                         order_code="QM99") -> tuple:
    """按真实箱单双段结构写合成表：首段套数/件数/毛重 + 后段零件净重。"""
    headers = ["KIT", "PO", "Order Code", "MSO", "Machine Model",
               "Machine Serial No", "QTY", "Packages", None, "Dimension",
               "Weight"]
    for col, value in enumerate(headers, 1):
        ws.cell(8, col).value = value
    tare_d = _dec(tare) if tare is not None else Decimal("0")
    base_gross = Decimal("100")
    ws.cell(9, 1).value = 9900001
    ws.cell(9, 2).value = "PO-9900001"
    ws.cell(9, 3).value = order_code
    ws.cell(9, 4).value = "MSOA"
    ws.cell(9, 5).value = "922F"
    ws.cell(9, 6).value = "SN00001"
    ws.cell(9, 7).value = sets
    ws.cell(9, 8).value = packages
    ws.cell(9, 11).value = float(base_gross)
    row = 10
    if tare is not None:
        ws.cell(row, 1).value = "TARE WEIGHT"
        ws.cell(row, 11).value = float(tare_d)
        row += 1
    total_row = row
    ws.cell(total_row, 1).value = "TOTAL"
    ws.cell(total_row, 7).value = sets
    ws.cell(total_row, 8).value = packages
    actual_gross = base_gross + tare_d
    ws.cell(total_row, 11).value = float(
        _dec(gross_total) if gross_total is not None else actual_gross)
    row = total_row + 2
    ws.cell(row, 1).value = "Every kit list is as below:"
    row += 1
    lower_headers = ["Kit No", "Machine Model", "Event/ part description",
                     "CHN HS Code", "COO", "DESCRIPTION OF GOODS", None,
                     "QTY/SET", "SET QTY", "TOTAL QTY", "NET WEIGHT"]
    for col, value in enumerate(lower_headers, 1):
        ws.cell(row, col).value = value
    lower_header_row = row
    row += 1
    lower_qty = Decimal("0")
    lower_net = Decimal("0")
    if include_parts:
        for part in parts:
            qty = _dec(part["qty_per_set"]) * _dec(part["sets"])
            net = _dec(part["unit_weight"]) * qty
            ws.cell(row, 1).value = part["kit_no"]
            ws.cell(row, 2).value = "922F"
            ws.cell(row, 3).value = "Maintenance 500H"
            ws.cell(row, 4).value = part["hs"]
            ws.cell(row, 5).value = part["coo"]
            ws.cell(row, 6).value = part["part_code"]
            ws.cell(row, 8).value = part["qty_per_set"]
            ws.cell(row, 9).value = part["sets"]
            ws.cell(row, 10).value = int(qty)
            ws.cell(row, 11).value = float(net)
            lower_qty += qty
            lower_net += net
            row += 1
        ws.cell(row, 1).value = "TOTAL"
        ws.cell(row, 10).value = float(lower_qty)
        ws.cell(row, 11).value = float(
            _dec(parts_net_total) if parts_net_total is not None
            else lower_net)
        lower_total_row = row
    else:
        lower_total_row = None
    return lower_header_row, lower_total_row, lower_qty, lower_net


def _write_bl_sheet(ws, invoice_no: str) -> None:
    ws["A1"] = "Shipper"
    ws["D22"] = "Kind of packages;description of goods"
    ws["D25"] = f"INVOICE NO.:  {invoice_no}"
    ws["A27"] = "Freight Prepaid"


def _write_application_table(ws, *, order_code: str, msos: list[str],
                             serials: list[str], start_row: int = 31) -> None:
    headers = ["KIT", "PO", "Order Code", "MSO", "Machine Model",
               "Machine Serial No"]
    for col, value in enumerate(headers, 1):
        ws.cell(start_row, col).value = value
    row = start_row + 1
    for index, (mso, serial) in enumerate(zip(msos, serials)):
        ws.cell(row, 1).value = f"=RIGHT(B{row},7)"
        ws.cell(row, 2).value = f"{mso}-9900001"
        ws.cell(row, 3).value = order_code
        ws.cell(row, 4).value = mso
        ws.cell(row, 5).value = "922F"
        ws.cell(row, 6).value = serial
        row += 1
    # 模板残留行：仅 Order Code，单项身份，不得并入身份集合
    ws.cell(row, 3).value = order_code
    ws.cell(row + 1, 3).value = order_code


def build_kit_workbook(path: Path, *, parts=None, invoice_no="CQLTEST261101ABC",
                       invoice_sheets=(INVOICE_SHEET, INVOICE2_SHEET),
                       invoice2_parts=None, fee_amount="10",
                       aux_total=None, row_overrides=None, blank_fields=None,
                       write_invoice=True, write_packing=True,
                       write_bl=True, bl_invoice_no=None, tare="10",
                       gross_total=None, parts_net_total=None,
                       include_parts=True, application=None,
                       sets: int = 3, packages: int = 2,
                       order_code="QM99") -> Path:
    """构造一份合成的 Health Care Kit 工作簿（可只写部分区域）。"""
    parts = copy.deepcopy(parts or DEFAULT_PARTS)
    wb = Workbook()
    if write_invoice:
        ws = wb.active
        ws.title = invoice_sheets[0]
        _write_invoice_sheet(ws, parts, invoice_no, fee_amount=fee_amount,
                             aux_total=aux_total,
                             row_overrides=row_overrides,
                             blank_fields=blank_fields)
        for sheet_name in invoice_sheets[1:]:
            other = wb.create_sheet(sheet_name)
            other_parts = (copy.deepcopy(invoice2_parts)
                           if invoice2_parts is not None else parts)
            _write_invoice_sheet(other, other_parts, invoice_no,
                                 fee_amount=fee_amount, aux_total=None,
                                 row_overrides=None, blank_fields=None)
        if application:
            _write_application_table(
                wb[invoice_sheets[0]],
                order_code=application.get("order_code", order_code),
                msos=application.get("msos", ["MSOA"]),
                serials=application.get("serials", ["SN00001"]),
            )
    else:
        wb.remove(wb.active)
    if write_packing:
        pl = wb.create_sheet(PACKING_SHEET)
        _write_packing_sheet(
            pl, parts, sets=sets, packages=packages, tare=tare,
            gross_total=gross_total, parts_net_total=parts_net_total,
            include_parts=include_parts, order_code=order_code)
    if write_bl:
        bl = wb.create_sheet(BL_SHEET)
        _write_bl_sheet(bl, bl_invoice_no or invoice_no)
    wb.save(path)
    return path


def _ref(path: Path, regions: list[tuple[str, str]],
         filed_name: str | None = None) -> dict:
    return {
        "sha256": _sha(path),
        "filenames": [filed_name or Path(path).name],
        "regions": [{"role": role, "sheet": sheet} for role, sheet in regions],
    }


def _source(path: Path) -> dict:
    return {"path": str(path), "filename": Path(path).name,
            "sha256": _sha(path)}


def _identity(invoice_no: str, *, order_code="QM99",
              msos=("MSOA",), serials=("SN00001",)) -> dict:
    return {
        "invoice_no": [invoice_no],
        "order_code": [order_code],
        "mso": list(msos),
        "serial": list(serials),
    }


def _candidate(paths, identity: dict, regions_by_path: dict,
               *, facts=None) -> dict:
    refs = []
    for path in paths:
        refs.append(_ref(path, regions_by_path[path]))
    return {
        "candidate_id": "cand-synthetic",
        "kind": "parts",
        "identity": identity,
        "source_refs": refs,
        "facts": facts or {},
        "issues": [],
        "needs_confirmation": False,
    }


def _default_regions(*, invoice_sheets=(INVOICE_SHEET, INVOICE2_SHEET),
                     include_packing=True, include_bl=True,
                     include_application=False):
    regions = [("kit_invoice", sheet) for sheet in invoice_sheets]
    if include_packing:
        regions.append(("kit_packing", PACKING_SHEET))
    if include_bl:
        regions.append(("bl", BL_SHEET))
    if include_application:
        regions.append(("kit_application", invoice_sheets[0]))
    return regions


def _run_parser(path, candidate, sources=None, mail_rows=None, aliases=None):
    # 延迟导入：避免测试收集阶段把已确认模块置入 sys.modules，污染
    # tests/core/test_confirmed_api.py 的“旧路径不 eager 导入”断言。
    from cater.core.parsing.kit_confirmed import parse_confirmed_kit_sources
    return parse_confirmed_kit_sources(
        sources if sources is not None else [_source(path)],
        candidate, mail_rows or [], aliases=aliases)


def _parse(path, candidate, sources=None, mail_rows=None):
    return _run_parser(path, candidate, sources=sources, mail_rows=mail_rows)


def _codes(result) -> list[str]:
    return [issue.code for issue in result.issues]


def _blocking(result) -> list:
    return [issue for issue in result.issues if issue.blocking]


# ---------------------------------------------------------------------------
# 合成清洁数据：完整可转换
# ---------------------------------------------------------------------------


def test_synthetic_clean_two_identical_invoice_sheets(tmp_path):
    path = build_kit_workbook(tmp_path / "clean.xlsx")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    result = _parse(path, cand)

    assert _blocking(result) == [], [i.to_dict() for i in result.issues]
    assert result.invoice_no == "CQLTEST261101ABC"
    assert result.sheet_name == INVOICE_SHEET
    assert len(result.records) == 2
    assert result.gross_weight == Decimal("110")
    assert result.packages == Decimal("2")

    first = result.records[0]
    assert first.part_code == "SYN-P100"  # 料号 D，不是 kit 号 A
    assert first.fields["kit_no"].value == "9900001"
    assert first.fields["qty_per_set"].value == Decimal("4")
    assert first.fields["sets"].value == Decimal("3")
    assert first.fields["qty"].value == Decimal("12")
    assert first.fields["unit_price"].value == Decimal("28.11")
    assert first.fields["total_price"].value == Decimal("337.32")
    assert first.fields["unit_weight"].value == Decimal("0.9")
    assert first.fields["net_weight"].value == Decimal("10.8")
    assert first.fields["coo"].value == "US"
    assert first.fields["hs_code"].value == "84219900"
    assert first.fields["product_name"].value == "合成滤芯"
    assert first.fields["spec"].value == "1|合成申报要素A"

    layout = result.layout
    assert layout["kit_invoice"]["conflict"] is False
    assert len(layout["kit_invoice"]["sheets"]) == 2
    assert layout["kit_packing"]["gross_weight"] == "110"
    assert layout["kit_packing"]["sets"] == "3"
    assert layout["kit_packing"]["packages"] == "2"
    assert layout["kit_packing"]["tare_weight"] == "10"
    assert layout["kit_packing"]["tare_included"] is True
    assert layout["kit_packing"]["parts_net_weight"] == "36"
    assert layout["kit_packing"]["parts_rows"] == 2
    assert layout["kit_packing"]["parts_qty_sum"] == "132"
    assert layout["kit_packing"]["parts_net_sum"] == "36"
    # 上下两段 TOTAL 必须分开：首段毛重不是后段零件净重
    packing_sheet = layout["kit_packing"]["sheets"][0]
    assert packing_sheet["total_row"] != packing_sheet["parts_total_row"]
    assert Decimal(layout["kit_packing"]["gross_weight"]) != \
        Decimal(layout["kit_packing"]["parts_net_weight"])
    assert any(check["rule"] == "KIT_INVOICE_SHEETS_AGREE"
               and check["result"] == "pass" for check in layout["checks"])
    assert any(check["rule"] == "KIT_PACKING_TARE_INCLUDED"
               and check["result"] == "pass" for check in layout["checks"])

    # 完整转换到既有 transform（后续集成入口可直接复用）
    issues = []
    rows, json_records, provenance, _ = build_parts_rows(
        result.records, CountryIndex(),
        {"name": "阿联酋", "alpha3": "ARE"},
        result.gross_weight, result.packages, result.invoice_no,
        result.gross_source, result.packages_source, result.invoice_source,
        issues)
    assert issues == []
    assert len(rows) == 2
    assert rows[0]["part_code"] == "SYN-P100"
    assert rows[0]["qty"] == "12"
    assert rows[0]["unit_price"] == "28.11"
    assert rows[0]["net_weight"] == "10.8"
    assert rows[0]["coo"] == "USA"
    assert rows[0]["gross_weight"] == "110"
    assert rows[0]["packages"] == "2"
    assert rows[0]["invoice_no"] == "CQLTEST261101ABC"
    assert rows[0]["currency"] == "USD"
    assert "gross_weight" not in rows[1]
    assert {record["record_key"] for record in json_records} == {
        "SYN-P100", "SYN-B200"}


def test_synthetic_single_invoice_sheet(tmp_path):
    path = build_kit_workbook(tmp_path / "single.xlsx",
                              invoice_sheets=(INVOICE_SHEET,))
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert _blocking(result) == []
    assert len(result.records) == 2
    assert result.sheet_name == INVOICE_SHEET


def test_precision_and_binary_tail(tmp_path):
    parts = [{
        "kit_no": 9900001,
        "part_code": "PREC0001",
        "hs": "84219900",
        "coo": "CN",
        "qty_per_set": 3,
        "sets": 1,
        "unit_price": "13.5555",
        "unit_weight": "0.12345",
        "name": "精密件",
        "spec": "1|精密申报要素",
    }]
    path = build_kit_workbook(tmp_path / "precision.xlsx", parts=parts,
                              invoice_sheets=(INVOICE_SHEET,),
                              fee_amount=None, tare="0")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert _blocking(result) == []
    record = result.records[0]
    assert record.fields["unit_price"].value == Decimal("13.5555")
    assert record.fields["total_price"].value == Decimal("40.6665")
    assert record.fields["unit_weight"].value == Decimal("0.12345")
    assert record.fields["net_weight"].value == Decimal("0.37035")
    # 精度归一：浮点尾差按字段最大小数位输出
    assert result.layout["kit_invoice"]["total_amount"] == "40.6665"
    assert result.layout["kit_invoice"]["total_net"] == "0.37035"


def test_duplicate_part_code_rows_preserved(tmp_path):
    first = copy.deepcopy(DEFAULT_PARTS[0])
    second = copy.deepcopy(DEFAULT_PARTS[0])
    second["qty_per_set"] = 2
    second["unit_price"] = "30.00"
    parts = [first, second, copy.deepcopy(DEFAULT_PARTS[1])]
    path = build_kit_workbook(tmp_path / "duplicate.xlsx", parts=parts,
                              invoice_sheets=(INVOICE_SHEET,))
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert _blocking(result) == []
    assert [record.part_code for record in result.records] == [
        "SYN-P100", "SYN-P100", "SYN-B200"]
    assert [record.fields["qty"].value for record in result.records] == [
        Decimal("12"), Decimal("6"), Decimal("120")]
    # 重复料号不得被聚合/去重；行索引仍指向原单元格
    assert result.records[0].row_index != result.records[1].row_index


def test_fee_row_excluded_but_included_in_total(tmp_path):
    path = build_kit_workbook(tmp_path / "fee.xlsx", fee_amount="10",
                              invoice_sheets=(INVOICE_SHEET,))
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert _blocking(result) == []
    assert len(result.records) == 2
    assert [record.part_code for record in result.records] == [
        "SYN-P100", "SYN-B200"]
    assert result.layout["kit_invoice"]["sheets"][0]["fee_rows"][0]["amount"] == "10"
    # 费用行金额进入合计：J 合计=行合计+10
    assert result.layout["kit_invoice"]["total_amount"] == "497.32"


def test_fee_row_missing_from_total_blocks(tmp_path):
    # 手工把 TOTAL 金额改小 10：费用行未计入合计，必须阻断
    path = build_kit_workbook(tmp_path / "fee_bad.xlsx", fee_amount="10",
                              invoice_sheets=(INVOICE_SHEET,))
    wb = openpyxl.load_workbook(path)
    ws = wb[INVOICE_SHEET]
    total_row = 15
    ws.cell(total_row, 10).value = float(ws.cell(total_row, 10).value) - 10
    wb.save(path)
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert "INVOICE_AMOUNT_TOTAL_MISMATCH" in _codes(result)
    assert _blocking(result)


# ---------------------------------------------------------------------------
# 冲突/阻断
# ---------------------------------------------------------------------------


def test_two_invoice_sheets_differ_reports_conflict_no_price_basis(tmp_path):
    parts2 = copy.deepcopy(DEFAULT_PARTS)
    parts2[0]["unit_price"] = "29.11"
    parts2[1]["spec"] = "1|另一版申报要素"
    path = build_kit_workbook(tmp_path / "conflict.xlsx",
                              invoice2_parts=parts2)
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    result = _parse(path, cand)

    assert result.records == []
    assert result.sheet_name is None
    assert "INVOICE_SHEET_CONFLICT" in _codes(result)
    conflict = next(issue for issue in result.issues
                    if issue.code == "INVOICE_SHEET_CONFLICT")
    assert conflict.blocking is True
    fields = {detail["field"]
              for detail in result.layout.get("kit_invoice_sheet_diff", [])}
    assert {"unit_price", "total_price", "spec"} <= fields
    assert result.layout["kit_invoice"]["conflict"] is True
    assert result.layout["kit_invoice"]["selected_sheet"] is None
    # 两页合计不同，无法给出共同合计；记录为空，没有替用户选口径
    assert result.layout["kit_invoice"]["total_amount"] is None
    sheet_totals = {item["sheet"]: item["total_amount"]
                    for item in result.layout["kit_invoice"]["sheets"]}
    assert sheet_totals == {INVOICE_SHEET: "497.32",
                            INVOICE2_SHEET: "509.32"}


def test_auxiliary_amount_column_blocks_without_overriding_detail(tmp_path):
    path = build_kit_workbook(tmp_path / "aux.xlsx", aux_total="999.99")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    result = _parse(path, cand)
    assert "AUXILIARY_AMOUNT_CONFLICT" in _codes(result)
    assert [issue for issue in result.issues
            if issue.code == "AUXILIARY_AMOUNT_CONFLICT"][0].blocking is True
    # 明细行仍按 T'Price 读取；辅助列数值不进入任何字段
    assert len(result.records) == 2
    assert Decimal("999.99") not in {
        value.value for record in result.records
        for value in record.fields.values()}
    assert result.layout["kit_invoice"]["selected_sheet"] is not None


def test_formula_cache_missing_blocks_row(tmp_path):
    path = build_kit_workbook(tmp_path / "formula.xlsx",
                              invoice_sheets=(INVOICE_SHEET,))
    wb = openpyxl.load_workbook(path)  # 保留公式
    ws = wb[INVOICE_SHEET]
    ws["H12"] = "=F12*G12"
    wb.save(path)  # openpyxl 保存后该公式无缓存值
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert "FORMULA_CACHE_MISSING" in _codes(result)
    assert any(issue.blocking for issue in result.issues
               if issue.code == "FORMULA_CACHE_MISSING")
    # 无缓存的数量行不进入明细，其他完整行仍保留
    assert [record.part_code for record in result.records] == ["SYN-B200"]


def test_missing_chinese_name_and_hs_block(tmp_path):
    path = build_kit_workbook(
        tmp_path / "missing.xlsx", invoice_sheets=(INVOICE_SHEET,),
        blank_fields={0: ("name", "hs"), 1: ("hs",)})
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    missing = [issue for issue in result.issues
               if issue.code == "MISSING_REQUIRED_FIELD"]
    fields = {issue.field for issue in missing}
    assert {"product_name", "hs_code"} <= fields
    assert all(issue.blocking for issue in missing)
    assert len(result.records) == 2
    assert result.records[0].fields["product_name"].value is None
    assert result.records[1].fields["hs_code"].value is None


def test_hs_format_invalid_blocks(tmp_path):
    parts = copy.deepcopy(DEFAULT_PARTS)
    parts[0]["hs"] = "ABC"
    path = build_kit_workbook(tmp_path / "hsbad.xlsx", parts=parts,
                              invoice_sheets=(INVOICE_SHEET,))
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert "HS_FORMAT_INVALID" in _codes(result)
    assert any(issue.blocking for issue in result.issues
               if issue.code == "HS_FORMAT_INVALID")


def test_row_amount_and_weight_mismatch_block(tmp_path):
    path = build_kit_workbook(
        tmp_path / "rowbad.xlsx", invoice_sheets=(INVOICE_SHEET,),
        row_overrides={
            0: {"total_price": "337.33"},
            1: {"net_weight": "25.21"},
        })
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert "AMOUNT_MISMATCH" in _codes(result)
    assert "WEIGHT_MISMATCH" in _codes(result)
    assert all(issue.blocking for issue in result.issues
               if issue.code in ("AMOUNT_MISMATCH", "WEIGHT_MISMATCH"))


def test_wrong_ticket_source_blocks(tmp_path):
    path = build_kit_workbook(tmp_path / "wrong.xlsx",
                              invoice_no="CQLOTHER261101XYZ")
    # 候选身份是另一票：即使工作簿结构完整，也必须按已确认身份拒绝
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    result = _parse(path, cand)
    assert "SOURCE_IDENTITY_MISMATCH" in _codes(result)
    assert any(issue.blocking for issue in result.issues
               if issue.code == "SOURCE_IDENTITY_MISMATCH")
    assert result.records == []


def test_unauthorized_extra_source_blocks(tmp_path):
    path = build_kit_workbook(tmp_path / "authorized.xlsx")
    extra = build_kit_workbook(tmp_path / "extra.xlsx",
                               invoice_no="CQLEXTRA261101XYZ")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    result = _parse(path, cand, sources=[_source(path), _source(extra)])
    assert "UNAUTHORIZED_SOURCE" in _codes(result)
    assert any(issue.blocking for issue in result.issues
               if issue.code == "UNAUTHORIZED_SOURCE")
    assert len(result.records) == 2


def test_confirmed_region_missing_blocks(tmp_path):
    path = build_kit_workbook(tmp_path / "noregion.xlsx")
    cand = _candidate(
        [path], _identity("CQLTEST261101ABC"),
        {path: _default_regions(include_packing=False, include_bl=False)})
    # 只有发票区域：箱单区域缺失必须阻断
    result = _parse(path, cand)
    assert "CONFIRMED_REGION_MISSING" in _codes(result)
    assert any(issue.blocking for issue in result.issues
               if issue.code == "CONFIRMED_REGION_MISSING")


def test_confirmed_region_coordinate_mismatch_blocks(tmp_path):
    path = build_kit_workbook(tmp_path / "regiondrift.xlsx",
                              invoice_sheets=(INVOICE_SHEET,))
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    cand["source_refs"][0]["regions"][0]["header_row"] = 99
    result = _parse(path, cand)
    assert "CONFIRMED_REGION_MISMATCH" in _codes(result)
    assert result.records == []


def test_tare_not_included_blocks_and_is_not_added(tmp_path):
    path = build_kit_workbook(
        tmp_path / "tarebad.xlsx", invoice_sheets=(INVOICE_SHEET,),
        gross_total="100")  # base 100 + tare 10; TOTAL 被写成 100
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert "TARE_NOT_INCLUDED" in _codes(result)
    assert result.gross_weight == Decimal("100")  # 读取 TOTAL，不自行加皮重
    assert result.layout["kit_packing"]["tare_weight"] == "10"
    assert result.layout["kit_packing"]["tare_included"] is False


def test_packing_total_mismatch_blocks(tmp_path):
    path = build_kit_workbook(
        tmp_path / "grossbad.xlsx", invoice_sheets=(INVOICE_SHEET,),
        gross_total="999")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert "PACKING_TOTAL_MISMATCH" in _codes(result)
    assert result.gross_weight == Decimal("999")


def test_candidate_facts_are_verification_only(tmp_path):
    path = build_kit_workbook(tmp_path / "facts.xlsx")
    facts = {
        "amount": {"value": "1.00", "conflict": False, "sources": []},
        "gross_weight": {"value": "9999", "conflict": False, "sources": []},
    }
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()}, facts=facts)
    result = _parse(path, cand, mail_rows=[{
        "table_index": 0, "row": 1,
        "fields": {"invoice_no": "CQLTEST261101ABC", "amount": "1.00"},
    }])
    assert "CONFIRMED_FACT_DRIFT" in _codes(result)
    # 明细/合计来自工作簿，不从 preview facts 或 mail_rows 填充
    assert result.gross_weight == Decimal("110")
    assert len(result.records) == 2
    assert result.records[0].fields["total_price"].value == Decimal("337.32")
    assert result.layout["kit_invoice"]["total_amount"] == "497.32"


def test_same_ticket_split_across_two_files(tmp_path):
    invoice_path = build_kit_workbook(
        tmp_path / "invoice_only.xlsx", write_packing=False, write_bl=False)
    packing_path = build_kit_workbook(
        tmp_path / "packing_only.xlsx", write_invoice=False)
    cand = _candidate(
        [invoice_path, packing_path], _identity("CQLTEST261101ABC"),
        {
            invoice_path: [("kit_invoice", sheet)
                           for sheet in (INVOICE_SHEET, INVOICE2_SHEET)],
            packing_path: [("kit_packing", PACKING_SHEET), ("bl", BL_SHEET)],
        })
    result = _parse(invoice_path, cand,
                    sources=[_source(invoice_path), _source(packing_path)])
    assert _blocking(result) == []
    assert len(result.records) == 2
    assert result.gross_weight == Decimal("110")
    assert result.packages == Decimal("2")
    assert result.invoice_no == "CQLTEST261101ABC"


def test_application_identity_matches_and_mismatch_blocks(tmp_path):
    path = build_kit_workbook(
        tmp_path / "app.xlsx", invoice_sheets=(INVOICE_SHEET,),
        application={"order_code": "QM99", "msos": ["MSOA"],
                     "serials": ["SN00001"]})
    identity = _identity("CQLTEST261101ABC", msos=("MSOA",),
                         serials=("SN00001",))
    cand = _candidate([path], identity,
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,),
                          include_application=True)})
    result = _parse(path, cand)
    assert _blocking(result) == []
    assert any(check["rule"] == "KIT_IDENTITY"
               and check["result"] == "pass"
               for check in result.layout["checks"])

    # 已确认身份与应用表不一致：疑似错票/混票
    cand["identity"]["mso"] = ["OTHER"]
    result = _parse(path, cand)
    assert "SOURCE_IDENTITY_MISMATCH" in _codes(result)


def test_multiple_invoice_files_differ_conflict_no_file_order_winner(tmp_path):
    """同票分文件时，两个文件各有一页发票且价格不同：不得按文件顺序胜出。"""
    main_path = build_kit_workbook(tmp_path / "main.xlsx")
    changed = copy.deepcopy(DEFAULT_PARTS)
    changed[0]["unit_price"] = "30.11"
    second_path = build_kit_workbook(
        tmp_path / "second.xlsx", invoice_sheets=(INVOICE_SHEET,),
        invoice2_parts=None, write_packing=False, write_bl=False,
        parts=changed)
    cand = _candidate(
        [main_path, second_path], _identity("CQLTEST261101ABC"),
        {
            main_path: _default_regions(),
            second_path: [("kit_invoice", INVOICE_SHEET)],
        })
    result = _parse(main_path, cand,
                    sources=[_source(main_path), _source(second_path)])
    assert "INVOICE_SHEET_CONFLICT" in _codes(result)
    assert result.records == []
    assert result.layout["kit_invoice"]["selected_sheet"] is None
    # 箱单不受发票价格口径冲突影响，仍可读取整票毛重/件数
    assert result.gross_weight == Decimal("110")


def test_multiple_packing_files_differ_conflict(tmp_path):
    invoice_path = build_kit_workbook(
        tmp_path / "inv.xlsx", write_packing=False, write_bl=False)
    packing_a = build_kit_workbook(
        tmp_path / "pack_a.xlsx", write_invoice=False, gross_total="110")
    packing_b = build_kit_workbook(
        tmp_path / "pack_b.xlsx", write_invoice=False, gross_total="220")
    cand = _candidate(
        [invoice_path, packing_a, packing_b],
        _identity("CQLTEST261101ABC"),
        {
            invoice_path: [("kit_invoice", sheet)
                           for sheet in (INVOICE_SHEET, INVOICE2_SHEET)],
            packing_a: [("kit_packing", PACKING_SHEET)],
            packing_b: [("kit_packing", PACKING_SHEET)],
        })
    result = _parse(invoice_path, cand,
                    sources=[_source(invoice_path), _source(packing_a),
                             _source(packing_b)])
    assert "PACKING_SHEET_CONFLICT" in _codes(result)
    assert result.gross_weight is None
    assert result.packages is None
    assert len(result.records) == 2


def test_unconfirmed_kit_workbook_without_region_blocks(tmp_path):
    path = build_kit_workbook(tmp_path / "no_region.xlsx")
    cand = _candidate([path], _identity("CQLTEST261101ABC"), {path: []})
    result = _parse(path, cand)
    assert "CONFIRMED_REGION_MISSING" in _codes(result)
    assert result.records == []
    assert result.layout["kit_invoice"]["sheets"] == []


def test_custom_alias_and_optional_titled_hs_column(tmp_path):
    path = build_kit_workbook(tmp_path / "alias.xlsx",
                              invoice_sheets=(INVOICE_SHEET,))
    wb = openpyxl.load_workbook(path)
    ws = wb[INVOICE_SHEET]
    ws["D10"] = "物料编号"  # 自定义料号表头
    ws["B10"] = None
    ws["B12"] = None
    ws["B13"] = None
    wb.save(path)
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand,
                    )
    # 默认别名不含“物料编号”：未追加时必须报结构不完整
    assert "KIT_INVOICE_INVALID" in _codes(result)

    result = _run_parser(path, cand, aliases={"part_code": ["物料编号"]})
    assert _blocking(result) == []
    assert [record.part_code for record in result.records] == [
        "SYN-P100", "SYN-B200"]
    # CHN HS Code 列缺失不单独阻断，HS 以补充列为准
    assert result.records[0].fields["hs_code"].value == "84219900"
    assert result.records[0].fields["hs_code_titled"].value is None


def test_candidate_fact_conflict_blocks_but_details_still_read(tmp_path):
    path = build_kit_workbook(tmp_path / "factconflict.xlsx")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    cand["facts"] = {
        "amount": {"conflict": True, "value": None, "variants": []},
    }
    result = _parse(path, cand)
    assert "CANDIDATE_FACT_CONFLICT" in _codes(result)
    assert any(issue.blocking for issue in result.issues
               if issue.code == "CANDIDATE_FACT_CONFLICT")
    # 候选事实冲突不改变正式明细读取
    assert len(result.records) == 2
    assert result.records[0].fields["total_price"].value == Decimal("337.32")


def test_packing_columns_located_semantically_not_by_letter(tmp_path):
    """箱单首段列被移动/互换后，仍按表头语义取套数/件数/毛重。"""
    path = build_kit_workbook(tmp_path / "moved_columns.xlsx",
                              invoice_sheets=(INVOICE_SHEET,))
    wb = openpyxl.load_workbook(path)
    ws = wb[PACKING_SHEET]
    total_row = next(row for row in range(9, ws.max_row + 1)
                     if str(ws.cell(row, 1).value).strip().upper() == "TOTAL")
    # 互换 G(QTY/套数) 与 H(Packages/件数) 两列
    for row in range(8, total_row + 1):
        g, h = ws.cell(row, 7).value, ws.cell(row, 8).value
        ws.cell(row, 7).value = h
        ws.cell(row, 8).value = g
    # 毛重列 K 移到 M，不再位于原 K 列
    for row in range(8, total_row + 1):
        ws.cell(row, 13).value = ws.cell(row, 11).value
        ws.cell(row, 11).value = None
    wb.save(path)

    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _parse(path, cand)
    assert _blocking(result) == []
    assert result.packages == Decimal("2")   # Packages 现在 G 列
    assert result.gross_weight == Decimal("110")  # Weight 现在 M 列
    assert result.layout["kit_packing"]["sets"] == "3"  # QTY 现在 H 列
    assert result.layout["kit_packing"]["packages"] == "2"
    assert result.layout["kit_packing"]["gross_weight"] == "110"


def test_stale_bl_invoice_blocks_without_changing_identity(tmp_path):
    path = build_kit_workbook(
        tmp_path / "stale_bl.xlsx", bl_invoice_no="CQLSTALE000000")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    result = _parse(path, cand)
    assert "STALE_BL_INVOICE" in _codes(result)
    assert all(issue.blocking for issue in result.issues
               if issue.code == "STALE_BL_INVOICE")
    # BL 只用于冲突核对，当前发票号仍来自发票页
    assert result.invoice_no == "CQLTEST261101ABC"
    assert result.layout["kit_bl"]["stale_invoice_nos"] == ["CQLSTALE000000"]


def test_synthetic_all_real_conflicts_combined(tmp_path):
    """完全合成数据覆盖真实四票同型冲突：发票页差价、辅助列、旧 BL。"""
    parts2 = copy.deepcopy(DEFAULT_PARTS)
    parts2[0]["unit_price"] = "30.11"
    path = build_kit_workbook(
        tmp_path / "all_conflicts.xlsx", invoice2_parts=parts2,
        aux_total="999.99", bl_invoice_no="CQLSTALE000000")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    result = _parse(path, cand)
    codes = set(_codes(result))
    assert {"INVOICE_SHEET_CONFLICT", "AUXILIARY_AMOUNT_CONFLICT",
            "STALE_BL_INVOICE"} <= codes
    assert result.records == []
    assert result.sheet_name is None
    assert result.layout["kit_invoice"]["conflict"] is True
    # 箱单首段/后段信息仍可读，但不选任何价格口径
    assert result.gross_weight == Decimal("110")
    assert result.layout["kit_packing"]["parts_net_weight"] == "36"


# ---------------------------------------------------------------------------
# 非有限 / 极端指数：统一结构化阻断，不进入算术
# ---------------------------------------------------------------------------

NON_FINITE_CASES = [
    ("NaN", "NON_FINITE_NUMBER"),
    ("sNaN", "NON_FINITE_NUMBER"),
    ("Infinity", "NON_FINITE_NUMBER"),
    ("-Infinity", "NON_FINITE_NUMBER"),
    ("1E+1000000", "NUMBER_OUT_OF_RANGE"),
    ("-1E+1000000", "NUMBER_OUT_OF_RANGE"),
    ("1E-1000000", "NUMBER_OUT_OF_RANGE"),
]


@pytest.mark.parametrize("bad_value,expected_code", NON_FINITE_CASES)
def test_non_finite_unit_price_blocks_with_exact_cell(
        tmp_path, bad_value, expected_code):
    path = build_kit_workbook(tmp_path / "bad_price.xlsx")
    wb = openpyxl.load_workbook(path)
    for sheet in (INVOICE_SHEET, INVOICE2_SHEET):
        wb[sheet]["I12"] = bad_value
    wb.save(path)

    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    result = _run_parser(path, cand)  # 不得抛 InvalidOperation/Overflow

    targets = [issue for issue in result.issues
               if issue.code == expected_code
               and issue.field == "unit_price"]
    assert targets, [issue.to_dict() for issue in result.issues]
    assert all(issue.blocking for issue in targets)
    assert {issue.sources[0].sheet for issue in targets} == {
        INVOICE_SHEET, INVOICE2_SHEET}
    assert {issue.sources[0].cell for issue in targets} == {"I12"}
    # 非法明细行不得形成可用记录；其他合法行仍保留但整体被阻断
    assert "SYN-P100" not in [record.part_code for record in result.records]
    assert "SYN-B200" in [record.part_code for record in result.records]
    assert any(issue.blocking for issue in result.issues)


@pytest.mark.parametrize("bad_value,expected_code", [
    ("NaN", "NON_FINITE_NUMBER"),
    ("-Infinity", "NON_FINITE_NUMBER"),
    ("1E+1000000", "NUMBER_OUT_OF_RANGE"),
])
def test_non_finite_total_fee_and_auxiliary_cells_block(
        tmp_path, bad_value, expected_code):
    path = build_kit_workbook(tmp_path / "bad_totals.xlsx",
                              invoice_sheets=(INVOICE_SHEET,))
    wb = openpyxl.load_workbook(path)
    ws = wb[INVOICE_SHEET]
    total_row = 15
    ws.cell(total_row - 1, 10).value = bad_value  # J：无料号费用行
    ws.cell(total_row, 10).value = bad_value   # J：TOTAL 金额
    ws.cell(total_row, 12).value = bad_value   # L：TOTAL 净重
    ws.cell(total_row, 16).value = bad_value   # P：辅助金额列
    wb.save(path)

    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _run_parser(path, cand)
    targets = [issue for issue in result.issues
               if issue.code == expected_code]
    assert targets
    assert all(issue.blocking for issue in targets)
    assert any(issue.sources and issue.sources[0].cell == "J14"
               for issue in targets)
    assert any(issue.sources and issue.sources[0].cell == "J15"
               for issue in targets)
    assert any(issue.sources and issue.sources[0].cell == "L15"
               for issue in targets)
    assert any(issue.sources and issue.sources[0].cell == "P15"
               for issue in targets)


@pytest.mark.parametrize("bad_value,expected_code", [
    ("Infinity", "NON_FINITE_NUMBER"),
    ("1E+1000000", "NUMBER_OUT_OF_RANGE"),
])
def test_non_finite_packing_totals_and_tare_block(
        tmp_path, bad_value, expected_code):
    path = build_kit_workbook(tmp_path / "bad_packing.xlsx",
                              invoice_sheets=(INVOICE_SHEET,))
    wb = openpyxl.load_workbook(path)
    ws = wb[PACKING_SHEET]
    total_row = next(row for row in range(9, ws.max_row + 1)
                     if str(ws.cell(row, 1).value).strip().upper() == "TOTAL")
    ws.cell(total_row, 11).value = bad_value   # K：首段毛重合计
    ws.cell(total_row - 1, 11).value = bad_value  # K：皮重行
    lower_total = max(row for row in range(total_row + 1, ws.max_row + 1)
                      if str(ws.cell(row, 1).value).strip().upper() == "TOTAL")
    ws.cell(lower_total, 10).value = bad_value  # J：后段总数量
    ws.cell(lower_total, 11).value = bad_value  # K：后段净重合计
    wb.save(path)

    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _run_parser(path, cand)
    targets = [issue for issue in result.issues
               if issue.code == expected_code and issue.sources
               and issue.sources[0].sheet == PACKING_SHEET]
    assert targets
    assert all(issue.blocking for issue in targets)
    assert len({(issue.sources[0].cell) for issue in targets}) >= 3


def test_non_finite_candidate_facts_and_mail_rows_block(tmp_path):
    path = build_kit_workbook(tmp_path / "bad_facts.xlsx")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions()})
    cand["facts"] = {
        "amount": {
            "value": "NaN", "conflict": False,
            "sources": [{"type": "workbook", "filename": "x.xlsx",
                         "sheet": "Invoice ", "cell": "J15"}],
        },
        "gross_weight": {
            "value": "1E+1000000", "conflict": False,
            "sources": [{"type": "workbook", "filename": "x.xlsx",
                         "sheet": "Packing list", "cell": "K11"}],
        },
    }
    mail_rows = [{
        "table_index": 0, "row": 2,
        "fields": {"qty": "Infinity", "amount": "NaN"},
    }]
    result = _run_parser(path, cand, mail_rows=mail_rows)
    codes = _codes(result)
    assert "NON_FINITE_NUMBER" in codes
    assert "NUMBER_OUT_OF_RANGE" in codes
    fact_issues = [issue for issue in result.issues
                   if issue.code in ("NON_FINITE_NUMBER",
                                     "NUMBER_OUT_OF_RANGE")
                   and issue.sources and issue.sources[0].type == "workbook"]
    assert any(issue.sources[0].cell == "J15" for issue in fact_issues)
    assert any(issue.sources[0].cell == "K11" for issue in fact_issues)
    mail_issues = [issue for issue in result.issues
                   if issue.code in ("NON_FINITE_NUMBER",
                                     "NUMBER_OUT_OF_RANGE")
                   and issue.sources and issue.sources[0].type == "mail_html"]
    assert {issue.field for issue in mail_issues} >= {"qty", "amount"}
    # 非法 facts/正文只阻断，不改变明细读取
    assert result.gross_weight == Decimal("110")
    assert len(result.records) == 2


@pytest.mark.parametrize("legal_value", ["0.0000001", "1.234567890123"])
def test_legal_small_and_precise_values_not_rejected(tmp_path, legal_value):
    parts = copy.deepcopy(DEFAULT_PARTS[:1])
    parts[0]["unit_price"] = legal_value
    path = build_kit_workbook(tmp_path / "legal.xlsx", parts=parts,
                              invoice_sheets=(INVOICE_SHEET,),
                              fee_amount=None, tare="0")
    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _run_parser(path, cand)
    assert _blocking(result) == [], [i.to_dict() for i in result.issues]
    assert result.records[0].fields["unit_price"].value == Decimal(legal_value)


def test_non_finite_detail_qty_and_weight_headers_block(tmp_path):
    path = build_kit_workbook(tmp_path / "bad_detail_fields.xlsx",
                              invoice_sheets=(INVOICE_SHEET,))
    wb = openpyxl.load_workbook(path)
    ws = wb[INVOICE_SHEET]
    ws["F12"] = "NaN"          # 每套数量
    ws["G12"] = "Infinity"     # 套数
    ws["H12"] = "-Infinity"    # 总数量
    ws["K12"] = "1E+1000000"   # 单件净重
    ws["L12"] = "NaN"          # 总净重
    wb.save(path)

    cand = _candidate([path], _identity("CQLTEST261101ABC"),
                      {path: _default_regions(
                          invoice_sheets=(INVOICE_SHEET,))})
    result = _run_parser(path, cand)
    numerical = [issue for issue in result.issues
                 if issue.code in ("NON_FINITE_NUMBER",
                                   "NUMBER_OUT_OF_RANGE")
                 and issue.sources and issue.sources[0].row == 12]
    assert {issue.field for issue in numerical} >= {
        "qty_per_set", "sets", "qty", "unit_weight", "net_weight"}
    assert {issue.sources[0].cell for issue in numerical} >= {
        "F12", "G12", "H12", "K12", "L12"}
    assert "SYN-P100" not in [record.part_code for record in result.records]
    assert all(issue.blocking for issue in numerical)
