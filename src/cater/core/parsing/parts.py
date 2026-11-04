"""配件发票工作簿解析。

选表规则（plan.md §5）：
- 优先含补充资料的 ``CI-For Dealer (2)``，不能只根据名字选表：
  须校验明细表头、K 列之后的补充字段（M/N/O）、有效料号行和价格信息；
- 原表 ``CI-For Dealer`` 不能自动替代最终调整表；
- 找不到最终表或无法唯一选定时暂停并追问。

列映射：料号←B、数量←H、单价←最终表 I、净重←L、原产国←E、
商品编码←M（与 D 冲突时以 M 为准，登记非阻塞提示）、商品名称←N、规格型号←O。
净重核对 H 数量×K 单件重量，不除以数量。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ..models import FieldValue, Issue, PartRecord, Source
from ..numbertext import decimals_close, to_decimal
from .xlsxutil import find_header_row

FINAL_SHEET_RE = re.compile(r"^CI-For Dealer\s*\(\d+\)\s*$", re.IGNORECASE)
BASE_SHEET_RE = re.compile(r"^CI-For Dealer\s*$", re.IGNORECASE)

# 明细表头（第 17 行样式）：B=Part Code, H=TOTAL QTY, I=UNIT PRICE
HEADER_REQUIRED = {2: "part code", 8: "total qty", 9: "unit price"}

INVOICE_NO_RE = re.compile(r"INVOICE\s+NO\.?\s*:?\s*([\w\-/]+)", re.IGNORECASE)


@dataclass
class PartsParseResult:
    sheet_name: Optional[str] = None
    records: list = field(default_factory=list)      # list[PartRecord]
    gross_weight: Optional[Decimal] = None           # Packing List TOTAL J
    packages: Optional[Decimal] = None               # Packing List TOTAL G
    gross_source: Optional[Source] = None
    packages_source: Optional[Source] = None
    invoice_no: Optional[str] = None
    invoice_source: Optional[Source] = None
    issues: list = field(default_factory=list)


def _sheet_is_final_candidate(ws_values) -> bool:
    """结构校验：表头匹配 + 数据区存在补充字段（M/N/O）与价格。"""
    header_row = find_header_row(ws_values, HEADER_REQUIRED)
    if header_row is None:
        return False
    supplementary = 0
    price_cells = 0
    for r in range(header_row + 1, min(ws_values.max_row, header_row + 40) + 1):
        if ws_values.cell(r, 13).value not in (None, "") and ws_values.cell(r, 14).value:
            supplementary += 1
        if to_decimal(ws_values.cell(r, 9).value) is not None:
            price_cells += 1
    return supplementary >= 1 and price_cells >= 1


def select_final_sheet(wb_values, filename: str, issues: list):
    """返回最终工作表或 None（问题已登记）。"""
    finals = [name for name in wb_values.sheetnames if FINAL_SHEET_RE.match(name)]
    base = [name for name in wb_values.sheetnames if BASE_SHEET_RE.match(name)]
    valid_finals = [n for n in finals if _sheet_is_final_candidate(wb_values[n])]
    if len(valid_finals) == 1:
        return wb_values[valid_finals[0]]
    if len(valid_finals) > 1:
        issues.append(Issue(
            code="AMBIGUOUS_FINAL_SHEET",
            message=f"存在多个合格的最终发票表：{valid_finals}，无法唯一选定",
            sources=[Source(type="workbook", filename=filename)],
            required_action="请确认应使用哪张最终调整表",
        ))
        return None
    if finals:  # 有名字但结构不合格
        issues.append(Issue(
            code="FINAL_SHEET_INVALID",
            message=f"最终发票表 {finals} 缺少补充字段或价格信息，校验未通过",
            sources=[Source(type="workbook", filename=filename)],
            required_action="请补充最终调整表的 M/N/O 列及单价后重发",
        ))
        return None
    if base:
        issues.append(Issue(
            code="FINAL_SHEET_NOT_FOUND",
            message="未找到含补充资料的最终发票表（如 CI-For Dealer (2)），原表不能自动替代",
            sources=[Source(type="workbook", filename=filename, sheet=base[0])],
            required_action="请提供含补充资料（K 列之后）的最终调整表",
        ))
        return None
    return None  # 调用方按“不含配件发票”处理


def _parse_invoice_no(ws, filename: str):
    for r in range(1, min(ws.max_row, 16) + 1):
        for c in range(1, min(ws.max_column, 12) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "INVOICE" in v.upper():
                m = INVOICE_NO_RE.search(v.replace("\n", " "))
                if m:
                    return m.group(1).strip(), Source(
                        type="workbook", filename=filename, sheet=ws.title,
                        cell=ws.cell(r, c).coordinate)
    return None, None


def _parse_packing_totals(wb_values, filename: str, issues: list):
    """Packing List 表 TOTAL 行：毛重=J，件数=G。"""
    name = next((n for n in wb_values.sheetnames if n.strip().lower() == "packing list"), None)
    if name is None:
        return None, None, None, None
    ws = wb_values[name]
    for r in range(1, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if isinstance(a, str) and a.strip().upper() == "TOTAL":
            gross = to_decimal(ws.cell(r, 10).value)
            pkgs = to_decimal(ws.cell(r, 7).value)
            return (gross, pkgs,
                    Source(type="workbook", filename=filename, sheet=name, cell=f"J{r}"),
                    Source(type="workbook", filename=filename, sheet=name, cell=f"G{r}"))
    return None, None, None, None


def parse_parts_workbook(path: str, filename: str) -> PartsParseResult:
    from .xlsxutil import load_workbook_pair, cell_value

    result = PartsParseResult()
    wb_values, wb_formula = load_workbook_pair(path)
    ws = select_final_sheet(wb_values, filename, result.issues)

    result.gross_weight, result.packages, result.gross_source, result.packages_source = \
        _parse_packing_totals(wb_values, filename, result.issues)

    if ws is None:
        return result

    ws_f = wb_formula[ws.title]
    result.invoice_no, result.invoice_source = _parse_invoice_no(ws, filename)

    header_row = find_header_row(ws, HEADER_REQUIRED)
    for r in range(header_row + 1, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if isinstance(a, str) and a.strip().upper().startswith("TOTAL"):
            break
        part_code = ws.cell(r, 2).value
        qty = cell_value(ws, ws_f, r, 8, record_key=None, field="qty",
                         filename=filename, issues=result.issues)
        price = cell_value(ws, ws_f, r, 9, record_key=None, field="unit_price",
                           filename=filename, issues=result.issues)
        if part_code in (None, ""):
            continue  # 费用行（如 APPLY TO CUSTOMS CHARGE）等：跳过
        qty_d = to_decimal(qty)
        price_d = to_decimal(price)
        if qty_d is None or price_d is None:
            continue  # 说明行、区间行等非明细行
        part_code = str(part_code).strip()
        src = lambda col: Source(type="workbook", filename=filename, sheet=ws.title,
                                 cell=f"{col}{r}")
        net = cell_value(ws, ws_f, r, 12, record_key=part_code, field="net_weight",
                         filename=filename, issues=result.issues)
        net_d = to_decimal(net)
        unit_w = to_decimal(ws.cell(r, 11).value)
        if net_d is not None and unit_w is not None and qty_d is not None:
            if not decimals_close(qty_d * unit_w, net_d, max_dp=4):
                result.issues.append(Issue(
                    code="WEIGHT_MISMATCH",
                    record_key=part_code,
                    field="net_weight",
                    message=f"净重核对失败：L{r}={net_d} 与 H×K={qty_d}×{unit_w} 不一致",
                    sources=[src("L"), src("H"), src("K")],
                    required_action="请核对最终表数量、单件重量与总净重",
                ))
        hs_d = ws.cell(r, 4).value
        hs_m = ws.cell(r, 13).value
        if hs_d not in (None, "") and hs_m not in (None, "") \
                and str(hs_d).strip() != str(hs_m).strip():
            result.issues.append(Issue(
                code="HS_CODE_DVERSUS_M",
                record_key=part_code,
                field="hs_code",
                message=f"D 列 {hs_d} 与 M 列 {hs_m} 不一致，按已确认规则以 M 列为准",
                sources=[src("D"), src("M")],
                required_action="无需操作；如需以 D 列为准请明确回复",
                blocking=False,
            ))
        rec = PartRecord(part_code=part_code, row_index=r)
        rec.fields = {
            "part_code": FieldValue(part_code, part_code, src("B")),
            "qty": FieldValue(qty, qty_d, src("H")),
            "unit_price": FieldValue(price, price_d, src("I")),
            "net_weight": FieldValue(net, net_d, src("L")),
            "unit_weight": FieldValue(ws.cell(r, 11).value, unit_w, src("K")),
            "coo": FieldValue(ws.cell(r, 5).value,
                              str(ws.cell(r, 5).value).strip() if ws.cell(r, 5).value else None,
                              src("E")),
            "hs_code": FieldValue(hs_m, str(hs_m).strip() if hs_m is not None else None, src("M")),
            "product_name": FieldValue(ws.cell(r, 14).value,
                                       ws.cell(r, 14).value, src("N")),
            "spec": FieldValue(ws.cell(r, 15).value, ws.cell(r, 15).value, src("O")),
        }
        result.records.append(rec)
    if not result.records:
        result.issues.append(Issue(
            code="NO_RECORDS",
            message=f"最终发票表 {ws.title} 未解析到任何有效料号明细行",
            sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
            required_action="请确认附件包含有效明细",
        ))
    return result
