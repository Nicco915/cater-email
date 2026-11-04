"""模板填充：仅写白名单数据区域，其余包部件字节不变。

规则（plan.md §6/§13 与审核修正）：
- 表头/说明行保护，只写数据区；超出行数复制最后一个原型行样式；
- 金额/数量/重量按数值存储（Decimal → Excel 安全数值 int/float）；
  标识符（SO、HS 编码、料号、MSO、币种、国家、名称、规格、申报要素、
  发票号、订单号等）按文本存储；
- 写入采用 OOXML 级替换（ooxml_writer）：除目标工作表 sheet XML 外，
  包内所有部件（批注、样式、docProps、printerSettings 等）字节不变；
- 未写到的原型行整行删除，旧值不残留；保存后回读校验表头、写入值、
  数据区外无残留；零条有效记录不交付。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from .numbertext import decimal_to_excel_number
from .ooxml_writer import OoxmlWriteError, rewrite_sheet


@dataclass(frozen=True)
class TemplateSpec:
    """输出模板契约（plan.md §13）。"""

    key: str
    template_filename: str
    sheet_name: str
    protected_rows: int        # 受保护表头行数
    data_start_row: int        # 数据起始行
    prototype_rows: int        # 模板中保留的样式原型行数
    columns: tuple             # ((列字母, 字段键), ...)
    numeric_columns: frozenset # 按数值存储的字段键


PARTS_LEDGER_SPEC = TemplateSpec(
    key="parts_ledger",
    template_filename="parts_ledger.xlsx",
    sheet_name="导入模板",
    protected_rows=5,
    data_start_row=6,
    prototype_rows=15,
    columns=(
        ("B", "part_code"), ("D", "qty"), ("G", "currency"), ("H", "unit_price"),
        ("J", "net_weight"), ("K", "gross_weight"), ("L", "packages"),
        ("M", "coo"), ("N", "destination"), ("T", "hs_code"),
        ("U", "product_name"), ("V", "spec"), ("Z", "order_no"), ("AA", "invoice_no"),
    ),
    numeric_columns=frozenset({"qty", "unit_price", "net_weight",
                               "gross_weight", "packages"}),
)

MACHINE_MATERIALS_SPEC = TemplateSpec(
    key="machine_materials",
    template_filename="machine_materials.xlsx",
    sheet_name="企业物料",
    protected_rows=5,
    data_start_row=6,
    prototype_rows=4,
    columns=(
        ("A", "part_code"), ("B", "hs_code"), ("C", "product_name"), ("D", "declaration"),
        ("E", "decl_unit"), ("G", "english_name"), ("H", "goods_type"), ("I", "enterprise_unit"),
        ("L", "legal_ratio"), ("M", "second_ratio"), ("N", "unit_net_weight"),
        ("P", "currency"), ("Q", "coo"), ("U", "model"),
    ),
    numeric_columns=frozenset({"legal_ratio", "second_ratio", "unit_net_weight"}),
)

MACHINE_COMM_SPEC = TemplateSpec(
    key="machine_comm",
    template_filename="machine_comm.xlsx",
    sheet_name="Sheet1",
    protected_rows=2,
    data_start_row=3,
    prototype_rows=4,
    columns=(
        ("A", "dealer_code"), ("C", "dealer_order"), ("D", "mso"), ("E", "material_col"),
        ("F", "so"), ("G", "model"), ("H", "serial_number"), ("I", "machine_price"),
        ("V", "country"),
    ),
    numeric_columns=frozenset({"machine_price"}),
)

ALL_SPECS = {s.key: s for s in (PARTS_LEDGER_SPEC, MACHINE_MATERIALS_SPEC, MACHINE_COMM_SPEC)}


class TemplateWriteError(Exception):
    pass


def _col_index(letter: str) -> int:
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def write_output(template_dir: str, spec: TemplateSpec, rows: list,
                 output_dir: str, output_filename: str) -> dict:
    """把 rows（list[dict 字段键→文本/数值]）写入模板，返回 artifact 描述 dict。

    零行时抛 TemplateWriteError（零条有效记录不交付）。
    """
    if not rows:
        raise TemplateWriteError("零条有效记录，不生成输出文件")
    template_path = Path(template_dir) / spec.template_filename
    if not template_path.exists():
        raise TemplateWriteError(f"模板缺失：{template_path}")

    values_by_row = {}
    for i, row_data in enumerate(rows):
        field_values = {}
        for letter, field_key in spec.columns:
            value = row_data.get(field_key)
            if value in (None, ""):
                continue
            if field_key in spec.numeric_columns:
                try:
                    value = decimal_to_excel_number(value)
                except ValueError as exc:
                    raise TemplateWriteError(str(exc)) from exc
            else:
                value = str(value)
            field_values[field_key] = value
        values_by_row[spec.data_start_row + i] = field_values

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx", dir=out_dir)
    os.close(fd)
    try:
        data = rewrite_sheet(str(template_path), spec.sheet_name,
                             spec.data_start_row, spec.prototype_rows,
                             spec.columns, spec.numeric_columns, values_by_row)
        with open(tmp_path, "wb") as fh:
            fh.write(data)
        _verify(tmp_path, str(template_path), spec, rows)
        final_path = out_dir / output_filename
        shutil.move(tmp_path, final_path)
    except (OoxmlWriteError, TemplateWriteError):
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    except Exception as exc:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise TemplateWriteError(f"模板写入失败：{exc}") from exc

    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
    return {"path": str(final_path), "filename": output_filename, "sha256": digest,
            "template": spec.key}


def _verify(output_path: str, template_path: str, spec: TemplateSpec, rows: list) -> None:
    """回读验证：表头不变、写入值正确、数据区之外无残留。"""
    wb_out = openpyxl.load_workbook(output_path)
    wb_tpl = openpyxl.load_workbook(template_path)
    if wb_out.sheetnames != wb_tpl.sheetnames:
        raise TemplateWriteError(
            f"工作表结构被改动：{wb_out.sheetnames} != {wb_tpl.sheetnames}")
    ws_out = wb_out[spec.sheet_name]
    ws_tpl = wb_tpl[spec.sheet_name]
    for r in range(1, spec.protected_rows + 1):
        for c in range(1, ws_tpl.max_column + 1):
            if ws_tpl.cell(r, c).value != ws_out.cell(r, c).value:
                raise TemplateWriteError(
                    f"模板表头被改动：{spec.sheet_name}!{ws_tpl.cell(r, c).coordinate}")
    for i, row_data in enumerate(rows):
        r = spec.data_start_row + i
        for letter, field_key in spec.columns:
            expected = row_data.get(field_key)
            if expected in (None, ""):
                expected = None
            actual = ws_out.cell(r, _col_index(letter)).value
            if actual is not None:
                actual = str(actual)
            if expected is not None:
                expected = str(expected)
            if actual != expected:
                raise TemplateWriteError(
                    f"写入校验失败：{spec.sheet_name}!{letter}{r} 期望 {expected!r} 实际 {actual!r}")
    # 数据区之后不得有残留值
    last_data = spec.data_start_row + len(rows) - 1
    for r in range(last_data + 1, ws_out.max_row + 1):
        for c in range(1, ws_out.max_column + 1):
            v = ws_out.cell(r, c).value
            if v not in (None, "") and not (isinstance(v, str) and not v.strip()):
                raise TemplateWriteError(
                    f"数据区之外存在残留值：{spec.sheet_name}!{ws_out.cell(r, c).coordinate}={v!r}")
