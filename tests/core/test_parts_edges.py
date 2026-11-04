"""配件边界：选表失败、未知国家、M/D 冲突、缺目的国、公式缺缓存、覆盖修正。"""
import shutil

import openpyxl
import pytest

from cater.core.api import process_job

from conftest import INPUT_PARTS
from xlsx_edit import edit_cell


@pytest.fixture()
def parts_copy(tmp_path):
    dst = tmp_path / "parts.xlsx"
    shutil.copy(INPUT_PARTS, dst)
    return dst


def _request(parts_request, path):
    parts_request["sources"] = [{"path": str(path), "filename": path.name, "mime_type": "xlsx"}]
    return parts_request


def test_final_sheet_missing_pauses(parts_request, parts_copy):
    wb = openpyxl.load_workbook(parts_copy)
    del wb["CI-For Dealer (2)"]
    wb.save(parts_copy)
    resp = process_job(_request(parts_request, parts_copy))
    assert resp["status"] == "waiting_input"
    assert resp["artifacts"] == []
    assert any(i["code"] == "FINAL_SHEET_NOT_FOUND" for i in resp["issues"])


def test_multiple_final_sheets_pause(parts_request, parts_copy):
    wb = openpyxl.load_workbook(parts_copy)
    wb.copy_worksheet(wb["CI-For Dealer (2)"]).title = "CI-For Dealer (3)"
    wb.save(parts_copy)
    resp = process_job(_request(parts_request, parts_copy))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "AMBIGUOUS_FINAL_SHEET" for i in resp["issues"])


def test_unknown_coo_pauses(parts_request, parts_copy):
    edit_cell(parts_copy, "CI-For Dealer (2)", "E20", "XX")
    resp = process_job(_request(parts_request, parts_copy))
    assert resp["status"] == "waiting_input"
    issues = [i for i in resp["issues"] if i["code"] == "UNKNOWN_COUNTRY"]
    assert issues and issues[0]["record_key"] == "5181827"


def test_missing_destination_pauses(parts_request):
    parts_request["destination"] = None
    resp = process_job(parts_request)
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "MISSING_DESTINATION" for i in resp["issues"])
    assert resp["artifacts"] == []


def test_hs_d_column_conflict_m_wins(parts_request, parts_copy):
    """D 与 M 冲突时以 M 为准，非阻塞提示，不暂停整票。"""
    edit_cell(parts_copy, "CI-For Dealer (2)", "D20", "9999999999")
    resp = process_job(_request(parts_request, parts_copy))
    assert resp["status"] == "ready", resp["issues"]
    info = [i for i in resp["issues"] if i["code"] == "HS_CODE_DVERSUS_M"]
    assert info and info[0]["blocking"] is False
    row = next(r for r in resp["records"] if r["record_key"] == "5181827")
    assert row["row"]["hs_code"] == "7318151001"


def test_weight_mismatch_pauses(parts_request, parts_copy):
    edit_cell(parts_copy, "CI-For Dealer (2)", "L20", 999)  # H×K=8.4
    resp = process_job(_request(parts_request, parts_copy))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "WEIGHT_MISMATCH" and i["record_key"] == "5181827"
               for i in resp["issues"])


def test_formula_cache_missing(parts_request, parts_copy):
    """公式无缓存不能当零：登记问题且不使用该值。"""
    wb = openpyxl.load_workbook(parts_copy)  # 保留公式
    ws = wb["CI-For Dealer (2)"]
    ws["I20"] = "=1.24+0.04"
    wb.save(parts_copy)  # openpyxl 保存的公式无缓存值
    resp = process_job(_request(parts_request, parts_copy))
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "FORMULA_CACHE_MISSING" for i in resp["issues"])


def test_override_applied_with_audit(parts_request):
    parts_request["overrides"] = [{
        "record_key": "5181827", "field": "unit_price", "value": "1.29",
        "source_message_id": "<user-confirm@example.com>",
    }]
    resp = process_job(parts_request)
    assert resp["status"] == "ready", resp["issues"]
    row = next(r for r in resp["records"] if r["record_key"] == "5181827")
    assert row["row"]["unit_price"] == "1.29"
    prov = next(p for p in resp["provenance"]
                if p["record_key"] == "5181827" and p["field"] == "unit_price")
    assert prov["source"]["type"] == "override"
    assert prov["source"]["message_id"] == "<user-confirm@example.com>"


def test_override_unmatched_record_pauses(parts_request):
    parts_request["overrides"] = [{
        "record_key": "NOPE", "field": "unit_price", "value": "1",
        "source_message_id": "<x@y>",
    }]
    resp = process_job(parts_request)
    assert resp["status"] == "waiting_input"
    assert any(i["code"] == "OVERRIDE_UNMATCHED" for i in resp["issues"])


def test_zero_records_not_delivered(parts_request, parts_copy):
    for r in range(19, 34):
        edit_cell(parts_copy, "CI-For Dealer (2)", f"B{r}", "")
    resp = process_job(_request(parts_request, parts_copy))
    assert resp["status"] == "waiting_input"
    assert resp["artifacts"] == []
    assert any(i["code"] == "NO_RECORDS" for i in resp["issues"])


def test_unsupported_file(parts_request, tmp_path):
    bad = tmp_path / "note.txt"
    bad.write_text("not a workbook", encoding="utf-8")
    parts_request["sources"] = [{"path": str(bad), "filename": "note.txt", "mime_type": "text/plain"}]
    resp = process_job(parts_request)
    assert resp["status"] in ("waiting_input", "unsupported")
    assert resp["artifacts"] == []
    assert any(i["code"] in ("UNSUPPORTED_FILE", "MISSING_SOURCE") for i in resp["issues"])
