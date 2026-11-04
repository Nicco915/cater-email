"""测试辅助：在不破坏公式缓存的前提下对 xlsx 做单元格级修改（zip/XML 级）。

openpyxl 保存会丢弃全部公式缓存值，无法用于构造“修改一个值但保留其他缓存”
的测试样例；本模块直接在 sheet XML 上替换目标单元格。
"""
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _sheet_path(zf, sheet_name):
    wb_xml = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_target = {r.get("Id"): r.get("Target") for r in rels}
    for sheet in wb_xml.iter(f"{{{NS_MAIN}}}sheet"):
        if sheet.get("name") == sheet_name:
            target = rid_target[sheet.get(f"{{{NS_R}}}id")].lstrip("/")
            return target if target.startswith("xl/") else "xl/" + target
    raise KeyError(sheet_name)


def edit_cell(xlsx_path, sheet_name, coord, value):
    """把 coord 单元格替换为字面量（str 或数字），保留其余全部内容。"""
    xlsx_path = Path(xlsx_path)
    tmp = xlsx_path.with_suffix(".edit.xlsx")
    with zipfile.ZipFile(xlsx_path) as zin:
        entries = {n: zin.read(n) for n in zin.namelist()}
        sheet = _sheet_path(zin, sheet_name)
    xml = entries[sheet].decode("utf-8")
    if isinstance(value, str):
        new_cell = f'<c r="{coord}" t="inlineStr"><is><t xml:space="preserve">{value}</t></is></c>'
    else:
        new_cell = f'<c r="{coord}"><v>{value}</v></c>'
    pattern = re.compile(r'<c r="%s"[^>]*(?:/>|>.*?</c>)' % re.escape(coord), re.DOTALL)
    if not pattern.search(xml):
        raise KeyError(f"cell {coord} not found in {sheet_name}")
    xml = pattern.sub(lambda m: new_cell, xml, count=1)
    entries[sheet] = xml.encode("utf-8")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in entries.items():
            zout.writestr(name, data)
    shutil.move(str(tmp), str(xlsx_path))
