"""配件发票工作簿解析（多结构版本）。

选表与结构规则（plan.md §5 + plan/tasks/claude-flexible-input.md 阶段 2/3）：
- 优先含补充资料的 ``CI-For Dealer (2)``，不能只根据名字选表：
  须通过结构校验（语义表头定位 + 无标题补充列锚点校验 + 有效料号行）；
- 原表 ``CI-For Dealer`` 不能自动替代最终调整表；多个合格最终表或
  结构不完整必须暂停追问；
- 语义列定位：表头别名可配置、大小写/空格/换行/标点规范化、支持
  双行表头（U'weight/kg、T'weight/kg 在次表头行）与表/列/行移动；
- 无标题补充列（商品编码/中文名/申报要素）按已知结构锚点取表头区
  右侧连续三列，并做格式与一致性校验，校验不过不猜测放行；
- 箱单 TOTAL 行的件数/毛重同样按表头语义定位（PACKAGE、GROSS WEIGHT）；
- 净重核对 数量×单件重量=总净重，明细净重合计与 TOTAL 行交叉核对；
- 费用行（无料号）与合计行跳过；原表中间计算列不属于语义字段，
  绝不导出。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ..models import FieldValue, Issue, PartRecord, Source
from ..numbertext import decimals_close, to_decimal
from .headers import (SCAN_ROW_LIMIT, SUBHEADER_FIELDS, _match_row,
                      column_letter, merged_aliases,
                      normalize_header, resolve_columns, select_total_row)
from .workbook import cell_value, load_workbook_pair

FINAL_SHEET_RE = re.compile(r"^CI-For Dealer\s*\(\d+\)\s*$", re.IGNORECASE)
BASE_SHEET_RE = re.compile(r"^CI-For Dealer\s*$", re.IGNORECASE)
PACKING_NAME_KEY = "packinglist"

INVOICE_NO_RE = re.compile(r"INVOICE\s+NO\.?\s*:?\s*([\w\-/]+)", re.IGNORECASE)
HS_CODE_VALUE_RE = re.compile(r"^\d{8,13}$")

# 规则 ID（日志/结果/回复正文引用，禁止泄露正文内容）
RULE_PARTS_WEIGHT_ROW = "PARTS_WEIGHT_QTY_X_UNIT"
RULE_PARTS_NET_TOTAL = "PARTS_DETAIL_NET_TOTAL"
RULE_PARTS_HS_AGREE = "PARTS_HS_TITLED_VS_SUPP"

# 最终发票表明细语义字段（表头行 + 次表头行）
INVOICE_FIELDS = ["part_code", "qty", "unit_price", "coo",
                  "hs_code_titled", "unit_weight", "net_weight"]
# coo 是输出必需字段（原产国列）：缺失必须结构化暂停，
# 禁止在取数阶段 KeyError 技术故障。
INVOICE_REQUIRE = ["part_code", "qty", "unit_price", "coo",
                   "unit_weight", "net_weight"]
PACKING_FIELDS = ["packages", "gross_weight"]
PACKING_REQUIRE = ["packages", "gross_weight"]

# 无标题补充列的固定角色（表头区右侧连续三列）
SUPP_FIELDS = ("hs_code", "product_name", "spec")


@dataclass
class PartsParseResult:
    sheet_name: Optional[str] = None
    records: list = field(default_factory=list)      # list[PartRecord]
    gross_weight: Optional[Decimal] = None           # Packing List TOTAL 行（语义列）
    packages: Optional[Decimal] = None               # Packing List TOTAL 行（语义列）
    gross_source: Optional[Source] = None
    packages_source: Optional[Source] = None
    invoice_no: Optional[str] = None
    invoice_source: Optional[Source] = None
    aliases: Optional[dict] = None
    layout: dict = field(default_factory=lambda: {"checks": [], "parts_invoice": {},
                                                  "packing_total": {}})
    issues: list = field(default_factory=list)


def _resolve_invoice_columns(ws, aliases):
    """语义定位最终发票表列；返回 ColumnResolution（有界全有效区扫描）。"""
    return resolve_columns(ws, INVOICE_FIELDS, aliases,
                           require=INVOICE_REQUIRE)


def _best_effort_gap(ws, aliases) -> dict:
    """无候选表头行时，按覆盖率最高的行报告真实缺失/多候选字段。

    resolve_columns 在无行覆盖全部必需字段时返回整份 require 列表，
    对操作者不友好；这里给出“最接近表头”的一行及其真实缺口。
    """
    primary = [n for n in INVOICE_REQUIRE if n not in SUBHEADER_FIELDS]
    sub = [n for n in INVOICE_REQUIRE if n in SUBHEADER_FIELDS]
    best = {"row": 0, "missing": list(INVOICE_REQUIRE), "ambiguous": []}
    best_score = -1
    limit = min(ws.max_row, SCAN_ROW_LIMIT)
    for row in range(1, limit + 1):
        m = _match_row(ws, row, aliases, primary)
        matched = set(m["columns"]) | {n for n, _ in m["ambiguous"]}
        score = len(matched)
        if sub and row + 1 <= limit:
            ms = _match_row(ws, row + 1, aliases, sub)
            matched |= set(ms["columns"]) | {n for n, _ in ms["ambiguous"]}
        if score > best_score:
            best_score = score
            best = {"row": row,
                    "missing": [n for n in INVOICE_REQUIRE if n not in matched],
                    "ambiguous": list(m["ambiguous"])}
            if not best["missing"] and not best["ambiguous"]:
                break
    return best


def _supplementary_columns(ws, resolution):
    """无标题补充列锚点：表头/次表头区右侧连续三列（商品编码/中文名/申报要素）。"""
    def rightmost(row: int) -> int:
        last = 0
        if row:
            for col in range(1, ws.max_column + 1):
                if ws.cell(row, col).value not in (None, ""):
                    last = col
        return last

    start = max(rightmost(resolution.header_row),
                rightmost(resolution.subheader_row)) + 1
    return {name: start + offset for offset, name in enumerate(SUPP_FIELDS)}


def _validate_supplementary(ws, resolution, supp, first_row, last_row,
                            filename, issues) -> bool:
    """补充列结构校验：HS 格式正确 + 名称/要素非空。

    表头 HS Code 列与补充列取值不同属于已确认业务冲突（以补充列为准、
    非阻塞提示），不构成结构不合格。
    """
    rows = range(first_row, last_row + 1)
    hs_col = supp["hs_code"]
    titled_col = resolution.columns.get("hs_code_titled")
    hs_seen = named = spec_seen = 0
    for r in rows:
        if ws.cell(r, resolution.columns["part_code"]).value in (None, ""):
            continue
        hs = ws.cell(r, hs_col).value
        name = ws.cell(r, supp["product_name"]).value
        spec = ws.cell(r, supp["spec"]).value
        hs_text = str(hs).strip() if hs is not None else ""
        if HS_CODE_VALUE_RE.match(hs_text):
            hs_seen += 1
        if name not in (None, ""):
            named += 1
        if spec not in (None, ""):
            spec_seen += 1
    if hs_seen == 0 or named == 0 or spec_seen == 0:
        issues.append(Issue(
            code="SUPPLEMENTARY_COLUMNS_INVALID",
            message=(
                f"最终发票表 {ws.title} 的无标题补充列（{column_letter(hs_col)} 起三列）"
                f"结构校验未通过：有效HS={hs_seen} 名称={named} 要素={spec_seen}"
            ),
            sources=[Source(type="workbook", filename=filename, sheet=ws.title,
                            cell=f"{column_letter(hs_col)}{resolution.header_row}")],
            required_action="请确认最终调整表 K 列之后依次为 商品编码/中文名/申报要素 后重发",
        ))
        return False
    return True


def _sheet_structure_valid(ws, aliases) -> bool:
    """表头与补充列结构校验（不要求存在明细行；空表在正式解析阶段报 NO_RECORDS）。"""
    resolution = _resolve_invoice_columns(ws, aliases)
    if resolution.missing or resolution.ambiguous or resolution.multiple_regions:
        return False
    supp = _supplementary_columns(ws, resolution)
    if supp["spec"] > ws.max_column:
        return False
    probe = _probe_rows(ws, resolution)
    if not probe:
        return True
    return _validate_supplementary(ws, resolution, supp, probe[0], probe[-1],
                                   filename="", issues=probe_issues)


def _sheet_is_final_candidate(ws, aliases) -> bool:
    """结构校验：语义表头完整 + 无歧义 + 补充列合格 + 有效料号行与价格。"""
    if not _sheet_structure_valid(ws, aliases):
        return False
    resolution = _resolve_invoice_columns(ws, aliases)
    probe = _probe_rows(ws, resolution)
    if not probe:
        return False
    price_cells = 0
    for r in probe:
        if to_decimal(ws.cell(r, resolution.columns["unit_price"]).value) is not None:
            price_cells += 1
    return price_cells >= 1


def _probe_rows(ws, resolution) -> list:
    """明细候选行：表头（含次表头）之后到 TOTAL 行前、料号非空的行。"""
    first_row = (resolution.subheader_row or resolution.header_row) + 1
    rows = []
    for r in range(first_row, min(ws.max_row, first_row + 60) + 1):
        a = ws.cell(r, 1).value
        if isinstance(a, str) and a.strip().upper().startswith("TOTAL"):
            break
        if ws.cell(r, resolution.columns["part_code"]).value in (None, ""):
            continue
        rows.append(r)
    return rows


probe_issues: list = []  # 候选校验专用：问题由正式解析阶段重新登记


def select_final_sheet(wb_values, filename: str, issues: list, aliases=None):
    """返回 (最终工作表, aliases) 或 (None, aliases)（问题已登记）。"""
    aliases = aliases or merged_aliases(None)
    probe_issues.clear()
    finals = [name for name in wb_values.sheetnames if FINAL_SHEET_RE.match(name)]
    base = [name for name in wb_values.sheetnames if BASE_SHEET_RE.match(name)]
    valid_finals = [n for n in finals if _sheet_is_final_candidate(wb_values[n], aliases)]
    if len(finals) == 1 and not valid_finals:
        # 唯一最终表：只要表头/补充列结构成立即采用，空表由正式解析报 NO_RECORDS；
        # 结构不合格仍按 FINAL_SHEET_INVALID 暂停。
        only = finals[0]
        if _sheet_structure_valid(wb_values[only], aliases):
            return wb_values[only], aliases
    if len(valid_finals) == 1:
        return wb_values[valid_finals[0]], aliases
    if len(valid_finals) > 1:
        issues.append(Issue(
            code="AMBIGUOUS_FINAL_SHEET",
            message=f"存在多个合格的最终发票表：{valid_finals}，无法唯一选定",
            sources=[Source(type="workbook", filename=filename)],
            required_action="请确认应使用哪张最终调整表",
        ))
        return None, aliases
    if finals:  # 有名字但结构不合格
        detail = ""
        if len(finals) == 1:
            resolution = _resolve_invoice_columns(wb_values[finals[0]], aliases)
            reasons = []
            missing, ambiguous = resolution.missing, resolution.ambiguous
            if resolution.header_row == 0 and missing:
                # 无候选表头行：报告覆盖率最高一行的真实缺口，而非整份字段清单
                gap = _best_effort_gap(wb_values[finals[0]], aliases)
                missing, ambiguous = gap["missing"], gap["ambiguous"]
                if gap["row"]:
                    reasons.append(f"疑似表头第 {gap['row']} 行")
            if missing:
                reasons.append(f"缺失字段 {missing}")
            if ambiguous:
                reasons.append(f"多候选 {ambiguous}")
            if resolution.multiple_regions:
                reasons.append(f"多个表头区 行{resolution.multiple_regions}")
            detail = "（" + "；".join(reasons) + "）" if reasons else ""
        issues.append(Issue(
            code="FINAL_SHEET_INVALID",
            message=f"最终发票表 {finals} 结构校验未通过{detail}（表头/补充列/价格信息"
                    "不完整或存在多候选），不能自动替代或猜测",
            sources=[Source(type="workbook", filename=filename)],
            required_action="请补齐最终调整表的表头与 M/N/O 风格补充列后重发",
        ))
        return None, aliases
    if base:
        issues.append(Issue(
            code="FINAL_SHEET_NOT_FOUND",
            message="未找到含补充资料的最终发票表（如 CI-For Dealer (2)），原表不能自动替代",
            sources=[Source(type="workbook", filename=filename, sheet=base[0])],
            required_action="请提供含补充资料（K 列之后）的最终调整表",
        ))
        return None, aliases
    return None, aliases  # 调用方按“不含配件发票”处理


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


def _find_total_row(ws, header_row: int, value_cols):
    """语义 TOTAL 锚定（有界全有效区扫描）。"""
    return select_total_row(ws, header_row, value_cols)


def _parse_packing_totals(wb_values, filename: str, issues: list, aliases):
    """Packing List 表：按表头语义定位 PACKAGE 与 GROSS WEIGHT 列，TOTAL 行取值。"""
    name = next((n for n in wb_values.sheetnames
                 if normalize_header(n) == PACKING_NAME_KEY), None)
    if name is None:
        return None, None, None, None, {}
    ws = wb_values[name]
    resolution = resolve_columns(ws, PACKING_FIELDS, aliases,
                                 require=PACKING_REQUIRE)
    if resolution.missing or resolution.ambiguous or resolution.multiple_regions:
        issues.append(Issue(
            code="PACKING_TOTALS_INVALID",
            message=f"箱单 {name} 的 TOTAL 行件数/毛重列无法唯一定位："
                    f"缺失={resolution.missing} 多候选={resolution.ambiguous} "
                    f"多表区={resolution.multiple_regions}",
            sources=[Source(type="workbook", filename=filename, sheet=name)],
            required_action="请确认箱单表头包含唯一一份 PACKAGE 与 GROSS WEIGHT 列",
        ))
        return None, None, None, None, {}
    total_row, total_error = select_total_row(
        ws, resolution.header_row,
        [resolution.columns["packages"], resolution.columns["gross_weight"]])
    if total_error == "multiple":
        issues.append(Issue(
            code="MULTIPLE_TOTAL_ROWS",
            message=f"箱单 {name} 表头下方存在多张同属一个表块的合计行，无法确定 TOTAL 锚点",
            sources=[Source(type="workbook", filename=filename, sheet=name)],
            required_action="请确认本票唯一 TOTAL 行后重发",
        ))
        return None, None, None, None, {
            "sheet": name, "header_row": resolution.header_row,
            "columns": {k: column_letter(v) for k, v in resolution.columns.items()},
        }
    if total_row is None:
        return None, None, None, None, {
            "sheet": name, "header_row": resolution.header_row,
            "columns": {k: column_letter(v) for k, v in resolution.columns.items()},
        }
    gross = to_decimal(ws.cell(total_row, resolution.columns["gross_weight"]).value)
    pkgs = to_decimal(ws.cell(total_row, resolution.columns["packages"]).value)
    layout = {"sheet": name, "header_row": resolution.header_row,
              "total_row": total_row,
              "columns": {k: column_letter(v) for k, v in resolution.columns.items()}}
    return (gross, pkgs,
            Source(type="workbook", filename=filename, sheet=name,
                   cell=f"{column_letter(resolution.columns['gross_weight'])}{total_row}"),
            Source(type="workbook", filename=filename, sheet=name,
                   cell=f"{column_letter(resolution.columns['packages'])}{total_row}"),
            layout)


def parse_parts_workbook(path: str, filename: str, aliases=None) -> PartsParseResult:
    result = PartsParseResult()
    wb_values, wb_formula = load_workbook_pair(path)
    ws, aliases = select_final_sheet(wb_values, filename, result.issues, aliases)
    result.aliases = aliases

    (result.gross_weight, result.packages,
     result.gross_source, result.packages_source,
     result.layout["packing_total"]) = _parse_packing_totals(
        wb_values, filename, result.issues, aliases)

    if ws is None:
        return result

    ws_f = wb_formula[ws.title]
    result.invoice_no, result.invoice_source = _parse_invoice_no(ws, filename)

    resolution = _resolve_invoice_columns(ws, aliases)
    if resolution.missing or resolution.ambiguous or resolution.multiple_regions:
        result.issues.append(Issue(
            code="FINAL_SHEET_INVALID",
            message=f"最终发票表 {ws.title} 结构不完整：缺失字段 {resolution.missing}"
                    f"，多候选 {resolution.ambiguous}，多表区 {resolution.multiple_regions}"
                    "，不能猜测放行",
            sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
            required_action="请补齐表头或使用已确认的表格布局",
        ))
        return result
    supp = _supplementary_columns(ws, resolution)

    first_row = (resolution.subheader_row or resolution.header_row) + 1
    total_row, total_error = _find_total_row(
        ws, resolution.subheader_row or resolution.header_row,
        [resolution.columns["qty"], resolution.columns["net_weight"]])
    if total_error == "multiple":
        result.issues.append(Issue(
            code="MULTIPLE_TOTAL_ROWS",
            message=f"最终发票表 {ws.title} 表头下方存在多张同属一个表块的合计行，"
                    "无法确定明细范围与合计锚点",
            sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
            required_action="请确认本票唯一 TOTAL 行后重发",
        ))
        return result
    last_row = (total_row - 1) if total_row else ws.max_row
    if any(ws.cell(r, resolution.columns["part_code"]).value not in (None, "")
           for r in range(first_row, last_row + 1)) \
            and not _validate_supplementary(ws, resolution, supp, first_row, last_row,
                                            filename, result.issues):
        return result

    # 明细区同样有界：xls 虚高维度不会拖慢循环。
    from .headers import SCAN_ROW_LIMIT
    last_row = min(last_row, first_row + SCAN_ROW_LIMIT - 1)

    src = lambda col, r: Source(type="workbook", filename=filename, sheet=ws.title,
                                cell=f"{column_letter(col)}{r}")
    net_total_sum = Decimal("0")
    net_total_count = 0
    detail_last = first_row - 1
    for r in range(first_row, last_row + 1):
        part_code = ws.cell(r, resolution.columns["part_code"]).value
        if part_code in (None, ""):
            continue  # 显式费用行/说明行（无料号）不属于明细，跳过不告警
        qty = cell_value(ws, ws_f, r, resolution.columns["qty"], record_key=None,
                         field="qty", filename=filename, issues=result.issues)
        price = cell_value(ws, ws_f, r, resolution.columns["unit_price"], record_key=None,
                           field="unit_price", filename=filename, issues=result.issues)
        qty_d = to_decimal(qty)
        price_d = to_decimal(price)
        if qty_d is None or price_d is None:
            # 料号行是产品行：数量/单价缺失或不可解析必须阻塞暂停，
            # 禁止静默丢行（真实料件可能因此从输出消失）。
            bad_field = "qty" if qty_d is None else "unit_price"
            bad_col = resolution.columns[bad_field]
            result.issues.append(Issue(
                code="INVALID_DETAIL_ROW",
                record_key=str(part_code).strip(),
                field=bad_field,
                message=f"最终发票表 {ws.title} 明细行 {r} 料号 {str(part_code).strip()!r} "
                        f"的 {bad_field} 缺失或无法解析（"
                        f"{column_letter(bad_col)}{r}={ws.cell(r, bad_col).value!r}），"
                        "该行未纳入输出",
                sources=[src(bad_col, r), src(resolution.columns["part_code"], r)],
                required_action=f"请补齐料号 {str(part_code).strip()} 的 "
                                f"{'数量' if bad_field == 'qty' else '单价'} 后重发",
            ))
            continue
        part_code = str(part_code).strip()
        detail_last = r
        net = cell_value(ws, ws_f, r, resolution.columns["net_weight"], record_key=part_code,
                         field="net_weight", filename=filename, issues=result.issues)
        net_d = to_decimal(net)
        unit_w = to_decimal(ws.cell(r, resolution.columns["unit_weight"]).value)
        if net_d is not None and unit_w is not None and qty_d is not None:
            ok = decimals_close(qty_d * unit_w, net_d, max_dp=4)
            result.layout["checks"].append({
                "rule": RULE_PARTS_WEIGHT_ROW, "record_key": part_code,
                "result": "pass" if ok else "fail",
                "detail": f"数量{qty_d}×单件重{unit_w}={qty_d * unit_w} vs 总净重{net_d}",
            })
            if not ok:
                result.issues.append(Issue(
                    code="WEIGHT_MISMATCH",
                    record_key=part_code,
                    field="net_weight",
                    message=f"净重核对失败：{column_letter(resolution.columns['net_weight'])}{r}={net_d} "
                            f"与 数量×单件重={qty_d}×{unit_w} 不一致",
                    sources=[src(resolution.columns["net_weight"], r),
                             src(resolution.columns["qty"], r),
                             src(resolution.columns["unit_weight"], r)],
                    required_action="请核对最终表数量、单件重量与总净重",
                ))
        if net_d is not None:
            net_total_sum += net_d
            net_total_count += 1
        hs_titled = ws.cell(r, resolution.columns.get("hs_code_titled", 0)).value \
            if "hs_code_titled" in resolution.columns else None
        hs_m = ws.cell(r, supp["hs_code"]).value
        hs_titled_present = hs_titled not in (None, "")
        hs_m_present = hs_m not in (None, "")
        if hs_titled_present and hs_m_present:
            if str(hs_titled).strip() != str(hs_m).strip():
                agree = False
                result.issues.append(Issue(
                    code="HS_CODE_DVERSUS_M",
                    record_key=part_code,
                    field="hs_code",
                    message=f"{column_letter(resolution.columns['hs_code_titled'])} 列 {hs_titled} 与 "
                            f"{column_letter(supp['hs_code'])} 列 {hs_m} 不一致，按已确认规则以补充列为准",
                    sources=[src(resolution.columns["hs_code_titled"], r), src(supp["hs_code"], r)],
                    required_action="无需操作；如需以表头 HS Code 列为准请明确回复",
                    blocking=False,
                ))
            else:
                agree = True
            result.layout["checks"].append({
                "rule": RULE_PARTS_HS_AGREE, "record_key": part_code,
                "result": "pass" if agree else "fail",
                "detail": f"表头HS列={hs_titled!r} 补充列={hs_m!r}",
            })
        elif hs_titled_present != hs_m_present:
            # 只有一侧有值：比对未执行，绝不登记“通过”。
            missing_side = "表头HS列" if not hs_titled_present else "补充编码列"
            result.layout["checks"].append({
                "rule": RULE_PARTS_HS_AGREE, "record_key": part_code,
                "result": "skipped",
                "detail": f"{missing_side}为空，HS 比对未执行",
            })
        # 两侧皆空：无可比对内容，不登记该行的 HS 核对。
        rec = PartRecord(part_code=part_code, row_index=r)
        coo_col = resolution.columns.get("coo")
        coo_raw = ws.cell(r, coo_col).value if coo_col else None
        rec.fields = {
            "part_code": FieldValue(part_code, part_code, src(resolution.columns["part_code"], r)),
            "qty": FieldValue(qty, qty_d, src(resolution.columns["qty"], r)),
            "unit_price": FieldValue(price, price_d, src(resolution.columns["unit_price"], r)),
            "net_weight": FieldValue(net, net_d, src(resolution.columns["net_weight"], r)),
            "unit_weight": FieldValue(ws.cell(r, resolution.columns["unit_weight"]).value,
                                      unit_w, src(resolution.columns["unit_weight"], r)),
            "coo": FieldValue(coo_raw,
                              str(coo_raw).strip() if coo_raw else None,
                              src(coo_col or 0, r)),
            "hs_code": FieldValue(hs_m, str(hs_m).strip() if hs_m is not None else None,
                                  src(supp["hs_code"], r)),
            "product_name": FieldValue(ws.cell(r, supp["product_name"]).value,
                                       ws.cell(r, supp["product_name"]).value,
                                       src(supp["product_name"], r)),
            "spec": FieldValue(ws.cell(r, supp["spec"]).value,
                               ws.cell(r, supp["spec"]).value, src(supp["spec"], r)),
        }
        result.records.append(rec)

    if total_row is not None and net_total_count:
        total_net = to_decimal(ws.cell(total_row, resolution.columns["net_weight"]).value)
        if total_net is not None:
            ok = decimals_close(net_total_sum, total_net, max_dp=4)
            result.layout["checks"].append({
                "rule": RULE_PARTS_NET_TOTAL, "record_key": None,
                "result": "pass" if ok else "fail",
                "detail": f"明细净重合计{net_total_sum} vs TOTAL行{total_net}",
            })
            if not ok:
                result.issues.append(Issue(
                    code="WEIGHT_TOTAL_MISMATCH",
                    message=f"明细净重合计 {net_total_sum} 与 TOTAL 行 "
                            f"{column_letter(resolution.columns['net_weight'])}{total_row}={total_net} 不一致",
                    sources=[src(resolution.columns["net_weight"], total_row)],
                    required_action="请核对最终表明细净重与合计",
                ))
    if not result.records:
        result.issues.append(Issue(
            code="NO_RECORDS",
            message=f"最终发票表 {ws.title} 未解析到任何有效料号明细行",
            sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
            required_action="请确认附件包含有效明细",
        ))
    result.layout["parts_invoice"] = {
        "sheet": ws.title,
        "header_row": resolution.header_row,
        "subheader_row": resolution.subheader_row,
        "columns": {name: column_letter(col) for name, col in sorted(resolution.columns.items())},
        "supplementary": {name: column_letter(col) for name, col in supp.items()},
        "data_first_row": first_row,
        "data_last_row": detail_last,
    }
    return result
