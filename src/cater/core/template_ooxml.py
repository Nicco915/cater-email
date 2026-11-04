"""OOXML 级模板构建：只清理目标 sheet 数据值与删除中间 sheet 的必要结构。

审核要求（docs/template-audit.md §3/§4）：格式严格不变，不允许把 printerSettings
丢失、批注 VML 重写等可避免缺陷交给用户批准。本模块替代 openpyxl 全量另存：

保留字节不变的部件（保留表引用或共享）：
- xl/printerSettings/printerSettings1.bin（保留表的打印设置，经 sheet rels 引用）
- xl/comments1.xml、xl/drawings/vmlDrawing1.vml（批注，原字节）
- xl/styles.xml、xl/theme/theme1.xml、docProps/*（原字节）

仅做必要结构与数据清理：
- 保留 sheet：自 clear_from 行起清除单元格值（<v>/<is>/<f> 与 t 属性），
  单元格样式（r/s 属性、行高、列宽、autofilter、mergeCells、pageSetup 引用）
  全部原样保留；
- sharedStrings.xml：仅保留保留表仍引用的 <si>（原 <si> 字节片段原样复制、
  相对顺序不变、索引重映射），被删历史 sheet 的字符串不残留；
- 删除中间/历史 sheet：worksheet 部件、其 _rels、其专属部件（printerSettings2
  等）、workbook.xml 的 <sheet> 项、workbook.xml.rels 的对应 Relationship、
  [Content_Types].xml 的对应 Override、指向被删 sheet 的 definedName。

原始样例只读；输出后可与原始样例逐部件比对（见 tests/core/test_template_fidelity.py）。
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

_ROW_RE = re.compile(r"<row\b[^>]*?/>|<row\b[^>]*?>.*?</row>", re.DOTALL)
_ROW_NUM_RE = re.compile(r'\br="(\d+)"')
_CELL_RE = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.DOTALL)
_CELL_T_RE = re.compile(r'\st="[^"]*"')
_SHEET_EL_RE = re.compile(r"<sheet\b[^>]*/>")
_REL_RE = re.compile(r"<Relationship\b[^>]*/>")
_CT_OVERRIDE_RE = re.compile(r"<Override\b[^>]*/>")
_DEFNAME_RE = re.compile(r"<definedName\b[^>]*>.*?</definedName>|<definedName\b[^>]*/>",
                         re.DOTALL)
_SI_RE = re.compile(r"<si>.*?</si>|<si/>", re.DOTALL)
_SST_OPEN_RE = re.compile(r"<sst\b[^>]*>")


class TemplateBuildError(Exception):
    pass


def _norm_part(base: str, target: str) -> str:
    """把 rels 的相对 target 解析为包内部件路径（base 为引用部件所在目录）。"""
    import posixpath
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base, target))


def _clear_values(sheet_xml: str, clear_from: int) -> str:
    """清除 clear_from 行起所有单元格的值（保留单元格与行结构/样式）。"""
    def clear_row(m):
        row = m.group(0)
        rm = _ROW_NUM_RE.search(row)
        if not rm or int(rm.group(1)) < clear_from:
            return row
        # 逐个单元格：去掉 t 属性与 <v>/<is>/<f> 内容
        def clear_cell(cm):
            attrs, inner = cm.group(1), cm.group(2)
            attrs = _CELL_T_RE.sub("", attrs)
            if inner is None:
                return f"<c{attrs}/>"
            inner = re.sub(r"<f\b[^>]*>.*?</f>|<f\b[^>]*/>|<v>.*?</v>|<is>.*?</is>",
                           "", inner, flags=re.DOTALL)
            return f"<c{attrs}>{inner}</c>" if inner else f"<c{attrs}/>"
        return _CELL_RE.sub(clear_cell, row)
    return _ROW_RE.sub(clear_row, sheet_xml)


def _collect_shared_refs(sheet_xml: str) -> list:
    return [int(v) for v in
            re.findall(r'<c\b[^>]*\bt="s"[^>]*><v>(\d+)</v></c>', sheet_xml)]


def _remap_shared(sheet_xml: str, remap: dict) -> str:
    def fix(cm):
        attrs, inner = cm.group(1), cm.group(2)
        if inner is None or 't="s"' not in attrs:
            return cm.group(0)
        new_inner = re.sub(r"<v>(\d+)</v>",
                           lambda m: f"<v>{remap[int(m.group(1))]}</v>", inner)
        return f"<c{attrs}>{new_inner}</c>"
    return _CELL_RE.sub(fix, sheet_xml)


def build_template(src_path: str, dst_path: str, keep_sheets: list,
                   clear_from: dict) -> dict:
    """构建受控模板，返回实际差异清单（供审计与测试断言）。

    keep_sheets: 保留工作表名；clear_from: {sheet_name: 首数据行}。
    """
    with zipfile.ZipFile(src_path) as zin:
        entries = [(n, zin.read(n)) for n in zin.namelist()]

    def get(name):
        for n, d in entries:
            if n == name:
                return d.decode("utf-8")
        raise TemplateBuildError(f"部件缺失：{name}")

    # --- 工作表结构 ---
    wb_xml = get("xl/workbook.xml")
    rels_xml = get("xl/_rels/workbook.xml.rels")
    wb_root = ET.fromstring(wb_xml)
    rels_root = ET.fromstring(rels_xml)
    rid_target = {r.get("Id"): r.get("Target") for r in rels_root}
    rid_type = {r.get("Id"): r.get("Type") for r in rels_root}
    sheets = []  # (name, sheetId, rid)
    for sh in wb_root.iter(f"{{{NS_MAIN}}}sheet"):
        sheets.append((sh.get("name"), sh.get("sheetId"), sh.get(f"{{{NS_R}}}id")))
    by_name = {n: rid for n, _, rid in sheets}
    missing = [n for n in keep_sheets if n not in by_name]
    if missing:
        raise TemplateBuildError(f"样例缺少保留工作表：{missing}")
    deleted = [(i, n, rid) for i, (n, _, rid) in enumerate(sheets)
               if n not in keep_sheets]

    deleted_sheet_parts = set()
    removed_parts = set()
    for _, _, rid in deleted:
        if rid_type.get(rid, "").endswith("/worksheet"):
            part = _norm_part("xl", rid_target[rid])
            deleted_sheet_parts.add(part)
            removed_parts.add(part)
            rels_part = part.replace("xl/worksheets/", "xl/worksheets/_rels/") + ".rels"
            if any(n == rels_part for n, _ in entries):
                removed_parts.add(rels_part)
                rels_root2 = ET.fromstring(get(rels_part))
                base = rels_part.rsplit("/", 1)[0].replace("/_rels", "")
                for r in rels_root2:
                    tgt = _norm_part(base, r.get("Target"))
                    # sheet 级 rels 指向的都是 sheet 专属部件（printerSettings/drawing 等）
                    removed_parts.add(tgt)
    # workbook 级共享部件不可经 sheet rels 删除；以上 target 均限 sheet 目录相对路径

    # --- 保留 sheet：清数据值 ---
    sheet_part_by_name = {}
    for name in keep_sheets:
        rid = by_name[name]
        sheet_part_by_name[name] = _norm_part("xl", rid_target[rid])
    new_content = {}
    for name, part in sheet_part_by_name.items():
        xml = get(part)
        new_content[part] = _clear_values(xml, clear_from[name])

    # --- sharedStrings：仅保留保留表引用的 <si>（原字节片段、原相对顺序） ---
    used = set()
    for part in sheet_part_by_name.values():
        used.update(_collect_shared_refs(new_content[part]))
    sst = get("xl/sharedStrings.xml")
    sst_open = _SST_OPEN_RE.search(sst)
    sis = [m.group(0) for m in _SI_RE.finditer(sst[sst_open.end():sst.rfind("</sst>")])]
    remap = {old: new for new, old in enumerate(sorted(used))}
    kept_si = [sis[i] for i in sorted(used)]
    total_refs = sum(len(_collect_shared_refs(new_content[p]))
                     for p in sheet_part_by_name.values())
    sst_open_new = re.sub(r'\scount="\d+"', f' count="{total_refs}"', sst_open.group(0))
    sst_open_new = re.sub(r'\suniqueCount="\d+"',
                          f' uniqueCount="{len(kept_si)}"', sst_open_new)
    new_content["xl/sharedStrings.xml"] = \
        sst[:sst_open.start()] + sst_open_new + "".join(kept_si) + "</sst>"
    for part in sheet_part_by_name.values():
        new_content[part] = _remap_shared(new_content[part], remap)

    # --- workbook.xml：删 <sheet> 项与被删 sheet 的 definedName ---
    new_wb = _SHEET_EL_RE.sub(
        lambda m: "" if re.search(r'<sheet\b[^>]*\bname="([^"]*)"', m.group(0))
        .group(1) not in keep_sheets else m.group(0), wb_xml)
    deleted_ids = {i for i, _, _ in deleted}
    if deleted_ids:
        def fix_defnames(m):
            block = m.group(0)
            def keep_dn(dm):
                lm = re.search(r'\blocalSheetId="(\d+)"', dm.group(0))
                return lm is None or int(lm.group(1)) not in deleted_ids
            return "<definedNames>" + "".join(
                d.group(0) for d in _DEFNAME_RE.finditer(block) if keep_dn(d)) \
                + "</definedNames>"
        new_wb = re.sub(r"<definedNames>.*?</definedNames>", fix_defnames,
                        new_wb, flags=re.DOTALL)
    new_content["xl/workbook.xml"] = new_wb

    # --- workbook rels：删被删 sheet 的 Relationship ---
    new_content["xl/_rels/workbook.xml.rels"] = _REL_RE.sub(
        lambda m: "" if re.search(r'\bId="([^"]*)"', m.group(0)).group(1)
        in {rid for _, _, rid in deleted} else m.group(0), rels_xml)

    # --- [Content_Types].xml：删被删部件的 Override ---
    ct = get("[Content_Types].xml")
    removed_override = {"/" + p for p in removed_parts}
    new_content["[Content_Types].xml"] = _CT_OVERRIDE_RE.sub(
        lambda m: "" if re.search(r'\bPartName="([^"]*)"', m.group(0)).group(1)
        in removed_override else m.group(0), ct)

    # --- 写包：未修改部件原字节复制，移除被删部件 ---
    out = io.BytesIO()
    changed, identical, removed = [], [], []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in entries:
            if name in removed_parts:
                removed.append(name)
                continue
            if name in new_content:
                blob = new_content[name].encode("utf-8")
                (changed if blob != data else identical).append(name)
                zout.writestr(name, blob)
            else:
                identical.append(name)
                zout.writestr(name, data)
    with open(dst_path, "wb") as fh:
        fh.write(out.getvalue())
    return {"changed": sorted(changed), "identical": sorted(identical),
            "removed": sorted(removed)}
