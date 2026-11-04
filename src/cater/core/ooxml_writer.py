"""OOXML 级工作表重写：只替换目标工作表的 sheet XML，其余部件字节不变。

动机（docs/template-audit.md §3）：openpyxl 全量另存会重写未修改部件
（批注 VML/几何、printerSettings、sharedStrings 布局等），未经用户确认
不得判定这些差异“可接受”。本模块改为 zip/XML 级替换：

- 仅解析并重写目标工作表的 <sheetData> 数据区行；
- 包内所有其他部件（含批注、样式、docProps、[Content_Types].xml 等）
  以原始字节原样复制，输出后可逐部件 sha256 比对证明未改动；
- 模板数据区行已预置样式化空单元格（模板由 template_builder 生成）；
  新写入行复用模板行的单元格样式，超出行复制最后一个原型行。

工作表 XML 结构约束：模板由 template_builder（openpyxl 3.1.5）生成，
行/单元格为正则可解析的扁平结构；写入后用 openpyxl 回读校验
（template_writer._verify）。
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_ROW_RE = re.compile(r"<row\b[^>]*?/>|<row\b[^>]*?>.*?</row>", re.DOTALL)
_ROW_NUM_RE = re.compile(r'\br="(\d+)"')
_CELL_RE = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.DOTALL)
_CELL_REF_RE = re.compile(r'\br="([A-Z]+)(\d+)"')
_CELL_STYLE_RE = re.compile(r'\bs="(\d+)"')
_DIMENSION_RE = re.compile(r'<dimension ref="[^"]*"/>')


class OoxmlWriteError(Exception):
    pass


def sheet_path(zf: zipfile.ZipFile, sheet_name: str) -> str:
    """按工作表名定位包内 sheet XML 路径。"""
    import xml.etree.ElementTree as ET

    wb_xml = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_target = {r.get("Id"): r.get("Target") for r in rels}
    for sheet in wb_xml.iter(f"{{{NS_MAIN}}}sheet"):
        if sheet.get("name") == sheet_name:
            target = rid_target[sheet.get(f"{{{NS_R}}}id")].lstrip("/")
            return target if target.startswith("xl/") else "xl/" + target
    raise OoxmlWriteError(f"模板缺少工作表 {sheet_name!r}")


def _col_index(letter: str) -> int:
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def _split_row(row_xml: str):
    """拆出 <row ...> 开标签属性、行号与单元格内容。"""
    m = re.match(r"<row\b([^>]*?)(?:/>|>(.*)</row>)", row_xml, re.DOTALL)
    if not m:
        raise OoxmlWriteError(f"无法解析行 XML：{row_xml[:80]!r}")
    attrs, body = m.group(1) or "", m.group(2) or ""
    num = int(_ROW_NUM_RE.search(attrs).group(1))
    return attrs, num, body


def _parse_cells(body: str) -> list:
    """[(列序, 列字母, 样式号或None, 原始单元格XML)]，保持文档顺序。"""
    cells = []
    for m in _CELL_RE.finditer(body):
        attrs, inner = m.group(1), m.group(2)
        ref = _CELL_REF_RE.search(attrs)
        if not ref:
            continue
        letter = ref.group(1)
        style = _CELL_STYLE_RE.search(attrs)
        cells.append((_col_index(letter), letter,
                      style.group(1) if style else None, m.group(0)))
    return cells


def _cell_xml(coord: str, style, value, numeric: bool) -> str:
    s_attr = f' s="{style}"' if style is not None else ""
    if value is None or value == "":
        return f'<c r="{coord}"{s_attr} t="n"></c>'
    if numeric:
        if isinstance(value, float):
            text = repr(value)
        else:
            text = str(value)
        return f'<c r="{coord}"{s_attr}><v>{text}</v></c>'
    return (f'<c r="{coord}"{s_attr} t="inlineStr">'
            f'<is><t xml:space="preserve">{escape(str(value))}</t></is></c>')


def rewrite_sheet(template_path: str, sheet_name: str, data_start_row: int,
                  prototype_rows: int, columns: tuple, numeric_fields: frozenset,
                  values_by_row: dict) -> bytes:
    """返回整包 xlsx 字节：仅目标工作表数据区被重写。

    values_by_row: {行号: {字段键: 值}}；columns: ((列字母, 字段键), ...)。
    - 数据区现有行复用其模板单元格样式；写入列缺失的单元格按
      “最后原型行 → 首个原型行 → 默认样式” 依次取样式；
    - 超出原型行数的写入行复制最后一个原型行（样式原型继承）；
    - 未写到的原型行整行删除（旧值不残留）；原型之外的既有行保留；
    - 数值字段写 <v>（数值存储），其余写 inlineStr（文本存储）。
    """
    with zipfile.ZipFile(template_path) as zin:
        entries = [(n, zin.read(n)) for n in zin.namelist()]
        target = sheet_path(zin, sheet_name)

    sheet = None
    for i, (name, data) in enumerate(entries):
        if name == target:
            sheet = data.decode("utf-8")
            sheet_idx = i
            break
    if sheet is None:
        raise OoxmlWriteError(f"模板缺少部件 {target}")

    sd_start = sheet.find("<sheetData>")
    sd_end = sheet.find("</sheetData>")
    if sd_start < 0 or sd_end < 0:
        raise OoxmlWriteError("工作表缺少 sheetData")
    head, body, tail = sheet[:sd_start], sheet[sd_start:sd_end], sheet[sd_end:]

    row_spans = []  # (行号, 起始, 结束, XML)
    for m in _ROW_RE.finditer(body):
        attrs, num, _ = _split_row(m.group(0))
        row_spans.append((num, m.start(), m.end(), m.group(0)))
    rows_by_num = {num: xml for num, _, _, xml in row_spans}

    last_proto = data_start_row + prototype_rows - 1
    proto_rows = [r for r in range(data_start_row, last_proto + 1)]
    missing = [r for r in proto_rows if r not in rows_by_num]
    if missing:
        raise OoxmlWriteError(f"模板数据区缺原型行：{missing}")

    col_letters = {key: letter for letter, key in columns}

    def donor_style(letter: str, row_num: int):
        for donor in (rows_by_num.get(last_proto), rows_by_num.get(data_start_row)):
            if donor is None:
                continue
            _, _, d_body = _split_row(donor)
            for _, d_letter, d_style, _ in _parse_cells(d_body):
                if d_letter == letter:
                    return d_style
        return None

    def build_row(row_num: int, row_xml: str, field_values: dict) -> str:
        attrs, _, r_body = _split_row(row_xml)
        cells = _parse_cells(r_body)
        by_letter = {letter: (style, xml) for _, letter, style, xml in cells}
        out = []
        for _, letter, style, xml in cells:
            key = next((k for l, k in columns if l == letter), None)
            if key is not None and key in field_values:
                out.append(_cell_xml(f"{letter}{row_num}", style,
                                     field_values[key], key in numeric_fields))
            else:
                # 模板行应为空样式单元格；仍剥离任何残留值，保证旧值不残留
                out.append(_cell_xml(f"{letter}{row_num}", style, None, False))
        have = {letter for _, letter, _, _ in cells}
        for letter, key in columns:
            if letter in have or key not in field_values:
                continue
            style = donor_style(letter, row_num)
            out.append(_cell_xml(f"{letter}{row_num}", style,
                                 field_values[key], key in numeric_fields))
        out.sort(key=lambda x: _col_index(_CELL_REF_RE.search(x).group(1)))
        return f'<row r="{row_num}"{attrs.replace(f"r=\"{row_num}\"", "", 1)}>' \
               + "".join(out) + "</row>"

    written = sorted(values_by_row)
    new_rows = {}
    for row_num in written:
        field_values = values_by_row[row_num]
        if row_num in rows_by_num:
            base = rows_by_num[row_num]
            attrs, _, r_body = _split_row(base)
        else:
            # 超出行数：复制最后一个原型行（含其全部样式单元格）
            attrs, _, r_body = _split_row(rows_by_num[last_proto])
        attrs = _ROW_NUM_RE.sub(f'r="{row_num}"', attrs, count=1)
        new_rows[row_num] = build_row(row_num, f"<row{attrs}>{r_body}</row>",
                                      field_values)

    # 未写到的原型行删除；其余既有行原样保留；模板之外的新行按序追加
    remove = {r for r in proto_rows if r not in values_by_row}
    existing_nums = {num for num, _, _, _ in row_spans}
    overflow = sorted(n for n in new_rows if n not in existing_nums)
    pieces = []
    cursor = 0
    for num, start, end, xml in row_spans:
        if num in remove:
            pieces.append(body[cursor:start])
            cursor = end
        elif num in new_rows:
            pieces.append(body[cursor:start])
            pieces.append(new_rows[num])
            cursor = end
    pieces.append(body[cursor:])
    for num in overflow:
        pieces.append(new_rows[num])
    new_body = "".join(pieces)

    # dimension 行范围跟随实际数据（仅修改部件内的提示属性）
    remaining = [num for num, _, _, _ in row_spans if num not in remove]
    if remaining:
        max_row = max(max(remaining), max(written))
        dim = _DIMENSION_RE.search(head)
        if dim:
            ref = dim.group(0)
            col_part = ref.split(":")[0].split("ref=\"")[1]
            head = _DIMENSION_RE.sub(f'<dimension ref="{col_part}:{max_row}"/>', head, count=1)

    new_sheet = (head + new_body + tail).encode("utf-8")
    entries[sheet_idx] = (target, new_sheet)
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in entries:
            zout.writestr(name, data)
    return buf.getvalue()
