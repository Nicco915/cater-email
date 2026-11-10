"""多票分票预览（inspect_sources）合成样例测试。

所有工作簿均由 openpyxl 现场构造（去标识化合成数据，非真实业务资料）；
xls 兼容性用仓库既有只读样例覆盖。断言针对行为契约（归并/冲突可见/
歧义保留/稳定性），不镜像实现细节。
"""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from cater.core.split_preview import inspect_sources

REPO_ROOT = Path(__file__).resolve().parents[2]
XLS_SAMPLE = REPO_ROOT / "input" / "整机" / "CI PL ---2X656D BIHWE-WF.xls"


# ---------------------------------------------------------------------------
# 合成工作簿构造
# ---------------------------------------------------------------------------


def _machine_invoice_sheet(ws, invoice_no, machines, amount):
    ws.cell(1, 1, f"INVOICE NO.: {invoice_no}")
    ws.cell(3, 1, "MODEL")
    ws.cell(3, 2, "MSO#")
    ws.cell(3, 3, "SERIAL#")
    ws.cell(3, 4, "PIN")
    ws.cell(3, 5, "ENGINE")
    for i, (mso, serial) in enumerate(machines):
        ws.cell(4 + i, 1, "TESTMODEL")
        ws.cell(4 + i, 2, mso)
        ws.cell(4 + i, 3, serial)
        ws.cell(4 + i, 4, f"PIN{serial}")
        ws.cell(4 + i, 5, f"ENG{i}")
    total_row = 4 + len(machines) + 1
    ws.cell(total_row, 1, "TOTAL AMOUNT (IN WORDS): SAY USD ONLY")
    ws.cell(total_row, 7, amount)


def _packing_sheet(ws, rows, total):
    ws.cell(2, 1, "DESCRIPTION OF GOODS")
    ws.cell(2, 2, "QTY")
    ws.cell(2, 3, "NET WEIGHT (KGS)")
    ws.cell(2, 4, "GROSS WEIGHT (KGS)")
    for i, (desc, qty, net, gross) in enumerate(rows):
        ws.cell(3 + i, 1, desc)
        ws.cell(3 + i, 2, qty)
        ws.cell(3 + i, 3, net)
        ws.cell(3 + i, 4, gross)
    total_row = 3 + len(rows)
    ws.cell(total_row, 1, "TOTAL")
    ws.cell(total_row, 2, total[0])
    ws.cell(total_row, 3, total[1])
    ws.cell(total_row, 4, total[2])


def make_machine_workbook(path: Path, invoice_no, machines, amount,
                          weight_rows, weight_total) -> Path:
    wb = openpyxl.Workbook()
    _machine_invoice_sheet(wb.active, invoice_no, machines, amount)
    wb.active.title = "Invoice"
    _packing_sheet(wb.create_sheet("Packing list"), weight_rows, weight_total)
    wb.save(path)
    return path


def make_invoice_only_workbook(path: Path, invoice_no, machines, amount) -> Path:
    wb = openpyxl.Workbook()
    _machine_invoice_sheet(wb.active, invoice_no, machines, amount)
    wb.active.title = "Invoice"
    wb.save(path)
    return path


def make_packing_only_workbook(path: Path, invoice_no, weight_rows,
                               weight_total) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Packing list"
    ws.cell(1, 1, f"INVOICE NO.: {invoice_no}")
    _packing_sheet(ws, weight_rows, weight_total)
    wb.save(path)
    return path


def make_machine_table_only_workbook(path: Path, machines) -> Path:
    """只有机器信息表（无发票号文本）的附件。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.cell(1, 1, "MODEL")
    ws.cell(1, 2, "MSO#")
    ws.cell(1, 3, "SERIAL#")
    for i, (mso, serial) in enumerate(machines):
        ws.cell(2 + i, 1, "TESTMODEL")
        ws.cell(2 + i, 2, mso)
        ws.cell(2 + i, 3, serial)
    wb.save(path)
    return path


def make_kit_workbook(path: Path, invoice_no, order_code, app_rows,
                      *, residue_code=None, bl_invoice=None,
                      amount=20, net=50, sets=2, packages=1,
                      gross=110, tare=10) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice "
    ws.cell(1, 1, f"INVOICE NO.: {invoice_no}")
    headers = ["Health Care Kit No", "CHN HS Code", "COO", "DESCRIPTION OF GOODS",
               "Qty/SET", "SET QTY", "TOTAL Qty", "U'Price", "T'Price"]
    for c, text in enumerate(headers, start=1):
        ws.cell(3, c, text)
    ws.cell(4, 11, "总净重")
    ws.cell(5, 1, "KIT-X")
    ws.cell(5, 4, "PART-A")
    ws.cell(5, 9, amount)
    ws.cell(6, 1, "TOTAL AMOUNT (IN WORDS): SAY USD ONLY")
    ws.cell(6, 9, amount)
    ws.cell(6, 11, net)
    ws.cell(8, 1, "Health care kit for below machines:")
    app_headers = ["KIT", "PO", "Order Code", "MSO", "Machine Model",
                   "Machine Serial No"]
    for c, text in enumerate(app_headers, start=1):
        ws.cell(9, c, text)
    for i, (mso, serial) in enumerate(app_rows):
        ws.cell(10 + i, 1, "KIT-X")
        ws.cell(10 + i, 2, f"PO-{i}")
        ws.cell(10 + i, 3, order_code)
        ws.cell(10 + i, 4, mso)
        ws.cell(10 + i, 5, "TESTMODEL")
        ws.cell(10 + i, 6, serial)
    residue_row = 10 + len(app_rows)
    if residue_code:
        ws.cell(residue_row, 3, residue_code)

    pl = wb.create_sheet("Packing list")
    pl.cell(2, 1, "KIT")
    pl.cell(2, 2, "QTY")
    pl.cell(2, 3, "Packages")
    pl.cell(2, 4, "Weight")
    pl.cell(3, 1, "KIT-X")
    pl.cell(3, 2, sets)
    pl.cell(3, 3, packages)
    pl.cell(3, 4, gross - tare)
    pl.cell(4, 1, "TARE WEIGHT")
    pl.cell(4, 4, tare)
    pl.cell(5, 1, "TOTAL")
    pl.cell(5, 2, sets)
    pl.cell(5, 3, packages)
    pl.cell(5, 4, gross)
    pl.cell(7, 1, "DESCRIPTION")
    pl.cell(7, 2, "NET WEIGHT")
    pl.cell(8, 1, "PART-A")
    pl.cell(8, 2, net)
    pl.cell(9, 1, "TOTAL")
    pl.cell(9, 2, net)

    bl = wb.create_sheet("BL")
    bl.cell(1, 1, "BILL OF LADING")
    if bl_invoice:
        bl.cell(3, 1, f"INVOICE NO.: {bl_invoice}")
    wb.save(path)
    return path


def _inspect(paths, mail=None):
    return inspect_sources({
        "sources": [{"path": str(p), "filename": p.name} for p in paths],
        "mail": mail or {},
    })


def _issue_codes(candidate):
    return [i["code"] for i in candidate["issues"]]


def _by_invoice(preview, invoice_no):
    for cand in preview["candidates"]:
        if invoice_no in cand["identity"].get("invoice_no", []):
            return cand
    raise AssertionError(f"候选缺失：{invoice_no}")


# ---------------------------------------------------------------------------
# 整机多票
# ---------------------------------------------------------------------------


def test_multi_ticket_split_attribution(tmp_path):
    a_inv = make_machine_workbook(
        tmp_path / "a-invoice.xlsx", "INV-A-001", [("MSOA1", "S000001")],
        1000, [("MACHINE A", 1, 5000, 5200)], (1, 5000, 5200))
    a_pl = make_packing_only_workbook(
        tmp_path / "a-packing.xlsx", "INV-A-001",
        [("MACHINE A", 1, 5000, 5200)], (1, 5000, 5200))
    b_inv = make_machine_workbook(
        tmp_path / "b-invoice.xlsx", "INV-B-001", [("MSOB1", "S000002")],
        2000, [("MACHINE B", 2, 8000, 8300)], (2, 8000, 8300))
    preview = _inspect([a_inv, a_pl, b_inv])
    assert preview["candidate_count"] == 2
    cand_a = _by_invoice(preview, "INV-A-001")
    cand_b = _by_invoice(preview, "INV-B-001")
    # 分文件合并：A 票由两份附件组成，事实来源跨文件且带溯源
    assert len(cand_a["source_refs"]) == 2
    assert cand_a["facts"]["net_weight"]["value"] == "5000"
    assert {s["sheet"] for s in cand_a["facts"]["net_weight"]["sources"]} == {
        "Packing list"}
    assert cand_a["facts"]["amount"]["value"] == "1000"
    # 不跨票混取：B 票的重量/金额独立
    assert cand_b["facts"]["amount"]["value"] == "2000"
    assert cand_b["facts"]["gross_weight"]["value"] == "8300"
    assert cand_b["identity"]["mso"] == ["MSOB1"]
    assert cand_a["identity"]["mso"] == ["MSOA1"]
    assert preview["batch_ready"] is True


def test_candidate_id_stable_under_reorder_and_rename(tmp_path):
    p1 = make_machine_workbook(
        tmp_path / "one.xlsx", "INV-S-001", [("MSOS1", "S000101")],
        100, [("M", 1, 100, 110)], (1, 100, 110))
    p2 = make_machine_workbook(
        tmp_path / "two.xlsx", "INV-S-002", [("MSOS2", "S000102")],
        200, [("M", 2, 200, 220)], (2, 200, 220))
    first = _inspect([p1, p2])
    renamed = tmp_path / "renamed"
    renamed.mkdir()
    q2 = renamed / "zzz-renamed.xlsx"
    q1 = renamed / "aaa-renamed.xlsx"
    q2.write_bytes(p2.read_bytes())
    q1.write_bytes(p1.read_bytes())
    second = _inspect([q2, q1])  # 改名 + 反序
    ids_first = sorted(c["candidate_id"] for c in first["candidates"])
    ids_second = sorted(c["candidate_id"] for c in second["candidates"])
    assert ids_first == ids_second
    assert first["source_fingerprint"] == second["source_fingerprint"]


def test_byte_duplicate_deduped(tmp_path):
    p1 = make_machine_workbook(
        tmp_path / "orig.xlsx", "INV-D-001", [("MSOD1", "S000201")],
        100, [("M", 1, 100, 110)], (1, 100, 110))
    dup = tmp_path / "forwarded-copy.xlsx"
    dup.write_bytes(p1.read_bytes())
    preview = _inspect([p1, dup])
    assert preview["candidate_count"] == 1
    cand = preview["candidates"][0]
    assert len(cand["source_refs"]) == 1
    assert sorted(cand["source_refs"][0]["filenames"]) == [
        "forwarded-copy.xlsx", "orig.xlsx"]


def test_same_invoice_conflict_visible(tmp_path):
    p1 = make_machine_workbook(
        tmp_path / "v1.xlsx", "INV-C-001", [("MSOC1", "S000301")],
        1000, [("M", 1, 100, 110)], (1, 100, 110))
    p2 = make_machine_workbook(
        tmp_path / "v2.xlsx", "INV-C-001", [("MSOC1", "S000301")],
        1500, [("M", 1, 100, 110)], (1, 100, 110))
    preview = _inspect([p1, p2])
    assert preview["candidate_count"] == 1
    cand = preview["candidates"][0]
    amount = cand["facts"]["amount"]
    assert amount["conflict"] is True
    assert amount["value"] is None
    # 两个版本并列展示，不吞掉任何一版
    assert sorted(v["value"] for v in amount["variants"]) == ["1000", "1500"]
    assert "CANDIDATE_FACT_CONFLICT" in _issue_codes(cand)
    assert cand["needs_confirmation"] is True
    assert preview["batch_ready"] is False


def test_bad_sources_visible_without_blocking_others(tmp_path):
    good = make_machine_workbook(
        tmp_path / "good.xlsx", "INV-G-001", [("MSOG1", "S000401")],
        100, [("M", 1, 100, 110)], (1, 100, 110))
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"this is not a real workbook")
    fake_suffix = tmp_path / "notes.pdf"
    fake_suffix.write_bytes(b"%PDF-1.4 fake")
    empty = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(empty)
    preview = _inspect([broken, good, fake_suffix, empty])
    assert preview["candidate_count"] == 1
    assert preview["candidates"][0]["identity"]["invoice_no"] == ["INV-G-001"]
    statuses = {}
    for s in preview["unmatched_sources"]:
        for name in ([s["filename"]] if "filename" in s
                     else s.get("filenames", [])):
            statuses[name] = s["status"]
    assert statuses["broken.xlsx"] == "unreadable"
    assert statuses["notes.pdf"] == "not_workbook"
    assert statuses["empty.xlsx"] == "empty"
    # 坏附件可见且批次不得被标记为可直接执行
    assert preview["batch_ready"] is False
    top_codes = [i["code"] for i in preview["issues"]]
    assert "UNSUPPORTED_FILE" in top_codes
    assert "SOURCE_IDENTITY_NOT_FOUND" in top_codes


def test_xls_sample_supported():
    preview = _inspect([XLS_SAMPLE])
    assert preview["candidate_count"] == 1
    cand = preview["candidates"][0]
    assert cand["kind"] == "machine"
    assert cand["identity"]["mso"] == ["BIHWE", "BIHWF"]
    assert cand["identity"]["serial"] == ["S5307023", "S5307024"]
    assert cand["facts"]["net_weight"]["value"] == "34600"


# ---------------------------------------------------------------------------
# 证据强度：弱字段不得桥接合票
# ---------------------------------------------------------------------------


def test_shared_weak_identity_does_not_merge_distinct_invoices(tmp_path):
    """两票明确不同发票号共享同一 MSO：保持分票，冲突可见。"""
    p1 = make_machine_workbook(
        tmp_path / "t1.xlsx", "INV-W-001", [("SHARED", "S000501")],
        100, [("M", 1, 100, 110)], (1, 100, 110))
    p2 = make_machine_workbook(
        tmp_path / "t2.xlsx", "INV-W-002", [("SHARED", "S000502")],
        200, [("M", 2, 200, 220)], (2, 200, 220))
    preview = _inspect([p1, p2])
    assert preview["candidate_count"] == 2
    for inv in ("INV-W-001", "INV-W-002"):
        cand = _by_invoice(preview, inv)
        assert "WEAK_IDENTITY_ACROSS_TICKETS" in _issue_codes(cand)
        assert cand["needs_confirmation"] is True


def test_no_invoice_attachment_matching_multiple_tickets_stays_ambiguous(tmp_path):
    """无发票号附件同时命中两票：保留歧义，不充当桥接节点。"""
    t1 = make_invoice_only_workbook(
        tmp_path / "t1.xlsx", "INV-N-001", [("MSON1", "S000601")], 100)
    t2 = make_invoice_only_workbook(
        tmp_path / "t2.xlsx", "INV-N-002", [("MSON2", "S000602")], 200)
    stray = make_machine_table_only_workbook(
        tmp_path / "stray.xlsx", [("MSON1", "S000601"), ("MSON2", "S000602")])
    preview = _inspect([t1, t2, stray])
    assert preview["candidate_count"] == 3
    cand_t1 = _by_invoice(preview, "INV-N-001")
    cand_t2 = _by_invoice(preview, "INV-N-002")
    # 两票未被桥接合票
    assert cand_t1["candidate_id"] != cand_t2["candidate_id"]
    assert len(cand_t1["source_refs"]) == 1
    assert len(cand_t2["source_refs"]) == 1
    stray_cand = next(c for c in preview["candidates"]
                      if not c["identity"].get("invoice_no"))
    assert "ATTACHMENT_MATCHES_MULTIPLE_TICKETS" in _issue_codes(stray_cand)
    assert stray_cand["needs_confirmation"] is True


def test_no_invoice_attachment_attaches_to_unique_ticket(tmp_path):
    inv = make_invoice_only_workbook(
        tmp_path / "inv.xlsx", "INV-U-001", [("MSOU1", "S000701")], 100)
    extra = make_machine_table_only_workbook(
        tmp_path / "extra.xlsx", [("MSOU1", "S000701")])
    preview = _inspect([inv, extra])
    assert preview["candidate_count"] == 1
    cand = preview["candidates"][0]
    assert len(cand["source_refs"]) == 2


# ---------------------------------------------------------------------------
# 配件 Health Care Kit
# ---------------------------------------------------------------------------


def test_kit_workbook_residue_and_stale_bl(tmp_path):
    kit = make_kit_workbook(
        tmp_path / "kit.xlsx", "KIT-INV-001", "QX1",
        [("MKIT1", "S000801"), ("MKIT2", "S000802")],
        residue_code="QX0", bl_invoice="KIT-OLD-999")
    preview = _inspect([kit])
    assert preview["candidate_count"] == 1
    cand = preview["candidates"][0]
    assert cand["kind"] == "parts"
    # 模板残留的 Order Code 不进入身份
    assert cand["identity"]["order_code"] == ["QX1"]
    assert cand["identity"]["invoice_no"] == ["KIT-INV-001"]
    codes = _issue_codes(cand)
    assert "TEMPLATE_RESIDUE_ROWS" in codes
    assert "STALE_BL_INVOICE" in codes
    # 皮重作为独立事实保留，毛重=箱单 TOTAL（含皮重），不重复加
    assert cand["facts"]["gross_weight"]["value"] == "110"
    assert cand["facts"]["tare_weight"]["value"] == "10"
    assert cand["facts"]["sets"]["value"] == "2"
    assert cand["facts"]["packages"]["value"] == "1"
    checks = {c["rule"]: c["result"] for c in cand["checks"]}
    assert checks.get("PREVIEW_TARE_INCLUDED") == "pass"
    assert checks.get("PREVIEW_KIT_NET_CROSSCHECK") == "pass"


def test_kit_tickets_split_by_order_code(tmp_path):
    k1 = make_kit_workbook(tmp_path / "k1.xlsx", "KIT-INV-101", "QX1",
                           [("MK1", "S000901")])
    k2 = make_kit_workbook(tmp_path / "k2.xlsx", "KIT-INV-102", "QX2",
                           [("MK2", "S000902")])
    preview = _inspect([k1, k2])
    assert preview["candidate_count"] == 2
    assert _by_invoice(preview, "KIT-INV-101")["identity"]["order_code"] == ["QX1"]
    assert _by_invoice(preview, "KIT-INV-102")["identity"]["order_code"] == ["QX2"]


# ---------------------------------------------------------------------------
# 正文业务行关联
# ---------------------------------------------------------------------------


def _mail_html(rows):
    body = "".join(
        f"<tr><td>{mso}</td><td>{serial}</td><td>{price}</td></tr>"
        for mso, serial, price in rows)
    return ("<html><body><p>Please find the order below.</p>"
            "<table><tr><td>MSO</td><td>Serial Number</td>"
            "<td>Total Sales Price</td></tr>"
            f"{body}</table></body></html>")


def test_mail_rows_matched_and_unmatched(tmp_path):
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "INV-M-001", [("MSOM1", "S001001")],
        1000, [("M", 1, 100, 110)], (1, 100, 110))
    p2 = make_machine_workbook(
        tmp_path / "m2.xlsx", "INV-M-002", [("MSOM2", "S001002")],
        2000, [("M", 2, 200, 220)], (2, 200, 220))
    mail = {"html": _mail_html([("MSOM1", "S001001", 1000),
                                ("MSOM2", "S001002", 2000),
                                ("MSOXX", "S009999", 3000)])}
    preview = _inspect([p1, p2], mail=mail)
    assert preview["candidate_count"] == 2
    cand1 = _by_invoice(preview, "INV-M-001")
    cand2 = _by_invoice(preview, "INV-M-002")
    assert len(cand1["mail_row_refs"]) == 1
    assert len(cand2["mail_row_refs"]) == 1
    assert len(preview["unmatched_mail_rows"]) == 1
    assert preview["unmatched_mail_rows"][0]["mso"] == "MSOXX"
    # 正文金额与附件金额一致时不产生冲突
    assert not cand1["needs_confirmation"]
    assert preview["batch_ready"] is True


def test_mail_row_serial_conflict_blocks(tmp_path):
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "INV-MC-001", [("MSOMC", "S001101")],
        1000, [("M", 1, 100, 110)], (1, 100, 110))
    mail = {"html": _mail_html([("MSOMC", "S009998", 1000)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "SERIAL_CONFLICT" in _issue_codes(cand)
    assert cand["needs_confirmation"] is True
    assert preview["batch_ready"] is False


def test_mail_amount_conflict_visible_price_untouched(tmp_path):
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "INV-MA-001", [("MSOMA", "S001201")],
        1000, [("M", 1, 100, 110)], (1, 100, 110))
    mail = {"html": _mail_html([("MSOMA", "S001201", 9999)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "AMOUNT_CONFLICT" in _issue_codes(cand)
    # 金额差异只标记不修改：附件金额原样保留，核对记录可见两侧取值
    assert cand["facts"]["amount"]["value"] == "1000"
    assert cand["facts"]["amount"]["conflict"] is False
    checks = {c["rule"]: c for c in cand["checks"]}
    assert checks["PREVIEW_MAIL_AMOUNT"]["result"] == "fail"
    assert "9999" in checks["PREVIEW_MAIL_AMOUNT"]["detail"]
    assert cand["needs_confirmation"] is True
    assert preview["batch_ready"] is False


def test_mail_amount_full_coverage_multi_machine(tmp_path):
    """同票两机、正文两行齐全：按票聚合 100+200=300 与整票金额核对通过。"""
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "SYN-INV-01",
        [("SYNMSO1", "S100001"), ("SYNMSO2", "S100002")],
        300, [("M", 2, 100, 110)], (2, 100, 110))
    mail = {"html": _mail_html([("SYNMSO1", "S100001", 100),
                                ("SYNMSO2", "S100002", 200)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "AMOUNT_CONFLICT" not in _issue_codes(cand)
    assert "MAIL_AMOUNT_INCOMPLETE" not in _issue_codes(cand)
    checks = {c["rule"]: c for c in cand["checks"]}
    assert checks["PREVIEW_MAIL_AMOUNT"]["result"] == "pass"
    assert preview["batch_ready"] is True


def test_mail_amount_partial_coverage_blocked(tmp_path):
    """同票两机、正文只有一行：不得把 100 当整票，显式问题且不伪造 pass。"""
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "SYN-INV-02",
        [("SYNMSO1", "S100011"), ("SYNMSO2", "S100012")],
        300, [("M", 2, 100, 110)], (2, 100, 110))
    mail = {"html": _mail_html([("SYNMSO1", "S100011", 100)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "MAIL_AMOUNT_INCOMPLETE" in _issue_codes(cand)
    assert "AMOUNT_CONFLICT" not in _issue_codes(cand)
    assert not any(c["rule"] == "PREVIEW_MAIL_AMOUNT"
                   for c in cand["checks"])
    # 附件金额原样保留，未被部分行覆盖
    assert cand["facts"]["amount"]["value"] == "300"
    assert cand["needs_confirmation"] is True
    assert preview["batch_ready"] is False


def test_mail_amount_duplicate_rows_blocked(tmp_path):
    """正文两行指向同一机器：重复显式可见，不做金额核对。"""
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "SYN-INV-03",
        [("SYNMSO1", "S100021"), ("SYNMSO2", "S100022")],
        300, [("M", 2, 100, 110)], (2, 100, 110))
    mail = {"html": _mail_html([("SYNMSO1", "S100021", 100),
                                ("SYNMSO1", "S100021", 200)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    codes = _issue_codes(cand)
    assert "MAIL_ROW_DUPLICATE" in codes
    assert "MAIL_AMOUNT_INCOMPLETE" in codes
    assert not any(c["rule"] == "PREVIEW_MAIL_AMOUNT"
                   for c in cand["checks"])
    assert preview["batch_ready"] is False


def test_mail_amount_adopted_only_on_full_coverage(tmp_path):
    """附件缺金额：全量覆盖才采用正文合计；部分覆盖留空且显式问题。"""
    full_wb = make_machine_table_only_workbook(
        tmp_path / "full.xlsx", [("SYNMSO1", "S100031"), ("SYNMSO2", "S100032")])
    mail_full = {"html": _mail_html([("SYNMSO1", "S100031", 100),
                                     ("SYNMSO2", "S100032", 200)])}
    preview = _inspect([full_wb], mail=mail_full)
    cand = preview["candidates"][0]
    assert cand["facts"]["amount"]["value"] == "300"
    assert cand["facts"]["amount"]["conflict"] is False
    # 采用值只来自正文行来源
    assert all(s["type"] == "mail_html"
               for s in cand["facts"]["amount"]["sources"])

    part_wb = make_machine_table_only_workbook(
        tmp_path / "part.xlsx", [("SYNMSO3", "S100033"), ("SYNMSO4", "S100034")])
    mail_part = {"html": _mail_html([("SYNMSO3", "S100033", 100)])}
    preview2 = _inspect([part_wb], mail=mail_part)
    cand2 = preview2["candidates"][0]
    assert "amount" not in cand2["facts"]
    assert "MAIL_AMOUNT_INCOMPLETE" in _issue_codes(cand2)


def test_serial_fallback_conflicting_mso_blocked(tmp_path):
    """附件已有明确 MSO：序列号兜底命中的冲突 MSO 不得并入身份。"""
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "SYN-INV-04", [("SYNMSO1", "S100041")],
        100, [("M", 1, 100, 110)], (1, 100, 110))
    mail = {"html": _mail_html([("DIFFERENT-MSO", "S100041", 100)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "MSO_CONFLICT" in _issue_codes(cand)
    # 身份未被污染
    assert cand["identity"]["mso"] == ["SYNMSO1"]
    assert cand["needs_confirmation"] is True
    assert preview["batch_ready"] is False


def test_cross_mso_serial_within_ticket_blocked(tmp_path):
    """同票多机：正文行 MSO 与序列号分属不同机器 → 行内交叉显式可见。"""
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "SYN-INV-05",
        [("SYNMSO1", "S100051"), ("SYNMSO2", "S100052")],
        300, [("M", 2, 100, 110)], (2, 100, 110))
    mail = {"html": _mail_html([("SYNMSO1", "S100052", 100)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "MSO_SERIAL_CROSS" in _issue_codes(cand)
    assert cand["needs_confirmation"] is True
    assert preview["batch_ready"] is False


def make_no_mso_workbook(path: Path, invoice_no, serial, amount) -> Path:
    """旧 CI 布局：机器信息区无 MSO 列，仅机型 + 序列号形态值。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.cell(1, 1, f"INVOICE NO.: {invoice_no}")
    ws.cell(3, 1, "MODEL")
    ws.cell(3, 2, "SERIAL#")
    ws.cell(4, 1, "TESTMODEL")
    ws.cell(4, 2, serial)
    ws.cell(6, 1, "TOTAL AMOUNT (IN WORDS): SAY USD ONLY")
    ws.cell(6, 7, amount)
    _packing_sheet(wb.create_sheet("Packing list"),
                   [("MACHINE", 1, 100, 110)], (1, 100, 110))
    wb.save(path)
    return path


def test_no_mso_attachment_adopts_mso_from_unique_serial_match(tmp_path):
    """附件确实无 MSO：唯一序列号完整匹配可补认 MSO（1434 正常兜底保留）。"""
    p1 = make_no_mso_workbook(tmp_path / "ci.xlsx", "SYN-INV-06",
                              "S100061", 100)
    mail = {"html": _mail_html([("ADOPTED-MSO", "S100061", 100)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert cand["identity"]["mso"] == ["ADOPTED-MSO"]
    assert "MSO_CONFLICT" not in _issue_codes(cand)
    assert cand["mail_row_refs"]
    assert preview["batch_ready"] is True


def test_mixed_key_forms_same_machine_counts_once(tmp_path):
    """复审复现：全字段行 + 仅 MSO 行指向同一台机器，不得算成两台。"""
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "SYN-REVIEW-01",
        [("MSOONE", "S100001"), ("MSOTWO", "S100002")],
        300, [("M", 2, 100, 110)], (2, 100, 110))
    mail = {"html": _mail_html([("MSOONE", "S100001", 100),
                                ("MSOONE", "", 200)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    codes = _issue_codes(cand)
    assert "MAIL_ROW_DUPLICATE" in codes
    assert "MAIL_AMOUNT_INCOMPLETE" in codes
    assert not any(c["rule"] == "PREVIEW_MAIL_AMOUNT"
                   for c in cand["checks"])
    assert preview["batch_ready"] is False


def test_mixed_key_forms_full_coverage_passes(tmp_path):
    """混合标识形式真正覆盖两台：全字段行 + 仅 MSO 行各指一台 → 正常通过。"""
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "SYN-REVIEW-02",
        [("MSOONE", "S100001"), ("MSOTWO", "S100002")],
        300, [("M", 2, 100, 110)], (2, 100, 110))
    mail = {"html": _mail_html([("MSOONE", "S100001", 100),
                                ("MSOTWO", "", 200)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "AMOUNT_CONFLICT" not in _issue_codes(cand)
    assert "MAIL_AMOUNT_INCOMPLETE" not in _issue_codes(cand)
    checks = {c["rule"]: c for c in cand["checks"]}
    assert checks["PREVIEW_MAIL_AMOUNT"]["result"] == "pass"
    assert preview["batch_ready"] is True


def test_serial_only_rows_full_coverage_passes(tmp_path):
    """仅序列号的正文行也能映射规范机器并核对企业金额。"""
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "SYN-REVIEW-03",
        [("MSOONE", "S100001"), ("MSOTWO", "S100002")],
        300, [("M", 2, 100, 110)], (2, 100, 110))
    mail = {"html": _mail_html([("", "S100001", 100),
                                ("", "S100002", 200)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "AMOUNT_CONFLICT" not in _issue_codes(cand)
    checks = {c["rule"]: c for c in cand["checks"]}
    assert checks["PREVIEW_MAIL_AMOUNT"]["result"] == "pass"
    assert preview["batch_ready"] is True


def test_adoption_baseline_is_attachment_identity(tmp_path):
    """无 MSO 附件：第一行补认不改变第二行判定基准（以附件原始身份为准）。"""
    p1 = make_no_mso_workbook(tmp_path / "ci.xlsx", "SYN-REVIEW-04",
                              "S100061", 100)
    mail = {"html": _mail_html([("X-MSO", "S100061", 50),
                                ("Y-MSO", "S100061", 50)])}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert "MSO_CONFLICT" in _issue_codes(cand)
    # 只保留首个补认值，身份不被第二个值污染
    assert cand["identity"]["mso"] == ["X-MSO"]
    assert preview["batch_ready"] is False


def test_ambiguous_machine_mapping_blocked(tmp_path):
    """同一 MSO 对应多台机器：仅 MSO 的行无法唯一映射，显式阻断。"""
    wb = make_machine_table_only_workbook(
        tmp_path / "dup.xlsx", [("DUPMSO", "S100071"), ("DUPMSO", "S100072")])
    mail = {"html": _mail_html([("DUPMSO", "", 100)])}
    preview = _inspect([wb], mail=mail)
    cand = preview["candidates"][0]
    assert "MAIL_ROW_MACHINE_AMBIGUOUS" in _issue_codes(cand)
    assert cand["needs_confirmation"] is True


def test_subject_does_not_override_attachments(tmp_path):
    p1 = make_machine_workbook(
        tmp_path / "m1.xlsx", "INV-SB-001", [("MSOSB", "S001301")],
        1000, [("M", 1, 100, 110)], (1, 100, 110))
    mail = {"subject": "INV-SB-999 something else", "html": ""}
    preview = _inspect([p1], mail=mail)
    cand = preview["candidates"][0]
    assert cand["identity"]["invoice_no"] == ["INV-SB-001"]
    assert preview["mail"]["subject"] == "INV-SB-999 something else"
