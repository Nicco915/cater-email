"""整机边界：双来源冲突、缺字段、未知机型、重复序列号、SO 规范化、重量组异常。"""
import shutil

import openpyxl
import pytest

from cater.core.api import process_job

from conftest import INPUT_MACHINE_XLSX
from xlsx_edit import edit_cell

TEXT_TABLE = (
    "Order\tDealer Order\tCust. Purch. Order (MSO#)\tDealer\tMaterial\tSerial Number\tTotal Sales Price\n"
    "{rows}"
)


def _text_mail(rows, message_id="<text-mail>"):
    lines = "\n".join("\t".join(r) for r in rows)
    return {"html": None, "text": TEXT_TABLE.format(rows=lines), "message_id": message_id}


SAMPLE_ROWS = [
    ("0620186623", "TSUJKT1371", "BFWVU", "JM28", "SEM520", "S8Z00583", "68,822.84"),
    ("0620186624", "TSUJKT1372", "BFWVW", "JM28", "SEM520", "S8Z00584", "68,822.84"),
    ("620186599", "TSUJKT1365", "BFWVK", "JM28", "SEM512", "GJW00836", "39,881.55"),
    ("0620186601", "TSUJKT1370", "BFWVN", "JM28", "SEM512", "GJW00835", "39,881.55"),
]


@pytest.fixture()
def machine_wb_copy(tmp_path):
    dst = tmp_path / "machine.xlsx"
    shutil.copy(INPUT_MACHINE_XLSX, dst)
    return dst


def _req(machine_request, rows=None, xlsx=None):
    machine_request["mail"] = _text_mail(rows if rows is not None else SAMPLE_ROWS)
    if xlsx is not None:
        machine_request["sources"] = [{"path": str(xlsx), "filename": xlsx.name,
                                       "mime_type": "xlsx"}]
    return machine_request


def test_text_mail_full_flow(machine_request):
    """纯文本业务表同样完成双来源合并；Order 在核对与存储时均补前导零。"""
    resp = process_job(_req(machine_request))
    assert resp["status"] == "ready", resp["issues"]
    rec = {r["record_key"]: r["fields"] for r in resp["records"]}
    assert rec["BFWVK"]["order"] == "0620186599"  #  intake 即规范化
    art = next(a for a in resp["artifacts"] if "沟通表" in a["filename"])
    wb = openpyxl.load_workbook(art["path"])
    ws = wb["Sheet1"]
    so_by_mso = {ws.cell(r, 4).value: ws.cell(r, 6).value for r in range(3, 7)}
    assert so_by_mso["BFWVK"] == "0620186599"   # 补前导零
    assert so_by_mso["BFWVU"] == "0620186623"   # 已有前导零保留
    assert so_by_mso["BFWVW"] == "0620186624"


def test_conflict_pauses_with_both_values(machine_request):
    """邮件序列号与箱单不一致 → 冲突暂停，列出两处原值，不静默覆盖。"""
    rows = [r if r[2] != "BFWVU" else
            ("0620186623", "TSUJKT1371", "BFWVU", "JM28", "SEM520", "S8Z99999", "68,822.84")
            for r in SAMPLE_ROWS]
    resp = process_job(_req(machine_request, rows))
    assert resp["status"] == "waiting_input"
    assert resp["artifacts"] == []
    conflicts = [i for i in resp["issues"] if i["code"] == "FIELD_CONFLICT"]
    assert conflicts
    c = conflicts[0]
    assert c["record_key"] == "BFWVU"
    assert c["field"] == "serial_number"
    assert "S8Z99999" in c["message"] and "S8Z00583" in c["message"]
    assert len(c["sources"]) >= 2


def test_complementary_sources_not_missing(machine_request):
    """来源互补（邮件无 PIN、箱单无价格）不属于缺失。"""
    resp = process_job(_req(machine_request))
    assert resp["status"] == "ready", resp["issues"]
    assert not any(i["code"] == "MISSING_FIELD" for i in resp["issues"])


def test_missing_mail_price_pauses(machine_request):
    rows = [r if r[2] != "BFWVU" else
            ("0620186623", "TSUJKT1371", "BFWVU", "JM28", "SEM520", "S8Z00583", "")
            for r in SAMPLE_ROWS]
    resp = process_job(_req(machine_request, rows))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "MISSING_FIELD" and i["record_key"] == "BFWVU"
               and "total_sales_price" in i["field"] for i in resp["issues"])


def test_unknown_model_pauses(machine_request):
    rows = [r if r[2] != "BFWVU" else
            ("0620186623", "TSUJKT1371", "BFWVU", "JM28", "SEM999", "S8Z00583", "68,822.84")
            for r in SAMPLE_ROWS]
    resp = process_job(_req(machine_request, rows))
    assert resp["status"] == "waiting_input"
    codes = {i["code"] for i in resp["issues"]}
    assert "UNKNOWN_MODEL" in codes or "FIELD_CONFLICT" in codes
    assert resp["artifacts"] == []


def test_duplicate_serial_pauses(machine_request):
    rows = [r if r[2] != "BFWVW" else
            ("0620186624", "TSUJKT1372", "BFWVW", "JM28", "SEM520", "S8Z00583", "68,822.84")
            for r in SAMPLE_ROWS]
    resp = process_job(_req(machine_request, rows))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] in ("DUPLICATE_SERIAL", "FIELD_CONFLICT") for i in resp["issues"])


def test_weight_group_qty_mismatch(machine_request, machine_wb_copy):
    edit_cell(machine_wb_copy, "Packing list", "D9", "3 UNITS\n(3 PACKAGE)")  # 组内只有 2 个 MSO
    resp = process_job(_req(machine_request, xlsx=machine_wb_copy))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "WEIGHT_GROUP_QTY_MISMATCH" for i in resp["issues"])


def test_mso_unmatched_pauses(machine_request):
    """邮件中多出一台箱单没有的机器 → 缺重量等字段，整票暂停。"""
    rows = SAMPLE_ROWS + [
        ("0620186625", "TSUJKT1373", "BFWVX", "JM28", "SEM520", "S8Z00585", "68,822.84")]
    resp = process_job(_req(machine_request, rows))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "MISSING_FIELD" and i["record_key"] == "BFWVX"
               for i in resp["issues"])
    assert resp["artifacts"] == []


def test_no_double_zero_so(machine_request):
    resp = process_job(_req(machine_request))
    assert resp["status"] == "ready", resp["issues"]
    art = next(a for a in resp["artifacts"] if "沟通表" in a["filename"])
    ws = openpyxl.load_workbook(art["path"])["Sheet1"]
    for r in range(3, 7):
        so = ws.cell(r, 6).value
        assert so.startswith("0") and not so.startswith("00")


def test_machine_missing_destination(machine_request):
    machine_request["destination"] = None
    resp = process_job(_req(machine_request))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "MISSING_DESTINATION" for i in resp["issues"])


def test_machine_override(machine_request):
    machine_request["overrides"] = [{
        "record_key": "BFWVU", "field": "order", "value": "620186623",
        "source_message_id": "<confirm@m>"}]
    resp = process_job(_req(machine_request))
    assert resp["status"] == "ready", resp["issues"]
    art = next(a for a in resp["artifacts"] if "沟通表" in a["filename"])
    ws = openpyxl.load_workbook(art["path"])["Sheet1"]
    assert ws.cell(3, 6).value == "0620186623"  # 覆盖值同样补零
    prov = next(p for p in resp["provenance"]
                if p["record_key"] == "BFWVU" and p["field"] == "order")
    assert prov["source"]["type"] == "override"
