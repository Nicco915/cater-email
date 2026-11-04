"""机型库导入：仅读 Sheet1、destination_scope 双变体、字段映射、同 scope 冲突仍报错。

用户已确认规则（plan.md）：915F 目的国 BRA 用 Sheet1 A=巴西 的 123KW 记录，
其他国家用 A 空的 110KW 记录；两条都保留，按 destination_scope 区分，
匹配由 core 按 request.destination.alpha3 完成。
"""
import shutil

import openpyxl
import pytest

from cater.core.api import import_machine_reference, process_job
from cater.core.references import MachineIndex

from conftest import DB_REFERENCE, INPUT_MACHINE_XLSX
from xlsx_edit import edit_cell


def _scopes_915f(entries):
    return {e["destination_scope"]: e for e in entries if e["model"] == "915F"}


def test_real_database_imports_two_915f_scopes():
    """真实数据库：915F 两条共存（default 110KW + BRA 123KW），不抛错、不覆盖。"""
    entries = import_machine_reference(str(DB_REFERENCE))
    scopes = {}
    for e in entries:
        if e["model"] == "915F":
            scopes[e["destination_scope"]] = e
    assert set(scopes) == {"default", "BRA"}
    assert "110KW" in scopes["default"]["declaration"]
    assert "123KW" in scopes["BRA"]["declaration"]
    assert scopes["BRA"]["special"] == "巴西"
    assert scopes["default"]["special"] == ""
    assert scopes["default"]["source_row"] == 39
    assert scopes["BRA"]["source_row"] == 40
    # 全部条目契约字段
    for e in entries:
        assert set(("model", "english_name", "hs_code", "product_name", "declaration",
                    "special", "verification", "destination_scope", "source_row")) \
               <= set(e)
    # 其余机型不受 scope 影响，均为 default（巴西标记仅 915F 与 SEM919BRZ 两行）
    bra_models = {e["model"] for e in entries if e["destination_scope"] == "BRA"}
    assert bra_models == {"915F", "SEM919BRZ"}
    assert all(e["destination_scope"] == "default" for e in entries
               if e["destination_scope"] != "BRA")
    assert "SEM520" in {e["model"] for e in entries} and len(entries) > 50


def test_machine_index_scope_matching():
    """匹配按 destination.alpha3 选 scope；未命中回退 default。"""
    entries = import_machine_reference(str(DB_REFERENCE))
    idx = MachineIndex(entries)
    assert "123KW" in idx.match("915F", "BRA")["declaration"]
    assert "110KW" in idx.match("915F", "IDN")["declaration"]
    assert "110KW" in idx.match("915F", None)["declaration"]     # 无目的国回退 default
    assert "110KW" in idx.match("915F")["declaration"]
    assert idx.match("915F", "BRA")["destination_scope"] == "BRA"
    # 无 default 变体且 scope 未命中 → 无匹配（不猜测）
    idx2 = MachineIndex([{"model": "X9", "hs_code": "1", "destination_scope": "BRA"}])
    assert idx2.match("X9", "IDN") is None
    assert idx2.match("X9", "BRA") is not None


def test_process_job_915f_brazil_vs_other(machine_request, tmp_path):
    """端到端：同一 915F 邮件+箱单，目的国 BRA → 123KW 记录；IDN → 110KW 记录。"""
    entries = import_machine_reference(str(DB_REFERENCE))
    machine_request["references"]["machines"] = entries
    # 把工作簿中 BFWVN 的机型改为 915F（Invoice 行26/箱单行16，公式缓存一并改）
    wb_path = tmp_path / "machine_915f.xlsx"
    shutil.copy(INPUT_MACHINE_XLSX, wb_path)
    edit_cell(wb_path, "Invoice", "B26", "915F")
    edit_cell(wb_path, "Packing list", "B16", "915F")
    # 四台机器齐全（仅 BFWVN 为 915F），避免无关缺字段
    rows_all = [
        ("0620186623", "TSUJKT1371", "BFWVU", "JM28", "SEM520", "S8Z00583", "68,822.84"),
        ("0620186624", "TSUJKT1372", "BFWVW", "JM28", "SEM520", "S8Z00584", "68,822.84"),
        ("620186599", "TSUJKT1365", "BFWVK", "JM28", "SEM512", "GJW00836", "39,881.55"),
        ("0620186601", "TSUJKT1370", "BFWVN", "JM28", "915F", "GJW00835", "39,881.55"),
    ]
    from test_machine_edges import _req
    machine_request["sources"] = [{"path": str(wb_path), "filename": wb_path.name,
                                   "mime_type": "xlsx"}]
    for dest, kw in (({"name": "巴西", "alpha3": "BRA"}, "123KW"),
                     ({"name": "印度尼西亚", "alpha3": "IDN"}, "110KW")):
        machine_request["destination"] = dest
        resp = process_job(_req(machine_request, rows_all))
        assert resp["status"] == "ready", resp["issues"]
        art = next(a for a in resp["artifacts"] if "企业物料" in a["filename"])
        ws = openpyxl.load_workbook(art["path"])["企业物料"]
        decl = {ws.cell(r, 4).value for r in range(6, 10)}
        assert any(kw in d for d in decl), (dest, decl)
        assert ws.cell(9, 2).value == "8429209000"   # BFWVN 行 HS


def test_import_fields_and_sheet2_ignored(tmp_path):
    """字段取自 Sheet1 B/C/D/E/F；Sheet2 不读取。"""
    dst = tmp_path / "db.xlsx"
    shutil.copy(DB_REFERENCE, dst)
    wb = openpyxl.load_workbook(dst)
    wb["Sheet2"]["B2"] = "FAKE-MODEL-999"
    wb.save(dst)

    entries = import_machine_reference(str(dst))
    by_key = {(e["model"], e["destination_scope"]): e for e in entries}
    assert ("FAKE-MODEL-999", "default") not in by_key
    sem520 = by_key[("SEM520", "default")]
    assert sem520["hs_code"] == "8429401100"
    assert sem520["english_name"] == "SEM520 SOIL COMPACTOR"
    assert sem520["product_name"] == "压路机"
    assert "19600KG(机重≥18吨)" in sem520["declaration"]  # 原样提取
    assert isinstance(sem520["verification"], str) and sem520["verification"]
    assert _scopes_915f(entries)["BRA"]["verification"] == "HTS correct"
    sem512 = by_key[("SEM512", "default")]
    assert "12200KG" in sem512["declaration"]
    assert "SEM510" in {e["model"] for e in entries} and len(entries) > 50


def test_same_scope_duplicate_with_differing_content_raises(tmp_path):
    """同一 (机型, scope) 内申报资料不同仍抛错（唯一性的最后防线）。"""
    dst = tmp_path / "db.xlsx"
    shutil.copy(DB_REFERENCE, dst)
    wb = openpyxl.load_workbook(dst)
    ws = wb["Sheet1"]
    # 复制一行 915F default（A 空），把功率改成 130KW → 同 scope 冲突
    ws.insert_rows(41)
    for col, val in zip("BCDEF", ("915F", "915F MOTOR GRADER", "8429209000", "平地机",
                                  "1|0|是自推进发动机输出功率130KW|山工品牌、SEM品牌|915F|无|无|无")):
        ws[f"{col}41"] = val
    wb.save(dst)
    with pytest.raises(ValueError, match="915F"):
        import_machine_reference(str(dst))


def test_unresolvable_special_marker_raises(tmp_path):
    """A 列特殊标记无法映射为国家时抛错，不猜测 scope。"""
    dst = tmp_path / "db.xlsx"
    shutil.copy(DB_REFERENCE, dst)
    wb = openpyxl.load_workbook(dst)
    ws = wb["Sheet1"]
    ws["A5"] = "亚特兰蒂斯"   # 不存在国家
    wb.save(dst)
    with pytest.raises(ValueError, match="亚特兰蒂斯"):
        import_machine_reference(str(dst))
