"""模板保真：表头不变、样式继承、扩展行、旧值不残留、批注保留、样例对比。"""
import zipfile

import openpyxl
import pytest

from cater.core.api import process_job
from cater.core.template_writer import (MACHINE_COMM_SPEC, PARTS_LEDGER_SPEC,
                                        TemplateWriteError, write_output)

from conftest import REPO_ROOT, TEMPLATE_DIR

SAMPLE_PARTS_OUTPUT = REPO_ROOT / "output" / "配件" / "出口台账表体.xlsx"
SAMPLE_MAT_OUTPUT = REPO_ROOT / "output" / "整机" / "企业物料 上传.xlsx"
SAMPLE_COMM_OUTPUT = REPO_ROOT / "output" / "整机" / "SEM发运沟通表 上传.xlsx"


def _style_sig(cell):
    f, fl, b, a = cell.font, cell.fill, cell.border, cell.alignment
    return (f.name, f.sz, f.b, fl.patternType,
            tuple((s.style if s else None) for s in (b.left, b.right, b.top, b.bottom)),
            a.horizontal, a.vertical, a.wrapText, cell.number_format)


def test_storage_types_numeric_vs_text(tmp_path):
    """审核契约：金额/数量/重量按数值存储；SO/HS/料号等标识符按文本存储。"""
    row = {
        "part_code": "X1", "qty": "16", "currency": "USD", "unit_price": "28.24",
        "net_weight": "2.8", "gross_weight": "317", "packages": "2",
        "coo": "USA", "destination": "TGO", "hs_code": "8421999090",
        "product_name": "滤芯", "spec": "a|b", "order_no": "", "invoice_no": "INV-1",
    }
    out = write_output(str(TEMPLATE_DIR), PARTS_LEDGER_SPEC, [row],
                       str(tmp_path), "types.xlsx")
    ws = openpyxl.load_workbook(out["path"])["导入模板"]
    assert ws.cell(6, 2).value == "X1" and isinstance(ws.cell(6, 2).value, str)   # 料号文本
    assert ws.cell(6, 4).value == 16 and isinstance(ws.cell(6, 4).value, int)     # 数量数值
    assert ws.cell(6, 8).value == 28.24 and isinstance(ws.cell(6, 8).value, float)  # 单价数值
    assert ws.cell(6, 10).value == 2.8 and isinstance(ws.cell(6, 10).value, float)  # 净重数值
    assert ws.cell(6, 11).value == 317 and isinstance(ws.cell(6, 11).value, int)    # 毛重数值
    assert ws.cell(6, 12).value == 2 and isinstance(ws.cell(6, 12).value, int)      # 件数数值
    assert ws.cell(6, 13).value == "USA" and isinstance(ws.cell(6, 13).value, str)  # 国家文本
    assert ws.cell(6, 20).value == "8421999090" and isinstance(ws.cell(6, 20).value, str)  # HS 文本
    assert ws.cell(6, 27).value == "INV-1" and isinstance(ws.cell(6, 27).value, str)  # 发票号文本


def test_zero_rows_rejected(tmp_path):
    with pytest.raises(TemplateWriteError):
        write_output(str(TEMPLATE_DIR), PARTS_LEDGER_SPEC, [], str(tmp_path), "x.xlsx")


def test_single_and_extended_rows(tmp_path):
    """1 行、样例行数、超过样例行数：样式继承自原型行，表头不变。"""
    base_row = {
        "part_code": "X1", "qty": "1", "currency": "USD", "unit_price": "1.5",
        "net_weight": "0.5", "coo": "CHN", "destination": "FRA",
        "hs_code": "1234567890", "product_name": "测试", "spec": "a|b",
    }
    # 1 行
    out1 = write_output(str(TEMPLATE_DIR), PARTS_LEDGER_SPEC, [base_row],
                        str(tmp_path), "one.xlsx")
    ws = openpyxl.load_workbook(out1["path"])["导入模板"]
    assert ws.cell(6, 2).value == "X1"
    assert ws.cell(7, 2).value in (None, "")
    # 20 行（超过 15 行原型）
    rows = [dict(base_row, part_code=f"X{i}") for i in range(20)]
    out20 = write_output(str(TEMPLATE_DIR), PARTS_LEDGER_SPEC, rows,
                         str(tmp_path), "twenty.xlsx")
    wb = openpyxl.load_workbook(out20["path"])
    ws = wb["导入模板"]
    assert ws.cell(25, 2).value == "X19"
    # 扩展行样式继承原型行
    tpl_ws = openpyxl.load_workbook(TEMPLATE_DIR / "parts_ledger.xlsx")["导入模板"]
    proto_sig = _style_sig(tpl_ws.cell(20, 2))  # 最后一个原型行
    for r in (21, 25):
        assert _style_sig(ws.cell(r, 2)) == proto_sig
    # 表头未变
    assert ws.cell(5, 2).value == "商品料号"


def test_generated_matches_sample_cells(parts_request):
    """生成文件与人工样例逐格一致（除已确认差异：订单号留空、净重尾差归一）。"""
    resp = process_job(parts_request)
    assert resp["status"] == "ready", resp["issues"]
    gen = openpyxl.load_workbook(resp["artifacts"][0]["path"])["导入模板"]
    ref = openpyxl.load_workbook(SAMPLE_PARTS_OUTPUT)["导入模板"]
    diffs = []
    for r in range(1, 21):
        for c in range(1, 42):
            a, b = gen.cell(r, c).value, ref.cell(r, c).value
            if a is None and b is None:
                continue
            if str(a) != str(b):
                diffs.append((gen.cell(r, c).coordinate, a, b))
    # 仅允许：Z16 旧订单号清除、J8 净重尾差归一（现按数值存储，2.8 尾差被量化消除）
    allowed = {("Z16", None, "CBS20250423-219355A"), ("J8", 2.8, 2.8000000000000003)}
    assert set(diffs) == allowed, diffs


def _assert_only_sheet_changed(art_path: str, tpl_path, sheet_name: str):
    """输出包与模板逐部件比对：仅目标工作表 sheet XML 允许不同，其余字节一致。"""
    from cater.core.ooxml_writer import sheet_path
    with zipfile.ZipFile(tpl_path) as zt, zipfile.ZipFile(art_path) as zo:
        target = sheet_path(zt, sheet_name)
        tin = {n: zt.read(n) for n in zt.namelist()}
        out = {n: zo.read(n) for n in zo.namelist()}
    assert set(tin) == set(out), f"部件集合被改动：{set(tin) ^ set(out)}"
    changed = {n for n in tin if tin[n] != out[n]}
    assert changed == {target}, \
        f"非目标部件被改动：{changed - {target}}（批注/样式/docProps 等必须字节不变）"


def test_only_target_sheet_part_modified(parts_request, machine_request):
    """保真证据（OOXML 替换）：除目标工作表外所有部件（含批注）与模板字节一致。"""
    resp = process_job(parts_request)
    assert resp["status"] == "ready", resp["issues"]
    _assert_only_sheet_changed(resp["artifacts"][0]["path"],
                               TEMPLATE_DIR / "parts_ledger.xlsx", "导入模板")

    resp = process_job(machine_request)
    assert resp["status"] == "ready", resp["issues"]
    for art in resp["artifacts"]:
        if "企业物料" in art["filename"]:
            _assert_only_sheet_changed(art["path"],
                                       TEMPLATE_DIR / "machine_materials.xlsx", "企业物料")
        else:
            _assert_only_sheet_changed(art["path"],
                                       TEMPLATE_DIR / "machine_comm.xlsx", "Sheet1")


def test_generated_machine_matches_sample_cells(machine_request):
    """与人工样例逐格一致；唯一预期差异：SEM520 申报要素按规则取数据库原文
    19600KG（旧样例曾被手工改为 20000KG，见 plan.md §13）。数值格按文本比较。"""
    resp = process_job(machine_request)
    assert resp["status"] == "ready", resp["issues"]
    mat_art = next(a for a in resp["artifacts"] if "企业物料" in a["filename"])
    gen = openpyxl.load_workbook(mat_art["path"])["企业物料"]
    ref = openpyxl.load_workbook(SAMPLE_MAT_OUTPUT)["企业物料"]
    diffs = []
    for r in range(1, 10):
        for c in range(1, 24):
            a, b = gen.cell(r, c).value, ref.cell(r, c).value
            if a is None and b is None:
                continue
            if str(a) != str(b):
                diffs.append((gen.cell(r, c).coordinate, str(a), str(b)))
    # 仅允许 SEM520 两行 D 列申报要素的预期差异
    assert {d[0] for d in diffs} == {"D6", "D7"}, diffs
    for coord, a, b in diffs:
        assert "19600KG" in a and "20000KG" in b
    comm_art = next(a for a in resp["artifacts"] if "沟通表" in a["filename"])
    gen = openpyxl.load_workbook(comm_art["path"])["Sheet1"]
    ref = openpyxl.load_workbook(SAMPLE_COMM_OUTPUT)["Sheet1"]
    diffs = []
    for r in range(1, 7):
        for c in range(1, 48):
            a, b = gen.cell(r, c).value, ref.cell(r, c).value
            if a is None and b is None:
                continue
            if str(a) != str(b):
                diffs.append((gen.cell(r, c).coordinate, a, b))
    assert diffs == [], diffs


def test_style_fidelity_header_and_data(parts_request):
    """样式验收：字体/填充/边框/对齐/数字格式/行高/列宽/合并区与样例一致。"""
    resp = process_job(parts_request)
    gen_wb = openpyxl.load_workbook(resp["artifacts"][0]["path"])
    ref_wb = openpyxl.load_workbook(SAMPLE_PARTS_OUTPUT)
    gen, ref = gen_wb["导入模板"], ref_wb["导入模板"]
    for r in range(1, 21):
        for c in range(1, 42):
            assert _style_sig(gen.cell(r, c)) == _style_sig(ref.cell(r, c)), \
                f"样式差异 {gen.cell(r, c).coordinate}"
    assert [str(x) for x in gen.merged_cells.ranges] == \
           [str(x) for x in ref.merged_cells.ranges]
    for col in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        w1 = gen.column_dimensions[col].width if col in gen.column_dimensions else None
        w2 = ref.column_dimensions[col].width if col in ref.column_dimensions else None
        assert w1 == w2, col
    for r in range(1, 21):
        h1 = gen.row_dimensions[r].height if r in gen.row_dimensions else None
        h2 = ref.row_dimensions[r].height if r in ref.row_dimensions else None
        assert h1 == h2, r


def test_comm_comments_preserved(machine_request):
    resp = process_job(machine_request)
    art = next(a for a in resp["artifacts"] if "沟通表" in a["filename"])
    z = zipfile.ZipFile(art["path"])
    assert any("comment" in n.lower() for n in z.namelist())
    wb = openpyxl.load_workbook(art["path"])
    assert wb["Sheet1"]["A2"].comment is not None
    assert "填写规范" in wb["Sheet1"]["A2"].comment.text
