"""整机发票/箱单工作簿解析。

- Invoice / Packing list 中的机器信息表（ITEM#|MODEL|MSO#|LOADER #|PIN#|ENGINE #）；
- Packing list 重量组：``SEM512 SOIL COMPACTOR--BFWVK/BFWVN`` 形式的组名，
  数量与组内 MSO 数量必须一致；第二比例=单颗料净重=组净重÷数量；
- 发票单价（U'PRICE）与 Total Sales Price 是不同业务字段，互不覆盖（plan.md §5）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ..models import Issue, Source, WeightGroup
from ..numbertext import to_decimal
from .xlsxutil import find_header_row, load_workbook_pair

MACHINE_HEADER = {2: "model", 3: "mso"}
GROUP_LABEL_RE = re.compile(r"^(?P<model>[A-Za-z0-9.]+).*?--(?P<msos>[A-Za-z0-9/]+)\s*$")
QTY_RE = re.compile(r"(\d+)")
INVOICE_NO_RE = re.compile(r"INVOICE\s+NO\.?\s*:?\s*([\w\-/]+)", re.IGNORECASE)


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
    issues: list = field(default_factory=list)


def _find_sheet(wb, *candidates):
    for want in candidates:
        for name in wb.sheetnames:
            if name.strip().lower() == want:
                return wb[name]
    return None


def _parse_machine_table(ws, filename: str) -> list:
    header_row = find_header_row(ws, MACHINE_HEADER)
    if header_row is None:
        return []
    # 列定位：按表头文本
    cols = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(header_row, c).value
        if v is None:
            continue
        key = str(v).replace("\xa0", " ").strip().upper()
        if key == "MODEL":
            cols["model"] = c
        elif key.startswith("MSO"):
            cols["mso"] = c
        elif key.startswith("LOADER"):
            cols["loader"] = c
        elif key.startswith("PIN"):
            cols["pin"] = c
        elif key.startswith("ENGINE"):
            cols["engine"] = c
    rows = []
    for r in range(header_row + 1, ws.max_row + 1):
        mso = ws.cell(r, cols["mso"]).value if "mso" in cols else None
        if mso in (None, ""):
            continue
        mso = str(mso).strip()
        rows.append(MachineRow(
            mso=mso,
            model=str(ws.cell(r, cols["model"]).value).strip() if cols.get("model") and ws.cell(r, cols["model"]).value else None,
            loader=str(ws.cell(r, cols["loader"]).value).strip() if cols.get("loader") and ws.cell(r, cols["loader"]).value else None,
            pin=str(ws.cell(r, cols["pin"]).value).strip() if cols.get("pin") and ws.cell(r, cols["pin"]).value else None,
            engine=str(ws.cell(r, cols["engine"]).value).strip() if cols.get("engine") and ws.cell(r, cols["engine"]).value else None,
            source=Source(type="workbook", filename=filename, sheet=ws.title,
                          cell=f"{chr(64 + cols['mso'])}{r}" if cols.get("mso", 0) <= 26 else None,
                          row=r),
        ))
    return rows


def _parse_weight_groups(ws, filename: str, issues: list) -> list:
    header_row = find_header_row(ws, {1: "marks", 2: "part name"})
    if header_row is None:
        return []
    groups = []
    for r in range(header_row + 1, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if isinstance(a, str) and a.strip().upper() == "TOTAL":
            break
        label = ws.cell(r, 2).value
        if not isinstance(label, str) or "--" not in label:
            continue
        m = GROUP_LABEL_RE.match(" ".join(label.split()))
        if not m:
            issues.append(Issue(
                code="WEIGHT_GROUP_UNPARSED",
                message=f"箱单重量组名称无法识别：{label!r}",
                sources=[Source(type="workbook", filename=filename, sheet=ws.title, cell=f"B{r}")],
                required_action="请确认箱单组名包含 机型--MSO列表 结构",
            ))
            continue
        msos = [x.strip() for x in m.group("msos").split("/") if x.strip()]
        qty_raw = ws.cell(r, 4).value
        qty_m = QTY_RE.search(str(qty_raw)) if qty_raw is not None else None
        qty = int(qty_m.group(1)) if qty_m else None
        net = to_decimal(ws.cell(r, 5).value)
        gross = to_decimal(ws.cell(r, 6).value)
        if qty is None or net is None:
            issues.append(Issue(
                code="WEIGHT_GROUP_INCOMPLETE",
                message=f"箱单重量组缺少数量或净重：{label!r}",
                sources=[Source(type="workbook", filename=filename, sheet=ws.title, cell=f"B{r}")],
                required_action="请补齐箱单组的数量与净重",
            ))
            continue
        if qty != len(msos):
            issues.append(Issue(
                code="WEIGHT_GROUP_QTY_MISMATCH",
                message=f"箱单组 {label!r} 数量 {qty} 与组内 MSO 数 {len(msos)} 不一致",
                sources=[Source(type="workbook", filename=filename, sheet=ws.title, cell=f"B{r}")],
                required_action="请核对箱单组数量与 MSO 清单",
            ))
        groups.append(WeightGroup(
            msos=msos, qty=qty, net_weight=net, gross_weight=gross,
            source=Source(type="workbook", filename=filename, sheet=ws.title, cell=f"B{r}", row=r),
        ))
    return groups


def _parse_invoice_no(ws, filename: str):
    for r in range(1, min(ws.max_row, 10) + 1):
        for c in range(1, min(ws.max_column, 12) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "INVOICE" in v.upper():
                m = INVOICE_NO_RE.search(v.replace("\n", " "))
                if m:
                    return m.group(1).strip(), Source(
                        type="workbook", filename=filename, sheet=ws.title,
                        cell=ws.cell(r, c).coordinate)
    return None, None


def parse_machine_workbook(path: str, filename: str) -> MachineWorkbookResult:
    result = MachineWorkbookResult()
    wb_values, _wb_formula = load_workbook_pair(path)
    invoice_ws = _find_sheet(wb_values, "invoice")
    packing_ws = _find_sheet(wb_values, "packing list")
    if invoice_ws is None and packing_ws is None:
        return result  # 非整机附件，调用方决定
    if invoice_ws is not None:
        result.invoice_rows = _parse_machine_table(invoice_ws, filename)
        result.invoice_no, result.invoice_source = _parse_invoice_no(invoice_ws, filename)
    if packing_ws is not None:
        result.packing_rows = _parse_machine_table(packing_ws, filename)
        result.weight_groups = _parse_weight_groups(packing_ws, filename, result.issues)
    return result
