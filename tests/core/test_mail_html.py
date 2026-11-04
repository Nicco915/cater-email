"""邮件业务表解析：引用区过滤、多版本检测、纯文本分列。"""
from cater.core.parsing.mail_html import parse_mail_html, parse_mail_text

from conftest import MAIL_HTML_FIXTURE, MESSAGE_ID


def _html():
    return MAIL_HTML_FIXTURE.read_text(encoding="utf-8", errors="replace")


def test_current_table_from_unquoted_region():
    result = parse_mail_html(_html(), MESSAGE_ID)
    assert result.issues == []
    assert result.tables_seen == 1, "引用区旧版业务表不参与计数"
    assert len(result.rows) == 4
    row = result.rows[0]
    assert row.fields["mso"] == "BFWVU"
    assert row.fields["order"] == "0620186623"
    assert row.fields["dealer_order"] == "TSUJKT1371"
    assert row.fields["dealer_code"] == "JM28"
    assert row.fields["material"] == "SEM520"
    assert row.fields["serial_number"] == "S8Z00583"
    assert row.fields["total_sales_price"] == "68,822.84"  # 原值保留，金额解析在核对层
    assert row.source.table_index is not None


def test_multiple_conflicting_versions_pause():
    """非引用区出现两张内容不同的业务表 → 暂停询问，不取最后一张。"""
    table = (
        "<table><tr><td>Cust. Purch. Order (MSO#)</td><td>Material</td>"
        "<td>Serial Number</td></tr>"
        "<tr><td>BFWVU</td><td>SEM520</td><td>S8Z00583</td></tr></table>"
    )
    table2 = table.replace("S8Z00583", "S8Z99999")
    html = f"<html><body>{table}<p>更新</p>{table2}</body></html>"
    result = parse_mail_html(html, "m1")
    assert result.rows == []
    assert any(i.code == "MULTIPLE_BUSINESS_TABLES" for i in result.issues)


def test_identical_duplicate_tables_deduped():
    table = (
        "<table><tr><td>Cust. Purch. Order (MSO#)</td><td>Material</td></tr>"
        "<tr><td>BFWVU</td><td>SEM520</td></tr></table>"
    )
    html = f"<html><body>{table}{table}</body></html>"
    result = parse_mail_html(html, "m1")
    assert result.issues == []
    assert result.tables_seen == 2
    assert len(result.rows) == 1


def test_blockquote_table_ignored():
    table = (
        "<table><tr><td>Cust. Purch. Order (MSO#)</td><td>Material</td></tr>"
        "<tr><td>BFWVU</td><td>SEM520</td></tr></table>"
    )
    html = f"<html><body><blockquote>{table}</blockquote></body></html>"
    result = parse_mail_html(html, "m1")
    assert result.tables_seen == 0
    assert result.rows == []


def test_plain_text_table():
    text = (
        "各位：\n"
        "Order\tDealer Order\tCust. Purch. Order (MSO#)\tDealer\tMaterial\tSerial Number\tTotal Sales Price\n"
        "0620186623\tTSUJKT1371\tBFWVU\tJM28\tSEM520\tS8Z00583\t68,822.84\n"
        "620186599\tTSUJKT1365\tBFWVK\tJM28\tSEM512\tGJW00836\t39,881.55\n"
    )
    result = parse_mail_text(text, "m2")
    assert len(result.rows) == 2
    assert result.rows[0].fields["mso"] == "BFWVU"
    assert result.rows[1].fields["order"] == "620186599"  # 无前导零，规范化在转换层


def test_plain_text_table_with_wide_spaces():
    text = (
        "Order  Dealer Order  Cust. Purch. Order (MSO#)  Material\n"
        "0620186623  TSUJKT1371  BFWVU  SEM520\n"
    )
    result = parse_mail_text(text, "m3")
    assert len(result.rows) == 1
    assert result.rows[0].fields["material"] == "SEM520"
