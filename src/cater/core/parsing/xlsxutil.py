"""xlsx 安全读取工具。

- data_only=True 读取公式缓存；同时以 data_only=False 复核：
  需要的单元格若是公式且缓存缺失（None），报 FORMULA_CACHE_MISSING，不能把缺失当零
  （plan.md §6/§11：不假设读取库能计算 Excel 公式）。
- 仅支持 .xlsx；xls/加密/损坏给出明确的不支持提示。
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Optional

import openpyxl

from ..models import Issue, Source


class UnsupportedSourceError(Exception):
    """无法作为 xlsx 解析（格式不支持、损坏或加密）。"""


def load_workbook_pair(path: str):
    """返回 (values_wb, formula_wb)。不支持的格式抛 UnsupportedSourceError。"""
    p = Path(path)
    if p.suffix.lower() != ".xlsx":
        raise UnsupportedSourceError(f"不支持的附件格式：{p.name}（仅支持 .xlsx，请补充 xlsx 版本）")
    try:
        if not zipfile.is_zipfile(p):
            raise UnsupportedSourceError(f"附件不是有效的 xlsx 文件：{p.name}（可能损坏或为旧版 xls）")
        values_wb = openpyxl.load_workbook(p, data_only=True)
        formula_wb = openpyxl.load_workbook(p, data_only=False)
    except UnsupportedSourceError:
        raise
    except Exception as exc:  # openpyxl 各类解析异常
        raise UnsupportedSourceError(f"无法解析附件 {p.name}：{exc}") from exc
    return values_wb, formula_wb


def cell_value(ws_values, ws_formula, row: int, col: int, *,
               record_key: Optional[str], field: str,
               filename: str, issues: list):
    """读取单元格值；若是公式且缓存缺失，登记问题并返回 None。

    公式有缓存时返回缓存值（不自行计算公式）。
    """
    v = ws_values.cell(row, col).value
    f = ws_formula.cell(row, col).value
    coord = ws_values.cell(row, col).coordinate
    if isinstance(f, str) and f.startswith("="):
        if v is None:
            issues.append(Issue(
                code="FORMULA_CACHE_MISSING",
                record_key=record_key,
                field=field,
                message=f"单元格 {ws_values.title}!{coord} 是公式但缺少缓存值，不能当作零处理",
                sources=[Source(type="workbook", filename=filename, sheet=ws_values.title, cell=coord)],
                required_action="请在 Excel 中重新计算保存后重发附件",
            ))
            return None
    return v


def find_header_row(ws, required: dict, max_scan: int = 40) -> Optional[int]:
    """在前 max_scan 行中寻找表头行：required = {列号: 表头关键词}，全部包含即命中。"""
    for r in range(1, min(ws.max_row, max_scan) + 1):
        ok = True
        for col, keyword in required.items():
            v = ws.cell(r, col).value
            if v is None or keyword.lower() not in str(v).replace("\xa0", " ").strip().lower():
                ok = False
                break
        if ok:
            return r
    return None
