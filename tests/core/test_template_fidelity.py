"""模板构建保真回归：原始样例 → 模板 → 输出 全链路逐部件验证。

审核要求：模板构建不得再有 openpyxl 全量另存导致的 printerSettings/VML 损失；
只有必要结构清理与数据清理差异允许。本测试逐部件锁定差异白名单。
"""
import re
import zipfile

import openpyxl

from cater.core.api import process_job

from conftest import (REPO_ROOT, TEMPLATE_DIR)

SAMPLES = {
    "parts_ledger.xlsx": REPO_ROOT / "output" / "配件" / "出口台账表体.xlsx",
    "machine_materials.xlsx": REPO_ROOT / "output" / "整机" / "企业物料 上传.xlsx",
    "machine_comm.xlsx": REPO_ROOT / "output" / "整机" / "SEM发运沟通表 上传.xlsx",
}

# 每份模板允许变化的部件白名单（必要结构/数据清理），其余必须字节一致
EXPECTED_CHANGED = {
    "parts_ledger.xlsx": {"xl/worksheets/sheet1.xml", "xl/sharedStrings.xml"},
    "machine_materials.xlsx": {"xl/worksheets/sheet1.xml", "xl/sharedStrings.xml",
                               "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                               "[Content_Types].xml"},
    "machine_comm.xlsx": {"xl/worksheets/sheet1.xml", "xl/sharedStrings.xml",
                          "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                          "[Content_Types].xml"},
}
EXPECTED_REMOVED = {
    "parts_ledger.xlsx": set(),
    "machine_materials.xlsx": {"xl/worksheets/sheet2.xml", "xl/worksheets/sheet3.xml",
                               "xl/worksheets/_rels/sheet2.xml.rels",
                               "xl/printerSettings/printerSettings2.bin"},
    "machine_comm.xlsx": {"xl/worksheets/sheet2.xml",
                          "xl/worksheets/_rels/sheet2.xml.rels",
                          "xl/printerSettings/printerSettings2.bin"},
}
# 必须原字节保留的关键部件（打印设置、批注、样式、主题、文档属性）
MUST_BE_BYTE_IDENTICAL = [
    "xl/printerSettings/printerSettings1.bin",
    "xl/styles.xml",
    "xl/theme/theme1.xml",
    "docProps/core.xml",
    "docProps/app.xml",
    "docProps/custom.xml",
]


def _parts(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def _diff(a: dict, b: dict):
    changed = {n for n in a if n in b and a[n] != b[n]}
    removed = set(a) - set(b)
    added = set(b) - set(a)
    return changed, removed, added


def test_template_vs_sample_part_whitelist():
    """样例→模板：差异严格限于必要结构/数据清理白名单。"""
    for tpl_name, sample in SAMPLES.items():
        a, b = _parts(sample), _parts(TEMPLATE_DIR / tpl_name)
        changed, removed, added = _diff(a, b)
        assert changed == EXPECTED_CHANGED[tpl_name], (tpl_name, changed)
        assert removed == EXPECTED_REMOVED[tpl_name], (tpl_name, removed)
        assert added == set(), (tpl_name, added)


def test_template_preserves_printer_settings_and_comments_bytes():
    """保留表引用的 printerSettings 与被删 sheet 无关的批注部件原字节保留。"""
    for tpl_name, sample in SAMPLES.items():
        a, b = _parts(sample), _parts(TEMPLATE_DIR / tpl_name)
        for part in MUST_BE_BYTE_IDENTICAL:
            assert part in b, (tpl_name, part, "缺失")
            assert a[part] == b[part], (tpl_name, part, "被改动")
    # 沟通表批注（comments1.xml + vmlDrawing1.vml）原字节保留
    a, b = _parts(SAMPLES["machine_comm.xlsx"]), \
        _parts(TEMPLATE_DIR / "machine_comm.xlsx")
    for part in ("xl/comments1.xml", "xl/drawings/vmlDrawing1.vml"):
        assert part in b and a[part] == b[part], part


def test_template_sheet_structure_references_intact():
    """保留表 sheet XML 的 autofilter/mergeCells/pageSetup(打印机引用)/legacyDrawing 保留。"""
    checks = {
        "parts_ledger.xlsx": ("xl/worksheets/sheet1.xml",
                              ['<pageSetup paperSize="9" orientation="portrait" r:id="rId1"/>',
                               "<mergeCells", "Confidential Green"]),
        "machine_materials.xlsx": ("xl/worksheets/sheet1.xml",
                                   ['<autoFilter ref="A5:W9"',
                                    '<pageSetup paperSize="9" orientation="portrait" r:id="rId1"/>',
                                    "<mergeCells"]),
        "machine_comm.xlsx": ("xl/worksheets/sheet1.xml",
                              ['<autoFilter ref="A2:AU2"',
                               '<pageSetup paperSize="9" orientation="portrait" r:id="rId1"/>',
                               '<legacyDrawing r:id="rId2"/>']),
    }
    for tpl_name, (sheet, needles) in checks.items():
        xml = _parts(TEMPLATE_DIR / tpl_name)[sheet].decode()
        for needle in needles:
            assert needle in xml, (tpl_name, needle)


def test_template_no_historical_sheet_data():
    """被删历史/中间 sheet 的数据字符串不得残留在模板 sharedStrings。"""
    for tpl_name, forbidden in (
            ("machine_materials.xlsx", ("GMMCO LIMITED", "BDPWF", "H8R00498")),
            ("machine_comm.xlsx", ("土库曼斯坦", "WEIGHT"))):
        sst = _parts(TEMPLATE_DIR / tpl_name)["xl/sharedStrings.xml"].decode()
        for text in forbidden:
            assert text not in sst, (tpl_name, text)
        # 每个保留 <si> 都被保留表引用（无孤儿历史条目）
        tpl = _parts(TEMPLATE_DIR / tpl_name)
        used = set()
        for n, d in tpl.items():
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
                used.update(int(v) for v in
                            re.findall(r't="s"><v>(\d+)</v>', d.decode()))
        sis = re.findall(r"<si>", sst)
        assert len(sis) == len(used) == max(used) + 1, (tpl_name, len(sis), len(used))


def test_template_headers_and_empty_data_region():
    """openpyxl 回读：表头完好、数据区清空、工作表清单正确。"""
    expect = {
        "parts_ledger.xlsx": (["导入模板"], "导入模板", 6, "商品料号"),
        "machine_materials.xlsx": (["企业物料"], "企业物料", 6, "商品料号"),
        "machine_comm.xlsx": (["Sheet1"], "Sheet1", 3, "Dealer Code"),
    }
    for tpl_name, (sheets, sheet, first_data, header) in expect.items():
        wb = openpyxl.load_workbook(TEMPLATE_DIR / tpl_name)
        assert wb.sheetnames == sheets, (tpl_name, wb.sheetnames)
        ws = wb[sheet]
        assert ws.cell(first_data - 1, 1).value == header or \
            any(ws.cell(first_data - 1, c).value == header
                for c in range(1, ws.max_column + 1)), (tpl_name, header)
        for r in range(first_data, ws.max_row + 1):
            for c in range(1, ws.max_column + 1):
                v = ws.cell(r, c).value
                assert v in (None, "") or (isinstance(v, str) and not v.strip()), \
                    (tpl_name, ws.cell(r, c).coordinate, v)


def test_full_chain_sample_to_template_to_output(parts_request, machine_request):
    """样例→模板→输出：printerSettings 与批注部件逐字节全程保留。"""
    resp = process_job(machine_request)
    assert resp["status"] == "ready", resp["issues"]
    comm_art = next(a for a in resp["artifacts"] if "沟通表" in a["filename"])
    sample = _parts(SAMPLES["machine_comm.xlsx"])
    out = _parts(comm_art["path"])
    for part in ("xl/printerSettings/printerSettings1.bin",
                 "xl/comments1.xml", "xl/drawings/vmlDrawing1.vml"):
        assert part in out, part
        assert out[part] == sample[part], f"输出未保留原始部件字节：{part}"
    # 输出仍只有目标 sheet 被改动（对模板）
    tpl = _parts(TEMPLATE_DIR / "machine_comm.xlsx")
    changed, removed, added = _diff(tpl, out)
    assert changed == {"xl/worksheets/sheet1.xml"}, changed
    assert removed == set() and added == set()

    resp = process_job(parts_request)
    assert resp["status"] == "ready", resp["issues"]
    out = _parts(resp["artifacts"][0]["path"])
    sample = _parts(SAMPLES["parts_ledger.xlsx"])
    assert out["xl/printerSettings/printerSettings1.bin"] == \
        sample["xl/printerSettings/printerSettings1.bin"]
