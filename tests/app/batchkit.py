"""多票批次测试共享构造器：全合成数据（假 MSO/序列号/发票号），
不含任何真实业务信息。被 test_batches*.py 与 test_batch_web.py 共用。
"""
from __future__ import annotations

import io

import openpyxl

from helpers import make_mail


def machine_workbook_bytes(invoice_no: str, mso: str, serial: str,
                           amount: float) -> bytes:
    """单票整机发票+箱单工作簿（结构仿核心测试合成样例）。"""
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
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def identity_free_workbook_bytes() -> bytes:
    """可读但没有任何业务身份的工作簿（可指派到候选的"empty"附件）。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Notes"
    ws.cell(1, 1, "misc notes without business identity")
    ws.cell(2, 1, "SYNTHETIC")
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def mail_table_html(rows: list[tuple[str, str, float]]) -> str:
    body = "".join(
        f"<tr><td>{mso}</td><td>{serial}</td><td>{price}</td></tr>"
        for mso, serial, price in rows)
    return ("<html><body><p>Please find the order below.</p>"
            "<table><tr><td>MSO</td><td>Serial Number</td>"
            "<td>Total Sales Price</td></tr>"
            f"{body}</table></body></html>")


def two_ticket_mail(*, subject: str = "两票整机发票",
                    sender: str = "tester@example.com",
                    extra_rows: list[tuple[str, str, float]] | None = None,
                    extra_attachments: tuple[tuple[str, bytes], ...] = (),
                    broken_attachment: bool = False) -> bytes:
    """两票整机邮件：INV-A-001/MSOA1/S000001(1000) + INV-B-001/MSOB1/S000002(2000)，
    正文业务行金额与附件一致（batch_ready=True）。"""
    rows = [("MSOA1", "S000001", 1000), ("MSOB1", "S000002", 2000)]
    rows.extend(extra_rows or [])
    attachments = (
        ("a-invoice.xlsx", machine_workbook_bytes("INV-A-001", "MSOA1", "S000001", 1000)),
        ("b-invoice.xlsx", machine_workbook_bytes("INV-B-001", "MSOB1", "S000002", 2000)),
    ) + tuple(extra_attachments)
    if broken_attachment:
        attachments += (("broken.xlsx", b"not-a-real-xlsx"),)
    return make_mail(
        sender=sender,
        subject=subject,
        text="见附件发票与箱单。",
        html=mail_table_html(rows),
        attachments=attachments,
    )


def import_two_ticket(service, *, actor: str = "tester",
                      **mail_kwargs):
    """导入两票邮件并返回 (batch_id, 首份方案)。"""
    result = service.batches.import_eml_bytes(
        two_ticket_mail(**mail_kwargs), filename="batch.eml", actor=actor)
    batch = result["batch"]
    return batch["id"], result
