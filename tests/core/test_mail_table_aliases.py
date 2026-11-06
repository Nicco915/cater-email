"""邮件业务表语义别名与结构识别回归（全部合成数据）。

覆盖用户确认的表头变体：Cust. Purch. Order 含/不含 (MSO#) 标记、
Serial Number / SO Serial Number、Regional vs Region 等受控同义词；
列顺序变化、无关多余列、表头换行/大小写/标点、配置别名扩展、双行/合并
表头与前导标题行；误报抑制、重复列歧义阻断、quoted 历史不参与。
"""

from __future__ import annotations

from cater.core.parsing.mail_html import (
    MAIL_DEFAULT_ALIASES,
    check_alias_collisions,
    mail_aliases,
    mail_has_business_table,
    parse_mail_html,
    parse_mail_text,
)

MSG = "<msg@example.com>"


def make_table(headers: list[str], rows: list[list[str]],
               caption: str | None = None) -> str:
    parts = ["<table>"]
    if caption:
        parts.append(f"<tr><td colspan='{len(headers)}'>{caption}</td></tr>")
    parts.append("<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>")
    for row in rows:
        parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


BASE_HEADERS = ["Regional", "Order", "Dealer Order", "Cust. Purch. Order (MSO#)",
                "Dealer", "Ship-to Party", "Dealer Name", "Material",
                "Serial Number", "Total Sales Price"]
BASE_ROW = ["AMS", "0620192963", "9400015379", "FAKE001", "UM16", "UM16",
            "SOTREQ S.A.", "SEMTEST", "SYN000001A", "50000.00"]
BASE_ROW_2 = ["AMS", "0620192964", "9400015381", "FAKE002", "UM16", "UM16",
              "SOTREQ S.A.", "SEMTEST", "SYN000002B", "50000.00"]

# 生产邮件右列被裁剪后的真实表头变体（合成数据等价）
REAL_VARIANT = ["Regional", "Order", "Dealer Order", "Cust. Purch. Order",
                "Dealer", "Ship-to Party", "Dealer Name", "Material",
                "SO Serial Number", "Total Sales Price"]


def fields_by_mso(result) -> dict:
    return {row.fields["mso"]: row.fields for row in result.rows}


def test_real_header_variant_cust_purch_order_without_mso_marker() -> None:
    """Cust. Purch. Order（无 (MSO#) 标记）与 SO Serial Number 必须识别。"""
    html = make_table(REAL_VARIANT, [BASE_ROW, BASE_ROW_2])
    result = parse_mail_html(html, MSG)
    assert not [i for i in result.issues if i.blocking]
    assert len(result.rows) == 2
    fields = fields_by_mso(result)
    assert set(fields) == {"FAKE001", "FAKE002"}
    assert fields["FAKE001"]["serial_number"] == "SYN000001A"
    assert fields["FAKE001"]["dealer_code"] == "UM16"
    assert fields["FAKE001"]["total_sales_price"] == "50000.00"


def test_changed_column_order_and_extra_irrelevant_columns() -> None:
    """列顺序变化 + 无关多余列：识别不受影响，多余列如实上报（非阻断）。"""
    headers = ["Remark", "Material", "Cust. Purch. Order (MSO#)", "Dealer",
               "Serial Number", "Dealer Order", "Order", "Total Sales Price",
               "Regional"]
    row = ["synthetic note", "SEMTEST", "FAKE001", "UM16", "SYN000001A",
           "9400015379", "0620192963", "50000.00", "AMS"]
    result = parse_mail_html(make_table(headers, [row]), MSG)
    assert len(result.rows) == 1
    fields = result.rows[0].fields
    assert fields["mso"] == "FAKE001"
    assert fields["material"] == "SEMTEST"
    assert fields["serial_number"] == "SYN000001A"
    unknown = [i for i in result.issues if i.code == "UNKNOWN_MAIL_HEADER"]
    assert len(unknown) == 1 and unknown[0].blocking is False
    assert "Remark" in unknown[0].message


def test_header_linebreak_case_punctuation_variants() -> None:
    """表头换行/大小写/标点变体归一到同一语义列。"""
    variants = [
        ["REGIONAL", "Cust Purch Order", "so serial number:",
         "TOTAL SALES PRICE"],
        ["Regional", "Cust. Purch.\nOrder", "Serial Number",
         "Total Sales Price"],
        ["regional", "cust purch order", "SO SERIAL NUMBER",
         "total sales price"],
    ]
    for headers in variants:
        row = ["AMS", "FAKE001", "SYN000001A", "50000.00"]
        result = parse_mail_html(make_table(headers, [row]), MSG)
        assert len(result.rows) == 1, (headers, [i.message for i in result.issues])
        assert result.rows[0].fields["mso"] == "FAKE001"
        assert result.rows[0].fields["serial_number"] == "SYN000001A"
        assert result.rows[0].fields["total_sales_price"] == "50000.00"


def test_config_alias_extension_for_mail_headers() -> None:
    """[core.header_aliases] 语义字段名对邮件表头同样生效。"""
    headers = ["区域", "物料", "客户采购单号", "序列", "销售价格"]
    row = ["AMS", "SEMTEST", "FAKE001", "SYN000001A", "50000.00"]
    html = make_table(headers, [row])
    without = parse_mail_html(html, MSG)
    assert without.rows == []           # 自定义写法不在默认别名中
    assert not mail_has_business_table(html, "", None)
    aliases = {"region": ["区域"], "material": ["物料"],
               "mso": ["客户采购单号"], "serial_number": ["序列"],
               "total_sales_price": ["销售价格"]}
    result = parse_mail_html(html, MSG, aliases=aliases)
    assert len(result.rows) == 1
    assert result.rows[0].fields["mso"] == "FAKE001"
    assert result.rows[0].fields["total_sales_price"] == "50000.00"
    assert mail_has_business_table(html, "", aliases)


def test_leading_title_row_and_two_line_merged_header() -> None:
    """前导标题行 + 双行表头（主行未命中、次行补全）保守识别。"""
    html = (
        "<table>"
        "<tr><td colspan='10'>SYNTHETIC SHIPMENT NOTICE (fixtures only)</td></tr>"
        "<tr><th>Regional</th><th>Order</th><th>Dealer Order</th>"
        "<th>Cust. Purch.</th><th>Dealer</th><th>Material</th>"
        "<th>Serial</th><th>Total Sales</th></tr>"
        "<tr><th></th><th></th><th></th><th>Order (MSO#)</th><th></th><th></th>"
        "<th>Number</th><th>Price</th></tr>"
        "<tr><td>AMS</td><td>0620192963</td><td>9400015379</td><td>FAKE001</td>"
        "<td>UM16</td><td>SEMTEST</td><td>SYN000001A</td><td>50000.00</td></tr>"
        "</table>"
    )
    result = parse_mail_html(html, MSG)
    assert not [i for i in result.issues if i.blocking]
    assert len(result.rows) == 1
    fields = result.rows[0].fields
    assert fields["mso"] == "FAKE001"
    assert fields["serial_number"] == "SYN000001A"
    assert fields["total_sales_price"] == "50000.00"


def test_rowspan_colspan_logical_grid_header() -> None:
    """rowspan/colspan 合并表头按逻辑网格展开后识别。"""
    html = (
        "<table>"
        "<tr><th rowspan='2'>Regional</th><th colspan='2'>Order Info</th>"
        "<th rowspan='2'>Cust. Purch. Order (MSO#)</th>"
        "<th rowspan='2'>Serial Number</th><th rowspan='2'>Total Sales Price</th></tr>"
        "<tr><th>Order</th><th>Dealer Order</th></tr>"
        "<tr><td>AMS</td><td>0620192963</td><td>9400015379</td><td>FAKE001</td>"
        "<td>SYN000001A</td><td>50000.00</td></tr>"
        "</table>"
    )
    result = parse_mail_html(html, MSG)
    assert not [i for i in result.issues if i.blocking]
    assert len(result.rows) == 1
    fields = result.rows[0].fields
    assert fields["mso"] == "FAKE001"
    assert fields["order"] == "0620192963"
    assert fields["dealer_order"] == "9400015379"
    assert fields["serial_number"] == "SYN000001A"


def test_non_business_and_nested_tables_are_not_false_positives() -> None:
    """无 MSO 列的表不是业务表；嵌套展示表只由内层业务表提取一次。"""
    junk = make_table(["Name", "Address"], [["SYN01", "somewhere"]])
    result = parse_mail_html(junk, MSG)
    assert result.rows == [] and result.tables_seen == 0
    assert not mail_has_business_table(junk, "", None)

    nested = (
        "<table><tr><td>layout</td><td>"
        + make_table(BASE_HEADERS, [BASE_ROW])
        + "</td></tr></table>"
    )
    result2 = parse_mail_html(nested, MSG)
    assert result2.tables_seen == 1
    assert len(result2.rows) == 1
    assert result2.rows[0].fields["mso"] == "FAKE001"


def test_ambiguous_duplicate_mso_columns_block() -> None:
    """同一语义字段（MSO 列）出现两列 → AMBIGUOUS_COLUMN 阻断，不猜测。"""
    headers = ["Cust. Purch. Order (MSO#)", "Cust. Purch. Order",
               "Serial Number"]
    row = ["FAKE001", "FAKE002", "SYN000001A"]
    result = parse_mail_html(make_table(headers, [row]), MSG)
    assert result.rows == []
    ambiguous = [i for i in result.issues if i.code == "AMBIGUOUS_COLUMN"]
    assert ambiguous and ambiguous[0].blocking


def test_multiple_inconsistent_business_tables_pause() -> None:
    t1 = make_table(BASE_HEADERS, [BASE_ROW])
    t2 = make_table(BASE_HEADERS, [BASE_ROW_2])
    result = parse_mail_html(t1 + t2, MSG)
    assert result.rows == []
    assert any(i.code == "MULTIPLE_BUSINESS_TABLES" for i in result.issues)


def test_quoted_history_tables_never_count() -> None:
    """blockquote / gmail_quote / 文本 > 引用行中的旧表不参与提取。"""
    fresh = make_table(BASE_HEADERS, [BASE_ROW])
    old = make_table(BASE_HEADERS, [BASE_ROW_2])
    html = fresh + f"<blockquote>{old}</blockquote>"
    html += f"<div class='gmail_quote'>{old}</div>"
    result = parse_mail_html(html, MSG)
    assert result.tables_seen == 1
    assert {r.fields["mso"] for r in result.rows} == {"FAKE001"}

    text = (
        "Regional\tOrder\tDealer Order\tCust. Purch. Order (MSO#)\tDealer\t"
        "Serial Number\tTotal Sales Price\n"
        "AMS\t0620192963\t9400015379\tFAKE001\tUM16\tSYN000001A\t50000.00\n"
        "On yesterday someone wrote:\n"
        "> Regional\tOrder\tDealer Order\tCust. Purch. Order (MSO#)\tDealer\t"
        "Serial Number\tTotal Sales Price\n"
        "> AMS\t0620192964\t9400015381\tFAKE002\tUM16\tSYN000002B\t50000.00\n"
    )
    tresult = parse_mail_text(text, MSG)
    assert len(tresult.rows) == 1
    assert tresult.rows[0].fields["mso"] == "FAKE001"


def test_text_table_variants() -> None:
    """Tab/空格分列的纯文本业务表：变体表头 + 全大写 + 标点。"""
    lines = [
        "REGIONAL\tCUST PURCH ORDER\tSO SERIAL NUMBER\tTOTAL SALES PRICE",
        "AMS\tFAKE001\tSYN000001A\t50000.00",
    ]
    result = parse_mail_text("\n".join(lines), MSG)
    assert len(result.rows) == 1
    assert result.rows[0].fields["mso"] == "FAKE001"
    assert result.rows[0].fields["serial_number"] == "SYN000001A"

    spaced = (
        "Regional  Cust. Purch. Order (MSO#)  Serial Number  Total Sales Price\n"
        "AMS  FAKE002  SYN000002B  50000.00\n"
    )
    result2 = parse_mail_text(spaced, MSG)
    assert len(result2.rows) == 1
    assert result2.rows[0].fields["mso"] == "FAKE002"


def test_order_dealer_order_mso_distinct_no_fuzzy_merge() -> None:
    """Order / Dealer Order / MSO 语义不互相归并（不做模糊猜测）。"""
    headers = ["Order", "Cust. Purch. Order (MSO#)", "Serial Number",
               "Total Sales Price"]
    row = ["0620192963", "FAKE001", "SYN000001A", "50000.00"]
    result = parse_mail_html(make_table(headers, [row]), MSG)
    fields = result.rows[0].fields
    assert fields["order"] == "0620192963"
    assert fields["mso"] == "FAKE001"
    assert "dealer_order" not in fields


def test_huge_colspan_blocks_with_structured_issue():
    """未受信的 colspan 整数不封顶会造成巨大分配：越上限必须整体阻断
    （TABLE_STRUCTURE_INVALID），绝不部分交付。"""
    html = (
        "<table><tr>"
        "<th>Cust. Purch. Order</th>"
        "<th colspan='999999'>Dealer</th>"
        "</tr>"
        "<tr><td>FAKE001</td><td>UM16</td></tr>"
        "</table>"
    )
    result = parse_mail_html(html, "<huge@example.com>")
    assert result.rows == []
    codes = [i.code for i in result.issues]
    assert "TABLE_STRUCTURE_INVALID" in codes
    issue = next(i for i in result.issues if i.code == "TABLE_STRUCTURE_INVALID")
    assert issue.blocking
    assert "999999" in issue.message


def test_huge_rowspan_blocks_with_structured_issue():
    """rowspan 同样封顶：越上限整体阻断。"""
    html = (
        "<table><tr>"
        "<th>Cust. Purch. Order</th><th>Dealer</th>"
        "</tr>"
        "<tr><td rowspan='999999'>FAKE001</td><td>UM16</td></tr>"
        "</table>"
    )
    result = parse_mail_html(html, "<huge-row@example.com>")
    assert result.rows == []
    assert any(i.code == "TABLE_STRUCTURE_INVALID" and i.blocking
               for i in result.issues)


def test_alias_collision_in_mail_fields_is_rejected():
    """同一归一键映射到两个邮件语义字段是配置冲突（last-wins 会掩盖
    映射错误）：必须出结构化阻断问题，而不是静默取后者。"""
    overrides = {"dealer_code": ["mso"]}   # mso 已属 mso 字段
    resolved = mail_aliases(overrides)
    conflicts = check_alias_collisions(resolved, scope=set(MAIL_DEFAULT_ALIASES))
    assert conflicts, "expected a collision report"
    assert any("mso" in c for c in conflicts)

    result = parse_mail_html(
        "<table><tr><th>Cust. Purch. Order</th><th>Dealer</th></tr>"
        "<tr><td>FAKE001</td><td>UM16</td></tr></table>",
        "<collision@example.com>", aliases=overrides)
    assert result.rows == []
    codes = [i.code for i in result.issues]
    assert "ALIAS_CONFIG_CONFLICT" in codes
    assert all(i.blocking for i in result.issues
               if i.code == "ALIAS_CONFIG_CONFLICT")

    # 纯文本同样拒绝
    text_result = parse_mail_text(
        "Cust. Purch. Order\tDealer\nFAKE001\tUM16",
        "<collision-text@example.com>", aliases=overrides)
    assert "ALIAS_CONFIG_CONFLICT" in [i.code for i in text_result.issues]


def test_cross_namespace_same_key_is_not_a_collision():
    """同一归一键出现在工作簿字段（gross_weight）与邮件字段下不算冲突：
    两个命名空间各自匹配。默认集必须零冲突。"""
    assert check_alias_collisions(mail_aliases(), scope=set(MAIL_DEFAULT_ALIASES)) == []
    # 工作簿风格字段名加入覆盖也不触发邮件侧冲突
    resolved = mail_aliases({"gross_weight": ["Gross Weight KGS"]})
    assert check_alias_collisions(resolved, scope=set(MAIL_DEFAULT_ALIASES)) == []
