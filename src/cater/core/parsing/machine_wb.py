"""整机发票/箱单工作簿解析（多结构与 xls 版本）。

规则（plan.md §5 + plan/tasks/claude-flexible-input.md 阶段 2/3）：
- sheet 名辅助、内容优先：业务表别名 invoice↔CI、packing list↔PL；
  名称命中的业务表直接采用，名称未命中时按结构（机器信息表/重量表）
  唯一候选回退，多候选或不完整必须暂停，不猜测；
- 机器信息表语义定位：MODEL/MSO#（SERIAL # 与 LOADER # 同义为序列号），
  允许第 56 行等深表头、表/列/行移动与双行表头；
- 重量表语义定位：DESCRIPTION/QTY/NET/NET WEIGHT (KGS)/GROSS，
  支持 "机型--MSO/MSO" 组名与 "CMOPS MSO#: X/Y" 描述两种 MSO 清单形式；
  数量与组内 MSO 数必须一致；组 MSO 必须能在机器信息表中对上；
- CI 文字 NET WEIGHT 与箱单净重合计交叉核对；箱单 TOTAL 行交叉核对；
- 历史辅助 sheet（BL/Sheet1/Sheet2/MACHINE DISASSEMBLY DETAILS 等）
  不参与解析；费用行（INLAND/SEA/INSURANCE）不在业务表内，自然排除；
- 发票单价（U'PRICE/UNIT PRICE）与 Total Sales Price 是不同业务字段：
  本模块不读发票单价，机器价格只来自邮件业务表。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ..models import Issue, Source, WeightGroup
from ..numbertext import decimals_close, to_decimal
from .headers import (column_letter, merged_aliases, normalize_header,
                      resolve_columns, select_total_row)
from .workbook import load_workbook_pair

# 规则 ID（日志/结果/回复正文引用）
RULE_GROUP_QTY = "MACHINE_WEIGHT_GROUP_QTY"
RULE_TOTAL_ROW = "MACHINE_PACKING_TOTAL_ROW"
RULE_CI_PL_NET = "MACHINE_CI_TEXT_VS_PL_NET"

INVOICE_NAME_KEYS = {"invoice", "ci", "commercialinvoice"}
PACKING_NAME_KEYS = {"packinglist", "pl", "packing"}

MACHINE_FIELDS = ["model", "mso", "serial", "pin", "engine"]
MACHINE_REQUIRE = ["model", "mso"]
WEIGHT_FIELDS = ["description", "qty", "net", "gross"]
WEIGHT_REQUIRE = ["description", "qty", "net"]

GROUP_LABEL_RE = re.compile(r"^(?P<model>[A-Za-z0-9.]+).*?--(?P<msos>[A-Za-z0-9/]+)\s*$")
MSO_LIST_RE = re.compile(
    r"MSO#?\s*[:：]\s*(?P<msos>[A-Za-z0-9]+(?:\s*/\s*[A-Za-z0-9]+)*)", re.IGNORECASE)
QTY_RE = re.compile(r"(\d+)")
INVOICE_NO_RE = re.compile(r"INVOICE\s+NO\.?\s*:?\s*([\w\-/]+)", re.IGNORECASE)
NET_WEIGHT_TEXT_RE = re.compile(r"NET\s*WEIGHT\s*[:：]?\s*([0-9][0-9,\.]*)", re.IGNORECASE)


@dataclass
class MachineRow:
    mso: str
    model: Optional[str]
    loader: Optional[str]
    pin: Optional[str]
    engine: Optional[str]
    source: Source


@dataclass
class MachineWorkbookResult:
    invoice_rows: list = field(default_factory=list)   # list[MachineRow]
    packing_rows: list = field(default_factory=list)   # list[MachineRow]
    weight_groups: list = field(default_factory=list)  # list[WeightGroup]
    invoice_no: Optional[str] = None
    invoice_source: Optional[Source] = None
    layout: dict = field(default_factory=lambda: {"checks": [], "machine_table": [],
                                                  "weight_table": []})
    issues: list = field(default_factory=list)


def _is_named(sheet_name: str, keys: set) -> bool:
    return normalize_header(sheet_name) in keys


def _find_machine_table(ws, aliases):
    """定位机器信息表；返回 (resolution, error)。"""
    resolution = resolve_columns(ws, MACHINE_FIELDS, aliases,
                                 require=MACHINE_REQUIRE)
    if resolution.multiple_regions:
        return resolution, "ambiguous"
    if resolution.header_row == 0:
        return resolution, "no_machine_table"
    if resolution.ambiguous:
        return resolution, "ambiguous"
    if resolution.missing:
        return resolution, "incomplete"
    return resolution, None


def _machine_data_end(ws, aliases, cols, start_row: int) -> tuple:
    """机器信息表数据区终止行：(boundary_row, error)。

    - 首个完全空行（含纯空白串）即表块终止——业务表是连续块，其后内容
      （备注、签名区、第二表块）不属于本表；
    - 已定位列上落回表头别名文本（如数据位出现 "MSO NO."/"MODEL"）说明
      紧邻着又一张表块（其表头不构成完整候选，未被候选枚举拦下），返回
      "second_header"——不猜测明细范围，调用方暂停。
    """
    alias_keys = {name: set(aliases.get(name, ())) for name in cols}
    for r in range(start_row, ws.max_row + 1):
        blank = True
        for c in range(1, ws.max_column + 1):
            value = ws.cell(r, c).value
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            blank = False
            break
        if blank:
            return r, None
        for name, col in cols.items():
            value = ws.cell(r, col).value
            if (isinstance(value, str)
                    and normalize_header(value) in alias_keys[name]):
                return r, "second_header"
    return ws.max_row + 1, None


def _parse_machine_table(ws, filename: str, aliases) -> tuple:
    """解析一张 sheet 内的机器信息表；返回 (rows, resolution, error)。"""
    resolution, error = _find_machine_table(ws, aliases)
    if error:
        return [], resolution, error
    cols = resolution.columns
    last_row, boundary_error = _machine_data_end(
        ws, aliases, cols, resolution.header_row + 1)
    rows = []
    serial_col = cols.get("serial")
    for r in range(resolution.header_row + 1, last_row):
        mso = ws.cell(r, cols["mso"]).value
        if mso in (None, ""):
            continue
        mso = str(mso).strip()
        rows.append(MachineRow(
            mso=mso,
            model=str(ws.cell(r, cols["model"]).value).strip()
                  if ws.cell(r, cols["model"]).value else None,
            loader=str(ws.cell(r, serial_col).value).strip()
                   if serial_col and ws.cell(r, serial_col).value else None,
            pin=str(ws.cell(r, cols["pin"]).value).strip()
                if cols.get("pin") and ws.cell(r, cols["pin"]).value else None,
            engine=str(ws.cell(r, cols["engine"]).value).strip()
                   if cols.get("engine") and ws.cell(r, cols["engine"]).value else None,
            source=Source(type="workbook", filename=filename, sheet=ws.title,
                          cell=f"{column_letter(cols['mso'])}{r}", row=r),
        ))
    if not rows:
        return [], resolution, "empty"
    if boundary_error == "second_header":
        # 紧邻第二张表块：明细范围不可猜测，整表作废并暂停。
        return [], resolution, "second_header"
    last = max(r.source.row for r in rows)
    layout = {
        "sheet": ws.title,
        "header_row": resolution.header_row,
        "columns": {name: column_letter(col) for name, col in sorted(cols.items())},
        "data_first_row": resolution.header_row + 1,
        "data_last_row": last,
    }
    return rows, layout, None


def _extract_group_msos(label: str) -> Optional[list]:
    text = " ".join(str(label).split())
    m = MSO_LIST_RE.search(text)
    if not m:
        m = GROUP_LABEL_RE.match(text)
    if not m:
        return None
    return [x.strip() for x in m.group("msos").split("/") if x.strip()]


def _find_weight_total_row(ws, description_col, header_row, cols):
    """语义 TOTAL 锚定（多候选同块返回 "multiple"）。"""
    row, error = select_total_row(
        ws, header_row,
        [cols.get("qty"), cols.get("net"), cols.get("gross")],
        anchor_cols=(1, description_col))
    return row, error


def _parse_weight_table(ws, filename: str, aliases, machine_msos: set) -> tuple:
    """解析一张 sheet 内的箱单重量表；返回 (groups, layout, total_info, error)。"""
    resolution = resolve_columns(ws, WEIGHT_FIELDS, aliases,
                                 require=WEIGHT_REQUIRE)
    if resolution.multiple_regions:
        return [], {}, None, "ambiguous"
    if resolution.header_row == 0:
        return [], {}, None, "no_weight_table"
    if resolution.ambiguous:
        return [], {}, None, "ambiguous"
    if resolution.missing:
        return [], {}, None, "incomplete"
    cols = resolution.columns
    first_row = (resolution.subheader_row or resolution.header_row) + 1
    total_row, total_error = _find_weight_total_row(
        ws, cols["description"], resolution.subheader_row or resolution.header_row, cols)
    if total_error == "multiple":
        return [], {}, None, "multiple_totals"
    last_row = (total_row - 1) if total_row else ws.max_row

    def num(row, name):
        return to_decimal(ws.cell(row, cols[name]).value)

    groups = []
    for r in range(first_row, last_row + 1):
        label = ws.cell(r, cols["description"]).value
        if label in (None, ""):
            continue
        msos = _extract_group_msos(label)
        if not msos:
            continue  # 非重量组行（费用/说明行）不猜测
        qty_raw = ws.cell(r, cols["qty"]).value
        qty_m = QTY_RE.search(str(qty_raw)) if qty_raw is not None else None
        qty = int(qty_m.group(1)) if qty_m else None
        net = num(r, "net")
        gross = num(r, "gross") if "gross" in cols else None
        groups.append((r, msos, qty, net, gross, label))

    total_info = None
    if total_row is not None:
        qty_raw = ws.cell(total_row, cols["qty"]).value
        qty_m = QTY_RE.search(str(qty_raw)) if qty_raw is not None else None
        total_info = {
            "row": total_row,
            "qty": int(qty_m.group(1)) if qty_m else None,
            "net": num(total_row, "net"),
            "gross": num(total_row, "gross") if "gross" in cols else None,
        }
    layout = {
        "sheet": ws.title,
        "header_row": resolution.header_row,
        "subheader_row": resolution.subheader_row,
        "columns": {name: column_letter(col) for name, col in sorted(cols.items())},
        "total_row": total_row,
    }
    return groups, layout, total_info, None


def _parse_invoice_no(ws, filename: str):
    for r in range(1, min(ws.max_row, 16) + 1):
        for c in range(1, min(ws.max_column, 16) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "INVOICE" in v.upper():
                m = INVOICE_NO_RE.search(v.replace("\n", " "))
                if m:
                    return m.group(1).strip(), Source(
                        type="workbook", filename=filename, sheet=ws.title,
                        cell=ws.cell(r, c).coordinate)
    return None, None


def _text_net_weight(ws) -> Optional[Decimal]:
    for r in range(1, ws.max_row + 1):
        for c in range(1, min(ws.max_column, 20) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "NET" in v.upper() and "WEIGHT" in v.upper():
                m = NET_WEIGHT_TEXT_RE.search(v)
                if m:
                    return to_decimal(m.group(1).replace(",", ""))
    return None


def sniff_machine_workbook(path: str, aliases=None) -> bool:
    """结构探测：工作簿内是否存在可解析的机器信息表或箱单重量表。

    与 sheet 名无关（已确认的布局灵活性：业务表可重命名）。用于附件
    挑选/分类的名称未命中回退；探测不解析明细、不产生副作用。
    """
    try:
        wb_values, _wb_formula = load_workbook_pair(path)
    except Exception:
        return False
    aliases = aliases or merged_aliases(None)
    for name in wb_values.sheetnames:
        ws = wb_values[name]
        _resolution, error = _find_machine_table(ws, aliases)
        if error is None:
            return True
        weight = resolve_columns(ws, WEIGHT_FIELDS, aliases,
                                 require=WEIGHT_REQUIRE)
        if (weight.header_row and not weight.missing
                and not weight.ambiguous and not weight.multiple_regions):
            return True
    return False


def parse_machine_workbook(path: str, filename: str, aliases=None) -> MachineWorkbookResult:
    result = MachineWorkbookResult()
    aliases = aliases or merged_aliases(None)
    wb_values, _wb_formula = load_workbook_pair(path)
    names = list(wb_values.sheetnames)
    named_invoice = [n for n in names if _is_named(n, INVOICE_NAME_KEYS)]
    named_packing = [n for n in names if _is_named(n, PACKING_NAME_KEYS)]

    # ---- 机器信息表：名称辅助优先，内容唯一候选回退 ----------------------
    business_named = set(named_invoice) | set(named_packing)
    machine_tables = []
    for name in names:
        if name not in business_named:
            continue  # 历史辅助 sheet 仅在无名称命中时按内容回退
        ws = wb_values[name]
        rows, layout, error = _parse_machine_table(ws, filename, aliases)
        if error is None:
            machine_tables.append((name, rows, layout))
        elif error == "second_header":
            result.issues.append(Issue(
                code="MACHINE_TABLE_INVALID",
                message=f"业务表 {name} 的机器信息表数据区出现重复表头行，"
                        "紧邻多张表块，无法唯一确定明细范围，不能猜测放行",
                sources=[Source(type="workbook", filename=filename, sheet=name)],
                required_action="请确认机器信息表只保留一张表块，或空行分隔各表块",
            ))
        elif error in ("ambiguous", "incomplete"):
            # 业务表上存在但结构不可唯一解析的机器信息表必须暂停；
            # 无表/空表（如 PL 只有重量表）属于正常分工。
            result.issues.append(Issue(
                code="MACHINE_TABLE_INVALID",
                message=f"业务表 {name} 的机器信息表结构校验未通过（{error}），不能猜测放行",
                sources=[Source(type="workbook", filename=filename, sheet=name)],
                required_action="请确认机器信息表（MODEL/MSO# 等表头）完整且唯一",
            ))
    if not machine_tables:
        # 回退：全 sheet 按结构找唯一候选（历史多候选必须暂停）。
        fallback = []
        for name in names:
            if name in business_named:
                continue
            ws = wb_values[name]
            rows, layout, error = _parse_machine_table(ws, filename, aliases)
            if error is None:
                fallback.append((name, rows, layout))
        if len(fallback) > 1:
            result.issues.append(Issue(
                code="AMBIGUOUS_MACHINE_TABLE",
                message=f"多个工作表存在机器信息表：{[n for n, _, _ in fallback]}，无法唯一选定",
                sources=[Source(type="workbook", filename=filename)],
                required_action="请确认应使用哪张工作表",
            ))
        elif fallback:
            machine_tables.append(fallback[0])
    if not machine_tables:
        if result.issues:
            return result  # 机器表结构问题已登记，暂停
        return result  # 非整机附件，调用方决定

    result.invoice_rows = [row for _, rows, _ in machine_tables for row in rows]
    result.layout["machine_table"] = [layout for _, _, layout in machine_tables]
    for name, _, _ in machine_tables:
        if _is_named(name, INVOICE_NAME_KEYS):
            invoice_no, invoice_source = _parse_invoice_no(wb_values[name], filename)
            if invoice_no:
                result.invoice_no, result.invoice_source = invoice_no, invoice_source

    machine_msos = {r.mso for r in result.invoice_rows}

    # ---- 重量表：名称辅助优先，唯一候选回退 ------------------------------
    weight_sources = list(named_packing)
    if not weight_sources:
        fallback = []
        for name in names:
            if name in business_named or any(name == n for n, _, _ in machine_tables):
                continue
            ws = wb_values[name]
            _, layout, _, error = _parse_weight_table(ws, filename, aliases, set())
            if error is None:
                fallback.append(name)
        if len(fallback) > 1:
            result.issues.append(Issue(
                code="AMBIGUOUS_WEIGHT_TABLE",
                message=f"多个工作表存在箱单重量表：{fallback}，无法唯一选定",
                sources=[Source(type="workbook", filename=filename)],
                required_action="请确认应使用哪张工作表",
            ))
            weight_sources = []
        else:
            weight_sources = fallback

    packing_msos_rows = []
    total_net_sum = Decimal("0")
    total_qty_sum = 0
    ci_text_net = None
    for name in weight_sources:
        ws = wb_values[name]
        groups, layout, total_info, error = _parse_weight_table(
            ws, filename, aliases, machine_msos)
        if error is not None:
            if name in named_packing and error == "multiple_totals":
                result.issues.append(Issue(
                    code="MULTIPLE_TOTAL_ROWS",
                    message=f"箱单 {name} 表头下方存在多张同属一个表块的合计行，"
                            "无法确定 TOTAL 锚点",
                    sources=[Source(type="workbook", filename=filename, sheet=name)],
                    required_action="请确认本票唯一 TOTAL 行后重发",
                ))
            elif name in named_packing and error in ("ambiguous", "incomplete"):
                result.issues.append(Issue(
                    code="WEIGHT_TABLE_INVALID",
                    message=f"箱单 {name} 的重量表结构校验未通过（{error}），不能猜测放行",
                    sources=[Source(type="workbook", filename=filename, sheet=name)],
                    required_action="请确认重量表（QTY/NET WEIGHT 等表头）完整且唯一",
                ))
            continue
        result.layout["weight_table"].append(layout)
        rows, layout2, _ = _parse_machine_table(ws, filename, aliases)
        if layout2:
            packing_msos_rows.extend(rows)
        for r, msos, qty, net, gross, label in groups:
            if qty is None or net is None:
                result.issues.append(Issue(
                    code="WEIGHT_GROUP_INCOMPLETE",
                    message=f"箱单重量组缺少数量或净重：{label!r}",
                    sources=[Source(type="workbook", filename=filename, sheet=name, cell=f"B{r}")],
                    required_action="请补齐箱单组的数量与净重",
                ))
                continue
            ok = qty == len(msos)
            result.layout["checks"].append({
                "rule": RULE_GROUP_QTY, "record_key": "/".join(msos),
                "result": "pass" if ok else "fail",
                "detail": f"组MSO数{len(msos)} vs 数量{qty}",
            })
            if not ok:
                result.issues.append(Issue(
                    code="WEIGHT_GROUP_QTY_MISMATCH",
                    message=f"箱单组 {label!r} 数量 {qty} 与组内 MSO 数 {len(msos)} 不一致",
                    sources=[Source(type="workbook", filename=filename, sheet=name, cell=f"B{r}")],
                    required_action="请核对箱单组数量与 MSO 清单",
                ))
            unknown = [m for m in msos if m not in machine_msos]
            if unknown:
                result.issues.append(Issue(
                    code="WEIGHT_GROUP_MSO_UNKNOWN",
                    message=f"箱单组 {label!r} 的 MSO {unknown} 在机器信息表中不存在，无法关联",
                    sources=[Source(type="workbook", filename=filename, sheet=name, cell=f"B{r}")],
                    required_action="请核对箱单组 MSO 与机器信息表一致性",
                ))
            result.weight_groups.append(WeightGroup(
                msos=msos, qty=qty, net_weight=net, gross_weight=gross,
                source=Source(type="workbook", filename=filename, sheet=name,
                              cell=f"B{r}", row=r),
            ))
            total_net_sum += net
            total_qty_sum += qty
        if total_info is not None and result.weight_groups:
            if total_info["qty"] is None and total_info["net"] is None:
                # TOTAL 行数量/净重均空白：无可比对的一侧，绝不冒称通过。
                result.layout["checks"].append({
                    "rule": RULE_TOTAL_ROW, "record_key": None,
                    "result": "skipped",
                    "detail": "TOTAL 行数量/净重为空，合计核对未执行",
                })
                continue
            net_ok = (total_info["net"] is None
                      or decimals_close(total_net_sum, total_info["net"], max_dp=4))
            qty_ok = (total_info["qty"] is None
                      or total_qty_sum == total_info["qty"])
            result.layout["checks"].append({
                "rule": RULE_TOTAL_ROW, "record_key": None,
                "result": "pass" if (net_ok and qty_ok) else "fail",
                "detail": f"组合计 净重{total_net_sum}/数量{total_qty_sum} vs "
                          f"TOTAL行 净重{total_info['net']}/数量{total_info['qty']}",
            })
            if not (net_ok and qty_ok):
                result.issues.append(Issue(
                    code="WEIGHT_TOTAL_MISMATCH",
                    message=f"箱单明细组合计（净重{total_net_sum} 数量{total_qty_sum}）与 TOTAL 行"
                            f"（净重{total_info['net']} 数量{total_info['qty']}）不一致",
                    sources=[Source(type="workbook", filename=filename, sheet=name,
                                    cell=f"B{total_info['row']}")],
                    required_action="请核对箱单明细与 TOTAL 行",
                ))
    result.packing_rows = packing_msos_rows

    # ---- CI 文字总净重交叉核对 ------------------------------------------
    for name in named_invoice:
        if any(name == layout["sheet"] for layout in result.layout["machine_table"]):
            ci_text_net = _text_net_weight(wb_values[name])
            if ci_text_net is not None:
                break
    if ci_text_net is not None and result.weight_groups:
        ok = decimals_close(total_net_sum, ci_text_net, max_dp=4)
        result.layout["checks"].append({
            "rule": RULE_CI_PL_NET, "record_key": None,
            "result": "pass" if ok else "fail",
            "detail": f"CI文字NET WEIGHT {ci_text_net} vs 箱单净重合计 {total_net_sum}",
        })
        if not ok:
            result.issues.append(Issue(
                code="WEIGHT_MISMATCH",
                message=f"CI 文字总净重 {ci_text_net} 与箱单净重合计 {total_net_sum} 不一致",
                sources=[Source(type="workbook", filename=filename)],
                required_action="请核对 CI 与箱单净重",
            ))
    return result
