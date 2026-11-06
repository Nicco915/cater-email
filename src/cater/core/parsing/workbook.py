"""统一只读工作簿接口：同时支持 OOXML xlsx 与真正 BIFF xls。

设计要点（plan/tasks/claude-flexible-input.md 阶段 1）：
- 两个格式都经 ``load_workbook_pair`` 返回 ``(values_wb, formula_wb)``，
  workbook/sheet/cell 暴露统一的最小只读接口：
  ``wb.sheetnames``、``wb[name]`` → ``ws.title`` / ``ws.max_row`` /
  ``ws.max_column`` / ``ws.cell(row, col)``（1 基）→ ``cell.value`` /
  ``cell.coordinate``；
- xlsx 用 openpyxl：``values`` 读公式缓存，``formula`` 复核公式存在性；
  并对 zip 解压设限（成员数与解压总大小），防止 zip 炸弹；
- xls 用 xlrd（纯 Python、支持 Windows、无 Excel/LibreOffice 外部进程依赖）。
  xlrd 2.x 不暴露公式信息：xls 的 ``formula_wb`` 即 values 视图，
  公式缓存缺失检查只对 xlsx 生效（BIFF 缓存值即文件内存储值）；
- 加密、损坏或超限文件给出明确错误，不猜测解析。
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Optional

import openpyxl

from ..models import Issue, Source

# 解压安全上限：成员数与解压后总字节（zip 炸弹防护）。
MAX_ZIP_MEMBERS = 2000
MAX_ZIP_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
# 单个 sheet 的单元格上限（xls  Ole 解析的内存保护）。
MAX_SHEET_CELLS = 3_000_000

SUPPORTED_SUFFIXES = (".xlsx", ".xls")


class UnsupportedSourceError(Exception):
    """无法作为工作簿解析（格式不支持、损坏、加密或超出安全限制）。"""


class WorkbookDependencyError(Exception):
    """运行环境缺少解析依赖（如 xlrd）：这是部署/运行时问题，不是输入缺失。

    与 UnsupportedSourceError 区分：附件本身已收到且可能完全有效，
    错误动作应指向维护人员安装依赖，而不是让发件人重发附件。
    """


# ----------------------------------------------------------------------
# xls (BIFF) wrapper
# ----------------------------------------------------------------------

def _a1_coordinate(row0: int, col0: int) -> str:
    """0 基行列 → A1 坐标（仅用于来源定位，列号超过 Z 也正确）。"""
    letters = ""
    col = col0
    while True:
        letters = chr(ord("A") + col % 26) + letters
        col = col // 26 - 1
        if col < 0:
            break
    return f"{letters}{row0 + 1}"


class _XlsCell:
    __slots__ = ("value", "coordinate")

    def __init__(self, value, coordinate: str) -> None:
        self.value = value
        self.coordinate = coordinate


class _XlsSheet:
    def __init__(self, book, sheet, title: str) -> None:
        self._sheet = sheet
        self.title = title
        if sheet.nrows * max(sheet.ncols, 1) > MAX_SHEET_CELLS:
            raise UnsupportedSourceError(
                f"工作表 {title!r} 单元格数量超出安全限制，拒绝解析")
        self.max_row = sheet.nrows
        self.max_column = sheet.ncols

    def cell(self, row: int, col: int) -> _XlsCell:
        row0, col0 = row - 1, col - 1
        if row0 < 0 or col0 < 0 or row0 >= self._sheet.nrows or col0 >= self._sheet.ncols:
            return _XlsCell(None, _a1_coordinate(max(row0, 0), max(col0, 0)))
        cell = self._sheet.cell(row0, col0)
        value = cell.value
        if value == "" or cell.ctype == 0:  # XL_CELL_EMPTY / 空文本统一为 None
            value = None
        return _XlsCell(value, _a1_coordinate(row0, col0))


class _XlsWorkbook:
    """xlrd book 的统一接口视图（on_demand，逐 sheet 物化并释放。"""

    has_formula_info = False

    def __init__(self, path: str) -> None:
        try:
            import xlrd
        except ImportError as exc:
            raise WorkbookDependencyError(
                "解析 .xls 附件需要 xlrd 依赖，但当前运行环境未安装"
            ) from exc
        try:
            self._book = xlrd.open_workbook(path, on_demand=True)
        except Exception as exc:
            raise UnsupportedSourceError(
                f"无法解析 xls 工作簿（可能损坏或加密）：{exc}") from exc
        self.sheetnames = [str(name) for name in self._book.sheet_names()]

    def __getitem__(self, name: str) -> _XlsSheet:
        index = self._book.sheet_names().index(name)
        sheet = self._book.sheet_by_index(index)
        return _XlsSheet(self._book, sheet, str(name))

    def close(self) -> None:  # 与 openpyxl 对齐的兼容方法
        self._book.release_resources()


# ----------------------------------------------------------------------
# xlsx loader
# ----------------------------------------------------------------------

def _load_xlsx_pair(p: Path):
    try:
        if not zipfile.is_zipfile(p):
            raise UnsupportedSourceError(
                f"附件不是有效的 Excel 文件：{p.name}（可能损坏；支持 .xlsx 与 .xls）")
        with zipfile.ZipFile(p) as probe:
            members = probe.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise UnsupportedSourceError(
                    f"附件 {p.name} 压缩包成员数超出安全限制，拒绝解析")
            total = sum(m.file_size for m in members)
            if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise UnsupportedSourceError(
                    f"附件 {p.name} 解压后大小超出安全限制，拒绝解析")
        values_wb = openpyxl.load_workbook(p, data_only=True)
        formula_wb = openpyxl.load_workbook(p, data_only=False)
    except UnsupportedSourceError:
        raise
    except Exception as exc:  # openpyxl 各类解析异常
        raise UnsupportedSourceError(f"无法解析附件 {p.name}：{exc}") from exc
    return values_wb, formula_wb


def load_workbook_pair(path: str):
    """返回 (values_wb, formula_wb)。不支持的格式抛 UnsupportedSourceError。

    xls 的 formula_wb 与 values_wb 是同一对象（xlrd 不提供公式信息），
    公式缓存复核（cell_value）因此对 xls 自动跳过。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".xls":
        head = p.open("rb").read(8)
        if head[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise UnsupportedSourceError(
                f"附件 {p.name} 不是有效的旧版 xls（OLE2 签名不符），"
                "请补充未损坏的 .xls 或 .xlsx 文件")
        wb = _XlsWorkbook(str(p))
        return wb, wb
    if suffix == ".xlsx":
        return _load_xlsx_pair(p)
    raise UnsupportedSourceError(
        f"不支持的附件格式：{p.name}（仅支持 .xlsx 与 .xls，"
        "加密/其他格式请转换为未加密的 xlsx 后重发）")


def has_formula_info(wb) -> bool:
    return getattr(wb, "has_formula_info", True)


def cell_value(ws_values, ws_formula, row: int, col: int, *,
               record_key: Optional[str], field: str,
               filename: str, issues: list):
    """读取单元格值；xlsx 中若是公式且缓存缺失，登记问题并返回 None。

    公式有缓存时返回缓存值（不自行计算公式）。xls 无公式信息，直接返回值。
    """
    v = ws_values.cell(row, col).value
    if not has_formula_info(ws_formula):
        return v
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
