"""来源附件挑选回归：缺席/读不了/读得到但不认识/多附件歧义 四类必须区分。

背景（Windows 生产缺陷）：新整机 .xls 发送两次，inputs 记录显示文件已
保存，核心却报“缺少整机发票箱单附件”。两个根因：
1. 运行环境缺 xlrd 时，读取失败被吞成 None，挑选落空后无条件追加
   MISSING_SOURCE（把“环境缺依赖”误报成“用户没附件”）；
2. 依赖装好后，两份内容不同的合格附件被当成“已选定”直接解析或误报，
   正确行为是 AMBIGUOUS_INPUT 暂停整票并列出候选文件名。

本文件全部使用合成数据（FAKE MSO / SYN 序列号），不接触真实样例。
"""

from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest

from cater.core.api import process_job

SYNTHETIC_MAIL_TABLE = (
    "以下为合成业务表（fixtures only）\n"
    "Dealer Code\tDealer Order\tCust. Purch. Order (MSO#)\tOrder\t"
    "Material\tSerial Number\tTotal Sales Price\n"
    "SYN01\tSYN-ORD-1\tFAKE001\t06200000101\tSEMTEST\tSYN000001A\t50000.00\n"
    "SYN02\tSYN-ORD-2\tFAKE002\t06200000201\tSEMTEST\tSYN000002B\t50000.00\n"
)

MACHINE_REF = {
    "model": "SEMTEST",
    "english_name": "SEMTEST WHEEL LOADER",
    "hs_code": "8429510000",
    "product_name": "装载机",
    "declaration": "1|0|合成申报要素|无|无|无|无|无|无",
    "destination_scope": "default",
}


def build_machine_xlsx(
    path: Path,
    *,
    sheets: tuple[str, str] = ("CI", "PL"),
    net: float = 24000.0,
    serials: tuple[str, str] = ("SYN000001A", "SYN000002B"),
    msos: tuple[str, str] = ("FAKE001", "FAKE002"),
    invoice_no: str = "SYN-TEST-0001",
) -> Path:
    """合成整机发票/箱单（结构仿 flexible_machine.xls，数据全合成）。"""
    book = openpyxl.Workbook()
    ci = book.active
    ci.title = sheets[0]
    ci.cell(9, 12, "INVOICE NO.: " + invoice_no)
    ci.cell(34, 3, f"CMOPS MSO#: {msos[0]}/{msos[1]}\nSEMTEST WHEEL LOADER")
    ci.cell(53, 1, f"NET WEIGHT: {net:g} KG")
    ci.cell(55, 1, "MACHINE INFORMATION:")
    for col, title in ((1, "ITEM#"), (2, "MODEL"), (5, "SERIAL #"),
                       (7, "ENGINE #"), (11, "PIN #"), (15, "MSO")):
        ci.cell(56, col, title)
    for row, (serial, mso, engine, pin) in (
        (57, (serials[0], msos[0], "SYNENG001", "SYNPIN0000001")),
        (58, (serials[1], msos[1], "SYNENG002", "SYNPIN0000002")),
    ):
        ci.cell(row, 1, row - 56)
        ci.cell(row, 2, "SEMTEST")
        ci.cell(row, 5, serial)
        ci.cell(row, 7, engine)
        ci.cell(row, 11, pin)
        ci.cell(row, 15, mso)
    pl = book.create_sheet(sheets[1])
    pl.cell(9, 14, "INVOICE NO.: " + invoice_no)
    pl.cell(28, 1, "DESCRIPTION OF GOODS")
    pl.cell(28, 13, "QTY")
    pl.cell(28, 16, "TOTAL WEIGHT/KGS")
    pl.cell(29, 16, "GROSS")
    pl.cell(29, 19, "NET")
    description = (
        f"CMOPS MSO#: {msos[0]}/{msos[1]}\nSEMTEST WHEEL LOADER\n"
        f"SN: {serials[0]}/{serials[1]}"
    )
    pl.cell(30, 1, description)
    pl.cell(30, 13, 2)
    pl.cell(30, 14, "UNITS")
    pl.cell(30, 16, net)
    pl.cell(30, 19, net)
    pl.cell(31, 1, "TOTAL")
    pl.cell(31, 13, 2)
    pl.cell(31, 16, net)
    pl.cell(31, 19, net)
    book.save(path)
    return path


def build_junk_xlsx(path: Path, sheet: str = "Data") -> Path:
    """可读但没有任何业务表的工作簿。"""
    book = openpyxl.Workbook()
    ws = book.active
    ws.title = sheet
    ws.cell(1, 1, "unrelated notes")
    ws.cell(2, 1, "nothing business here")
    book.save(path)
    return path


def make_request(tmp_path: Path, sources: list, *, kind: str = "machine",
                 mail: dict | None = None) -> dict:
    return {
        "task_id": "TEST-PICK-1",
        "kind": kind,
        "destination": {"name": "法国", "alpha3": "FRA"},
        "sources": sources,
        "mail": {"text": SYNTHETIC_MAIL_TABLE} if mail is None else mail,
        "references": {"machines": [dict(MACHINE_REF)], "countries": [],
                       "machine_aliases": {}},
        "output_dir": str(tmp_path / "out"),
        "overrides": [],
    }


def source_entry(path: Path) -> dict:
    return {"path": str(path), "filename": path.name,
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def codes(response: dict) -> set[str]:
    return {str(issue.get("code")) for issue in response["issues"]}


def test_missing_xlrd_reports_dependency_not_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺 xlrd 是部署问题：报 WORKBOOK_DEPENDENCY，绝不报“缺少附件”。"""
    monkeypatch.setitem(sys.modules, "xlrd", None)
    xls = tmp_path / "machine.xls"
    # OLE2 签名合法（确属旧版 xls），才能走到 xlrd 导入这一步
    xls.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    response = process_job(make_request(tmp_path, [source_entry(xls)]))
    assert response["status"] == "waiting_input"
    assert "WORKBOOK_DEPENDENCY" in codes(response)
    dependency = next(i for i in response["issues"]
                      if i["code"] == "WORKBOOK_DEPENDENCY")
    assert "xlrd" in dependency["message"]
    assert "无需重发" in dependency["required_action"]
    assert not any("发票箱单附件" in str(i.get("message"))
                   for i in response["issues"]), (
        "依赖缺失被误报成缺少附件"
    )


def test_invalid_xls_reports_unsupported_not_missing(tmp_path: Path) -> None:
    """损坏/无效的 .xls：报 UNSUPPORTED_FILE（输入问题），不报“缺少附件”。"""
    xls = tmp_path / "machine.xls"
    # OLE2 签名合法但内容损坏：xlrd 打开失败 → UnsupportedSourceError
    xls.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 256)
    response = process_job(make_request(tmp_path, [source_entry(xls)]))
    assert response["status"] == "waiting_input"
    assert "UNSUPPORTED_FILE" in codes(response)
    assert not any("发票箱单附件" in str(i.get("message"))
                   for i in response["issues"])


def test_absent_source_still_reports_missing(tmp_path: Path) -> None:
    """确实未附工作簿：保留 MISSING_SOURCE（回归保护，不得误删）。"""
    response = process_job(make_request(tmp_path, []))
    assert response["status"] == "waiting_input"
    assert any(i["code"] == "MISSING_SOURCE"
               and "发票箱单附件" in str(i.get("message"))
               for i in response["issues"])


def test_readable_unrecognized_not_missing(tmp_path: Path) -> None:
    """附件可读但没有业务表：报“未找到…工作表”，不报“缺少附件”。"""
    junk = build_junk_xlsx(tmp_path / "notes.xlsx")
    response = process_job(make_request(tmp_path, [source_entry(junk)]))
    assert response["status"] == "waiting_input"
    assert "UNSUPPORTED_FILE" in codes(response)
    assert any("未找到" in str(i.get("message")) for i in response["issues"])
    assert not any("发票箱单附件" in str(i.get("message"))
                   for i in response["issues"])


def test_renamed_machine_sheets_picked_by_structure(tmp_path: Path) -> None:
    """已确认的布局灵活性：业务表重命名（发票/装箱单）也能被选中并解析。"""
    renamed = build_machine_xlsx(tmp_path / "renamed.xlsx",
                                 sheets=("发票", "装箱单"))
    response = process_job(make_request(tmp_path, [source_entry(renamed)]))
    assert response["status"] == "ready", response["issues"]
    assert response["kind"] == "machine"
    assert len(response["records"]) == 2
    assert response["artifacts"], "ready 必须有产物"


def test_auto_classify_uses_structure_sniff(tmp_path: Path) -> None:
    """kind=auto：名称未命中时按机器信息表/重量表结构识别为整机。"""
    renamed = build_machine_xlsx(tmp_path / "renamed.xlsx",
                                 sheets=("发票", "装箱单"))
    response = process_job(make_request(tmp_path, [source_entry(renamed)],
                                        kind="auto"))
    assert response["kind"] == "machine"
    assert response["status"] == "ready", response["issues"]


def test_identical_bytes_twice_is_single_candidate(tmp_path: Path) -> None:
    """相同字节内容传两次（同名/换名）= 一个来源候选，不歧义、不重复输出。"""
    first = build_machine_xlsx(tmp_path / "machine.xlsx")
    second = tmp_path / "machine-again.xlsx"
    second.write_bytes(first.read_bytes())
    response = process_job(make_request(
        tmp_path,
        [source_entry(first),
         {"path": str(second), "filename": "machine-again.xlsx",
          "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}],
    ))
    assert response["status"] == "ready", response["issues"]
    assert "AMBIGUOUS_INPUT" not in codes(response)
    assert len(response["records"]) == 2
    assert len(response["artifacts"]) == len({a["filename"] for a in response["artifacts"]})


def test_different_contents_pause_with_candidate_filenames(tmp_path: Path) -> None:
    """两份内容不同的合格附件：AMBIGUOUS_INPUT 暂停整票，列出候选文件名，
    绝不同时报 MISSING_SOURCE。"""
    first = build_machine_xlsx(tmp_path / "machine-a.xlsx")
    second = build_machine_xlsx(tmp_path / "machine-b.xlsx", net=25000.0)
    response = process_job(make_request(
        tmp_path, [source_entry(first), source_entry(second)]))
    assert response["status"] == "waiting_input"
    ambiguous = [i for i in response["issues"] if i["code"] == "AMBIGUOUS_INPUT"]
    assert len(ambiguous) == 1, response["issues"]
    assert "machine-a.xlsx" in ambiguous[0]["message"]
    assert "machine-b.xlsx" in ambiguous[0]["message"]
    assert "联系维护人员" in ambiguous[0]["required_action"]
    assert not any(i["code"] == "MISSING_SOURCE" for i in response["issues"]), (
        "歧义时不得再误报缺少附件"
    )


def test_parts_ambiguous_is_single_issue(tmp_path: Path) -> None:
    """配件同样适用：两份 CI-For Dealer 只报一个 AMBIGUOUS_INPUT。"""
    def parts_file(name: str) -> Path:
        book = openpyxl.Workbook()
        ws = book.active
        ws.title = "CI-For Dealer (2)"
        ws.cell(1, 1, "synthetic parts invoice " + name)
        book.save(tmp_path / name)
        return tmp_path / name

    first, second = parts_file("parts-a.xlsx"), parts_file("parts-b.xlsx")
    request = make_request(tmp_path,
                           [source_entry(first), source_entry(second)],
                           kind="parts")
    response = process_job(request)
    assert response["status"] == "waiting_input"
    ambiguous = [i for i in response["issues"] if i["code"] == "AMBIGUOUS_INPUT"]
    assert len(ambiguous) == 1, response["issues"]
    assert "parts-a.xlsx" in ambiguous[0]["message"]
    assert "parts-b.xlsx" in ambiguous[0]["message"]
    assert not any(i["code"] == "MISSING_SOURCE" for i in response["issues"])


def test_renamed_workbook_copied_twice_is_one_candidate(tmp_path: Path) -> None:
    """重命名业务表的工作簿传两份相同字节：结构逐个探测后按内容去重，
    仍是一个来源候选（不能因为“无名称命中 + 多附件”整体跳过识别）。"""
    first = build_machine_xlsx(tmp_path / "book.xlsx",
                               sheets=("发票", "装箱单"))
    second = tmp_path / "book-copy.xlsx"
    second.write_bytes(first.read_bytes())
    response = process_job(make_request(
        tmp_path,
        [source_entry(first),
         {"path": str(second), "filename": "book-copy.xlsx",
          "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}],
    ))
    assert response["status"] == "ready", response["issues"]
    assert "AMBIGUOUS_INPUT" not in codes(response)
    assert len(response["records"]) == 2


def test_named_plus_renamed_valid_is_ambiguous(tmp_path: Path) -> None:
    """名称命中的合格附件 + 重命名业务表的合格附件（内容不同）：
    两个都是候选，必须 AMBIGUOUS_INPUT，不得静默选定名称命中的一份。"""
    named = build_machine_xlsx(tmp_path / "machine.xlsx", sheets=("CI", "PL"))
    renamed = build_machine_xlsx(tmp_path / "copy.xlsx",
                                 sheets=("发票", "装箱单"), net=25000.0)
    response = process_job(make_request(
        tmp_path, [source_entry(named), source_entry(renamed)]))
    assert response["status"] == "waiting_input"
    ambiguous = [i for i in response["issues"] if i["code"] == "AMBIGUOUS_INPUT"]
    assert len(ambiguous) == 1, response["issues"]
    assert "machine.xlsx" in ambiguous[0]["message"]
    assert "copy.xlsx" in ambiguous[0]["message"]
    assert not any(i["code"] == "MISSING_SOURCE" for i in response["issues"])


def test_two_renamed_different_valid_is_ambiguous(tmp_path: Path) -> None:
    """两份重命名但内容不同的合格附件：逐个结构探测后报歧义。"""
    first = build_machine_xlsx(tmp_path / "a.xlsx", sheets=("发票", "装箱单"))
    second = build_machine_xlsx(tmp_path / "b.xlsx",
                                sheets=("发票", "装箱单"), net=25000.0)
    response = process_job(make_request(
        tmp_path, [source_entry(first), source_entry(second)]))
    assert response["status"] == "waiting_input"
    ambiguous = [i for i in response["issues"] if i["code"] == "AMBIGUOUS_INPUT"]
    assert len(ambiguous) == 1, response["issues"]
    assert "a.xlsx" in ambiguous[0]["message"]
    assert "b.xlsx" in ambiguous[0]["message"]


def test_auto_missing_xlrd_without_mail_keeps_dependency_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kind=auto、无邮件正文、缺 xlrd：依赖错误必须存活，
    不得被“无法识别业务类型”吞掉。"""
    monkeypatch.setitem(sys.modules, "xlrd", None)
    xls = tmp_path / "machine.xls"
    xls.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    request = make_request(tmp_path, [source_entry(xls)], kind="auto")
    request["mail"] = {}
    response = process_job(request)
    assert response["status"] == "waiting_input"
    assert "WORKBOOK_DEPENDENCY" in codes(response)
    assert "UNSUPPORTED_BUSINESS" not in codes(response)
    dependency = next(i for i in response["issues"]
                      if i["code"] == "WORKBOOK_DEPENDENCY")
    assert "xlrd" in dependency["message"]
    assert "无需重发" in dependency["required_action"]


def test_auto_classify_threads_configured_aliases(tmp_path: Path) -> None:
    """kind=auto 的结构识别必须使用请求配置的别名：模型列名只在自定义
    别名中时，不带别名的探测无法识别，带别名才能识别为整机。为隔离
    机器信息表这一条识别路径，工作簿只含该表（无箱单重量表）。"""
    custom = build_machine_xlsx(tmp_path / "custom.xlsx",
                                sheets=("发票", "装箱单"))
    book = openpyxl.load_workbook(custom)
    book.remove(book["装箱单"])
    # 把 CI 表模型列标题改成只在自定义别名里出现的写法
    ci = book["发票"]
    ci.cell(56, 2, "设备型号")
    book.save(custom)
    # 无邮件正文，结构是唯一识别途径
    base = make_request(tmp_path, [source_entry(custom)], kind="auto")
    base["mail"] = {}
    without = process_job(base)
    assert without["kind"] == "unsupported"
    with_aliases = make_request(tmp_path, [source_entry(custom)], kind="auto")
    with_aliases["mail"] = {}
    with_aliases["header_aliases"] = {"model": ["设备型号"]}
    response = process_job(with_aliases)
    assert response["kind"] == "machine"
    # 无箱单重量表：净重缺失如实暂停（证明确实走了机器信息表识别路径）
    assert response["status"] == "waiting_input"
    assert "MISSING_FIELD" in codes(response)
