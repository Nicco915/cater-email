"""配件样例端到端：15 行明细、逐字段基准值、订单号留空、首条毛重/件数/发票号。

审核修正：数量/单价/净重/毛重/件数按数值存储，标识符按文本。
"""
from decimal import Decimal

import openpyxl

from cater.core.api import process_job
from cater.core.numbertext import to_decimal

from conftest import INPUT_PARTS


def test_parts_sample_ready(parts_request):
    resp = process_job(parts_request)
    assert resp["status"] == "ready", resp["issues"]
    assert resp["kind"] == "parts"
    assert len(resp["records"]) == 15
    assert resp["business_identity"] == "CQLUIL260812HCB-BEECS"
    assert resp["content_hash"]
    blocking = [i for i in resp["issues"] if i.get("blocking")]
    assert blocking == []
    assert len(resp["artifacts"]) == 1
    assert resp["artifacts"][0]["filename"] == "出口台账表体.xlsx"


def test_parts_sample_row_values(parts_request):
    resp = process_job(parts_request)
    assert resp["status"] == "ready", resp["issues"]
    rows = {r["record_key"]: r["row"] for r in resp["records"]}
    first = rows["1R0774"]
    assert first["qty"] == "4"
    assert first["currency"] == "USD"
    assert first["unit_price"] == "28.24"          # 最终表 I 列，非原表 28.08
    assert first["net_weight"] == "3.6"
    assert first["coo"] == "USA"                    # E 列 US → 三位码
    assert first["destination"] == "TGO"
    assert first["hs_code"] == "8421999090"
    assert first["product_name"] == "滤芯"
    # 首条三个整票值
    assert first["gross_weight"] == "317"
    assert first["packages"] == "2"
    assert first["invoice_no"] == "CQLUIL260812HCB-BEECS"
    # 订单号本期留空，禁止复制旧值 CBS20250423-219355A
    for row in rows.values():
        assert row["order_no"] == ""
        assert "gross_weight" not in row or row is first
    second = rows["5181827"]
    assert "gross_weight" not in second
    assert "packages" not in second
    assert "invoice_no" not in second
    # 二进制尾差归一：2.8000000000000003 → 2.8
    assert rows["5186595"]["net_weight"] == "2.8"
    # 样例 15 个单价与最终表一致（抽查边界行）
    assert rows["7D1577"]["unit_price"] == "157.79"
    assert rows["5984710"]["qty"] == "16"


def test_parts_spec_cleaning(parts_request):
    """仅开头 1||不适合进口报关 改为单竖线；其他连续竖线保持不变。"""
    resp = process_job(parts_request)
    rows = {r["record_key"]: r["row"] for r in resp["records"]}
    assert rows["5181827"]["spec"].startswith("1|不适合进口报关|碳素钢|1030 Mpa|")
    # 行 6534354：前缀不是 1||，保持原样（含开头分号等）
    assert rows["6534354"]["spec"].startswith("境内自主品牌|不适合进口报关|;")
    # 行 1R0774 中部连续竖线 ||| 保持
    assert "无型号|||用于工程机械液压系统" in rows["1R0774"]["spec"]


def test_parts_output_file_cells(parts_request):
    resp = process_job(parts_request)
    assert resp["status"] == "ready", resp["issues"]
    path = resp["artifacts"][0]["path"]
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["导入模板"]
    ws = wb["导入模板"]
    # 表头保护行未变
    assert ws.cell(5, 2).value == "商品料号"
    assert ws.cell(5, 26).value == "订单号"
    # 数据行
    assert ws.cell(6, 2).value == "1R0774"
    assert to_decimal(ws.cell(6, 11).value) == Decimal("317")   # K 毛重（数值）
    assert to_decimal(ws.cell(6, 12).value) == Decimal("2")     # L 件数（数值）
    assert ws.cell(6, 27).value == "CQLUIL260812HCB-BEECS"  # AA 发票号（文本）
    assert ws.cell(20, 2).value == "7D1577"
    # 无第 21 行数据
    assert ws.cell(21, 2).value is None
    # 订单号列无残留
    for r in range(6, 21):
        assert ws.cell(r, 26).value in (None, "")


def test_parts_provenance(parts_request):
    resp = process_job(parts_request)
    prov = resp["provenance"]
    p1 = [p for p in prov if p["record_key"] == "1R0774"]
    fields = {p["field"]: p["source"] for p in p1}
    assert fields["part_code"]["sheet"] == "CI-For Dealer (2)"
    assert fields["part_code"]["cell"] == "B19"
    assert fields["gross_weight"]["sheet"] == "Packing List"
    assert fields["invoice_no"]["sheet"] == "CI-For Dealer (2)"
    assert fields["destination"]["type"] == "user_input"
    assert fields["currency"]["type"] == "fixed"


def test_parts_kind_auto(parts_request):
    parts_request["kind"] = "auto"
    resp = process_job(parts_request)
    assert resp["kind"] == "parts"
    assert resp["status"] == "ready"
