"""整机样例端到端：4 台机器、双来源合并、双输出、预期差异（19600KG 原文/20000 重量）。

审核修正：金额/重量按数值存储（Excel 数值类型），标识符（SO/HS/MSO 等）按文本。
"""
import zipfile
from decimal import Decimal

import openpyxl

from cater.core.api import process_job
from cater.core.numbertext import to_decimal


def _num(cell):
    """单元格须为数值类型（非文本），返回 Decimal 便于比较。"""
    assert not isinstance(cell.value, str), f"应为数值存储：{cell.coordinate}={cell.value!r}"
    return to_decimal(cell.value)


def test_machine_sample_ready(machine_request):
    resp = process_job(machine_request)
    assert resp["status"] == "ready", resp["issues"]
    assert resp["kind"] == "machine"
    assert len(resp["records"]) == 4
    assert [r["record_key"] for r in resp["records"]] == ["BFWVU", "BFWVW", "BFWVK", "BFWVN"]
    assert resp["business_identity"] == "BFWVK|BFWVN|BFWVU|BFWVW|CQLTSU260811M"
    assert len(resp["artifacts"]) == 2
    names = sorted(a["filename"] for a in resp["artifacts"])
    assert names == ["SEM发运沟通表 上传.xlsx", "企业物料 上传.xlsx"]


def test_machine_merged_fields(machine_request):
    resp = process_job(machine_request)
    rec = {r["record_key"]: r["fields"] for r in resp["records"]}
    bfwvu = rec["BFWVU"]
    assert bfwvu["dealer_code"] == "JM28"
    assert bfwvu["dealer_order"] == "TSUJKT1371"
    assert bfwvu["order"] == "0620186623"
    assert bfwvu["material"] == "SEM520"
    assert bfwvu["serial_number"] == "S8Z00583"
    # Machine Price ← Total Sales Price 68,822.84；不是发票单价 66,757.84
    assert bfwvu["total_sales_price"] == "68822.84"
    # 箱单组 40000÷2，不是数据库申报要素中的 19600KG
    assert bfwvu["net_weight"] == "20000"
    assert rec["BFWVK"]["net_weight"] == "12200"   # 24400÷2
    assert rec["BFWVK"]["pin"] == "SEM00512TGJW00836"


def test_machine_materials_output(machine_request):
    resp = process_job(machine_request)
    art = next(a for a in resp["artifacts"] if a["filename"] == "企业物料 上传.xlsx")
    wb = openpyxl.load_workbook(art["path"])
    assert wb.sheetnames == ["企业物料"], "中间 Sheet2 与历史 Sheet1 须已移除"
    ws = wb["企业物料"]
    assert ws.cell(5, 1).value == "商品料号"
    rows = {}
    for r in range(6, 10):
        rows[ws.cell(r, 1).value] = r
    assert set(rows) == {"BFWVU", "BFWVW", "BFWVK", "BFWVN"}
    r520 = rows["BFWVU"]
    assert ws.cell(r520, 2).value == "8429401100"
    assert ws.cell(r520, 3).value == "压路机"
    # 预期差异：申报要素取数据库原文（含 19600KG），第二比例/单颗料净重按箱单为 20000
    assert "19600KG(机重≥18吨)" in ws.cell(r520, 4).value
    assert _num(ws.cell(r520, 13)) == Decimal("20000")
    assert _num(ws.cell(r520, 14)) == Decimal("20000")
    assert ws.cell(r520, 5).value == "台"
    assert ws.cell(r520, 8).value == "成品"
    assert _num(ws.cell(r520, 12)) == Decimal("1")
    assert ws.cell(r520, 16).value == "USD"
    assert ws.cell(r520, 17).value == "CHN"
    assert ws.cell(r520, 21).value == "SEM520"
    assert ws.cell(r520, 7).value == "SEM520 SOIL COMPACTOR"
    r512 = rows["BFWVK"]
    assert ws.cell(r512, 2).value == "8429401900"
    assert "12200KG" in ws.cell(r512, 4).value
    assert _num(ws.cell(r512, 13)) == Decimal("12200")


def test_machine_comm_output(machine_request):
    resp = process_job(machine_request)
    art = next(a for a in resp["artifacts"] if a["filename"] == "SEM发运沟通表 上传.xlsx")
    wb = openpyxl.load_workbook(art["path"])
    assert wb.sheetnames == ["Sheet1"], "中间 Sheet2 须已移除"
    ws = wb["Sheet1"]
    assert ws.cell(2, 1).value == "Dealer Code"
    assert ws.cell(3, 1).value == "JM28"
    assert ws.cell(3, 3).value == "TSUJKT1371"
    assert ws.cell(3, 4).value == "BFWVU"
    assert ws.cell(3, 5).value == "BFWVU"          # Material 列取 MSO
    assert ws.cell(3, 6).value == "0620186623"     # SO 按文本
    assert ws.cell(3, 7).value == "SEM520"         # Model ← 输入 Material
    assert ws.cell(3, 8).value == "S8Z00583"
    assert _num(ws.cell(3, 9)) == Decimal("68822.84")   # Machine Price 数值存储
    assert ws.cell(3, 22).value == "印度尼西亚"     # 国家 ← 本次用户国家中文名
    # 样例顺序：SEM520 两台在前
    assert [ws.cell(r, 4).value for r in range(3, 7)] == ["BFWVU", "BFWVW", "BFWVK", "BFWVN"]
    # 其余列留空
    assert ws.cell(3, 2).value is None
    assert ws.cell(3, 10).value is None
    # 旧残留（样例 G10 空格）不存在
    assert ws.cell(10, 7).value in (None, "")
    # 批注（表头填写规范）保留
    z = zipfile.ZipFile(art["path"])
    assert any("comment" in n.lower() for n in z.namelist())


def test_machine_provenance(machine_request):
    resp = process_job(machine_request)
    prov = resp["provenance"]
    p = [x for x in prov if x["record_key"] == "BFWVU"]
    fields = {x["field"]: x["source"] for x in p}
    assert fields["dealer_code"]["type"] == "mail_html"
    assert fields["net_weight"]["type"] == "computed"
    assert "40000" in str(fields["net_weight"])
    assert fields["hs_code"]["type"] == "reference"
    assert fields["country"]["type"] == "user_input"


def test_machine_ignores_quoted_old_version(machine_request):
    """引用区旧版业务表（Total Sales Price 69,847.84）必须被忽略。"""
    resp = process_job(machine_request)
    assert resp["status"] == "ready", resp["issues"]
    for r in resp["records"]:
        assert r["fields"]["total_sales_price"] != "69847.84"
        assert r["fields"]["total_sales_price"] != "40906.55"


def test_machine_kind_auto(machine_request):
    machine_request["kind"] = "auto"
    resp = process_job(machine_request)
    assert resp["kind"] == "machine"
    assert resp["status"] == "ready"
