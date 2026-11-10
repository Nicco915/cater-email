"""多票分票核心层测试（阶段 2 人工归属调整）。

验证 assign/exclude/clear 语义：只接受服务端存储方案中的未归属目标；
未知目标/矛盾指派显式阻断；排除留审计；指纹随调整变化。
所有工作簿均为现场构造的合成数据。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import openpyxl

from cater.core.split_preview import inspect_sources


def _machine_workbook(path: Path, invoice_no, mso, serial, amount) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.cell(1, 1, f"INVOICE NO.: {invoice_no}")
    ws.cell(3, 1, "MODEL")
    ws.cell(3, 2, "MSO#")
    ws.cell(3, 3, "SERIAL#")
    ws.cell(4, 1, "TESTMODEL")
    ws.cell(4, 2, mso)
    ws.cell(4, 3, serial)
    ws.cell(6, 1, "TOTAL AMOUNT (IN WORDS): SAY USD ONLY")
    ws.cell(6, 7, amount)
    pl = wb.create_sheet("Packing list")
    pl.cell(2, 1, "DESCRIPTION OF GOODS")
    pl.cell(2, 2, "QTY")
    pl.cell(2, 3, "NET WEIGHT (KGS)")
    pl.cell(2, 4, "GROSS WEIGHT (KGS)")
    pl.cell(3, 1, "MACHINE")
    pl.cell(3, 2, 1)
    pl.cell(3, 3, 100)
    pl.cell(3, 4, 110)
    pl.cell(4, 1, "TOTAL")
    pl.cell(4, 2, 1)
    pl.cell(4, 3, 100)
    pl.cell(4, 4, 110)
    wb.save(path)
    return path


def _identity_free_workbook(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Notes"
    ws.cell(1, 1, "misc notes without business identity")
    wb.save(path)
    return path


def _mail_html(rows):
    body = "".join(
        f"<tr><td>{mso}</td><td>{serial}</td><td>{price}</td></tr>"
        for mso, serial, price in rows)
    return ("<html><body><table><tr><td>MSO</td><td>Serial Number</td>"
            "<td>Total Sales Price</td></tr>"
            f"{body}</table></body></html>")


def _inspect(paths, mail=None, assignments=None):
    request = {
        "sources": [{"path": str(p), "filename": p.name} for p in paths],
        "mail": mail or {},
    }
    if assignments is not None:
        request["assignments"] = assignments
    return inspect_sources(request)


def _by_invoice(preview, invoice_no):
    for cand in preview["candidates"]:
        if invoice_no in cand["identity"].get("invoice_no", []):
            return cand
    raise AssertionError(f"候选缺失：{invoice_no}")


def _two_ticket_sources(tmp_path):
    a = _machine_workbook(tmp_path / "a.xlsx", "INV-A-001", "MSOA1", "S000001", 1000)
    b = _machine_workbook(tmp_path / "b.xlsx", "INV-B-001", "MSOB1", "S000002", 2000)
    return [a, b]


def _blocking_codes(preview):
    return {i["code"] for i in preview["issues"] if i.get("blocking")}


# ---------------------------------------------------------------------------
# 正文行调整
# ---------------------------------------------------------------------------


def test_exclude_unmatched_row_resolves_with_audit(tmp_path):
    paths = _two_ticket_sources(tmp_path)
    mail = {"html": _mail_html([("MSOA1", "S000001", 1000),
                                ("MSOB1", "S000002", 2000),
                                ("MSOXX", "S009999", 3000)])}
    base = _inspect(paths, mail=mail)
    assert len(base["unmatched_mail_rows"]) == 1
    row = base["unmatched_mail_rows"][0]

    adjusted = _inspect(paths, mail=mail, assignments={
        "mail_rows": [{"table_index": row["table_index"], "row": row["row"],
                       "action": "exclude", "reason": "非本批业务"}],
        "sources": [],
    })
    assert adjusted["unmatched_mail_rows"] == []
    assert len(adjusted["excluded_mail_rows"]) == 1
    assert adjusted["excluded_mail_rows"][0]["reason"] == "非本批业务"
    assert adjusted["batch_ready"] is True
    # 指纹随调整变化（乐观锁基础）
    assert adjusted["source_fingerprint"] != base["source_fingerprint"]


def test_assign_unmatched_row_attaches_and_rechecks(tmp_path):
    paths = _two_ticket_sources(tmp_path)
    mail = {"html": _mail_html([("MSOA1", "S000001", 1000),
                                ("MSOB1", "S000002", 2000),
                                ("MSOXX", "S009999", 3000)])}
    base = _inspect(paths, mail=mail)
    row = base["unmatched_mail_rows"][0]
    cand_a = _by_invoice(base, "INV-A-001")

    adjusted = _inspect(paths, mail=mail, assignments={
        "mail_rows": [{"table_index": row["table_index"], "row": row["row"],
                       "action": "assign",
                       "candidate_id": cand_a["candidate_id"], "reason": ""}],
        "sources": [],
    })
    assert adjusted["unmatched_mail_rows"] == []
    cand = _by_invoice(adjusted, "INV-A-001")
    assigned = [r for r in cand["mail_row_refs"] if r.get("assigned")]
    assert len(assigned) == 1
    # 重新核对：MSOXX/S009999 不属 A 票 -> 映射问题 + 不完整
    codes = {i["code"] for i in cand["issues"]}
    assert "MAIL_ROW_MACHINE_AMBIGUOUS" in codes
    assert cand["needs_confirmation"] is True
    assert adjusted["batch_ready"] is False


def test_assign_to_unknown_candidate_blocked(tmp_path):
    paths = _two_ticket_sources(tmp_path)
    mail = {"html": _mail_html([("MSOA1", "S000001", 1000),
                                ("MSOXX", "S009999", 3000)])}
    base = _inspect(paths, mail=mail)
    row = base["unmatched_mail_rows"][0]
    adjusted = _inspect(paths, mail=mail, assignments={
        "mail_rows": [{"table_index": row["table_index"], "row": row["row"],
                       "action": "assign", "candidate_id": "cand-nope",
                       "reason": ""}],
        "sources": [],
    })
    assert "ADJUSTMENT_TARGET_UNKNOWN" in _blocking_codes(adjusted)
    assert adjusted["batch_ready"] is False
    # 行保持未归属（未被静默吞掉）
    assert len(adjusted["unmatched_mail_rows"]) == 1


def test_assign_contradicting_unique_evidence_blocked(tmp_path):
    """自动证据唯一指向 A，人工指派到 B：矛盾，显式阻断。"""
    paths = _two_ticket_sources(tmp_path)
    mail = {"html": _mail_html([("MSOA1", "S000001", 1000),
                                ("MSOB1", "S000002", 2000)])}
    base = _inspect(paths, mail=mail)
    assert base["unmatched_mail_rows"] == []
    cand_a = _by_invoice(base, "INV-A-001")
    cand_b = _by_invoice(base, "INV-B-001")
    row_ref = cand_a["mail_row_refs"][0]
    adjusted = _inspect(paths, mail=mail, assignments={
        "mail_rows": [{"table_index": row_ref["table_index"],
                       "row": row_ref["row"], "action": "assign",
                       "candidate_id": cand_b["candidate_id"], "reason": ""}],
        "sources": [],
    })
    assert "ADJUSTMENT_CONFLICT" in _blocking_codes(adjusted)
    assert adjusted["batch_ready"] is False


def test_assign_resolves_multi_candidate_ambiguity(tmp_path):
    """序列号命中两票的歧义行：指派到命中集合内的候选即解决歧义。"""
    a = _machine_workbook(tmp_path / "a.xlsx", "INV-S-001", "MSOSA", "S100001", 1000)
    b = _machine_workbook(tmp_path / "b.xlsx", "INV-S-002", "MSOSB", "S100001", 2000)
    mail = {"html": _mail_html([("", "S100001", 1000)])}
    base = _inspect([a, b], mail=mail)
    assert len(base["unmatched_mail_rows"]) == 1
    assert base["unmatched_mail_rows"][0]["reason"] == "ambiguous_match"
    row = base["unmatched_mail_rows"][0]
    cand_a = _by_invoice(base, "INV-S-001")

    adjusted = _inspect([a, b], mail=mail, assignments={
        "mail_rows": [{"table_index": row["table_index"], "row": row["row"],
                       "action": "assign",
                       "candidate_id": cand_a["candidate_id"], "reason": ""}],
        "sources": [],
    })
    assert adjusted["unmatched_mail_rows"] == []
    cand = _by_invoice(adjusted, "INV-S-001")
    assert any(r.get("assigned") for r in cand["mail_row_refs"])
    assert not _blocking_codes(adjusted)


def test_assign_outside_ambiguity_hits_blocked(tmp_path):
    """歧义行指派到命中集合之外的第三票：显式阻断。"""
    a = _machine_workbook(tmp_path / "a.xlsx", "INV-T-001", "MSOTA", "S200001", 1000)
    b = _machine_workbook(tmp_path / "b.xlsx", "INV-T-002", "MSOTB", "S200001", 2000)
    c = _machine_workbook(tmp_path / "c.xlsx", "INV-T-003", "MSOTC", "S200003", 3000)
    mail = {"html": _mail_html([("", "S200001", 1000)])}
    base = _inspect([a, b, c], mail=mail)
    row = base["unmatched_mail_rows"][0]
    cand_c = _by_invoice(base, "INV-T-003")
    adjusted = _inspect([a, b, c], mail=mail, assignments={
        "mail_rows": [{"table_index": row["table_index"], "row": row["row"],
                       "action": "assign",
                       "candidate_id": cand_c["candidate_id"], "reason": ""}],
        "sources": [],
    })
    assert "ADJUSTMENT_CONFLICT" in _blocking_codes(adjusted)


def test_malformed_assignments_blocked(tmp_path):
    paths = _two_ticket_sources(tmp_path)
    adjusted = _inspect(paths, assignments=["not-a-dict"])
    assert "ADJUSTMENT_INVALID" in _blocking_codes(adjusted)
    adjusted2 = _inspect(paths, assignments={
        "mail_rows": [{"action": "assign"}],  # 缺定位
        "sources": [],
    })
    assert "ADJUSTMENT_INVALID" in _blocking_codes(adjusted2)


# ---------------------------------------------------------------------------
# 附件调整
# ---------------------------------------------------------------------------


def test_assign_identity_free_workbook_to_unique_candidate(tmp_path):
    paths = _two_ticket_sources(tmp_path)
    notes = _identity_free_workbook(tmp_path / "notes.xlsx")
    base = _inspect(paths + [notes])
    empty = next(s for s in base["unmatched_sources"]
                 if s.get("status") == "empty")
    sha = hashlib.sha256(notes.read_bytes()).hexdigest()
    assert empty["sha256"] == sha
    cand_a = _by_invoice(base, "INV-A-001")

    adjusted = _inspect(paths + [notes], assignments={
        "mail_rows": [],
        "sources": [{"sha256": sha, "action": "assign",
                     "candidate_id": cand_a["candidate_id"], "reason": ""}],
    })
    assert all(s["sha256"] != sha for s in adjusted["unmatched_sources"])
    cand = _by_invoice(adjusted, "INV-A-001")
    ref = next(r for r in cand["source_refs"] if r["sha256"] == sha)
    assert ref["assigned"] is True
    # 候选身份不变（注入附件不改变候选基础身份）
    assert cand["candidate_id"] == cand_a["candidate_id"]


def test_assign_source_to_unknown_candidate_blocked(tmp_path):
    paths = _two_ticket_sources(tmp_path)
    notes = _identity_free_workbook(tmp_path / "notes.xlsx")
    sha = hashlib.sha256(notes.read_bytes()).hexdigest()
    adjusted = _inspect(paths + [notes], assignments={
        "mail_rows": [],
        "sources": [{"sha256": sha, "action": "assign",
                     "candidate_id": "cand-nope", "reason": ""}],
    })
    assert "ADJUSTMENT_TARGET_UNKNOWN" in _blocking_codes(adjusted)
    assert any(s["sha256"] == sha for s in adjusted["unmatched_sources"])


def test_exclude_source_keeps_audit(tmp_path):
    paths = _two_ticket_sources(tmp_path)
    notes = _identity_free_workbook(tmp_path / "notes.xlsx")
    sha = hashlib.sha256(notes.read_bytes()).hexdigest()
    adjusted = _inspect(paths + [notes], assignments={
        "mail_rows": [],
        "sources": [{"sha256": sha, "action": "exclude",
                     "reason": "仅为参考笔记"}],
    })
    assert all(s["sha256"] != sha for s in adjusted["unmatched_sources"])
    excluded = next(s for s in adjusted["excluded_sources"]
                    if s["sha256"] == sha)
    assert excluded["reason"] == "仅为参考笔记"
    assert adjusted["batch_ready"] is True
