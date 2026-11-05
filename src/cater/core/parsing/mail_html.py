"""邮件业务表格解析（HTML 与可明确分列的纯文本）。

规则（plan.md §2/§5/§9）：
- 忽略转发/引用历史中的表格：位于 blockquote、飞书 lark-mail-quote、
  gmail_quote 等引用容器内的表格不参与业务提取；
- 同一封邮件中非引用区出现多版业务表时：内容一致去重，不同则暂停询问，
  不能取最后一张当作最新；
- 表头识别以 ``Cust. Purch. Order (MSO#)`` 等业务列名为准，不凭位置猜测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Issue, Source
from .headers import normalize_header

QUOTE_CONTAINERS = {"blockquote"}

# 业务表识别键：规范化后包含即视为业务表（大小写/空格/换行/标点不敏感）
BUSINESS_KEY_HEADER = "custpurchorder"

# 业务表列名 → 标准字段名（表头文本规范化后精确匹配；可配置别名见
# HEADER_ALIASES_OVERRIDES 参数与 process_job 请求 header_aliases）
COLUMN_MAP = {
    "region": "region",
    "order": "order",
    "dealerorder": "dealer_order",
    "custpurchordermso": "mso",
    "custpurchordermso号": "mso",
    "dealer": "dealer_code",
    "dealercode": "dealer_code",
    "shiptoparty": "ship_to_party",
    "dealername": "dealer_name",
    "material": "material",
    "serialnumber": "serial_number",
    "netvalueorditem": "net_value",
    "doccurrency": "currency",
    "orderstatus": "order_status",
    "dealernetnet": "dealer_net_net",
    "inlandfreight": "inland_freight",
    "oceanfrieight": "ocean_freight",   # 历史样例中的原始拼写
    "oceanfreight": "ocean_freight",
    "insurance": "insurance",
    "others": "others",
    "totalsalesprice": "total_sales_price",
}

_TEXT_HEADER_HINT = "custpurchorder"


@dataclass
class MailRow:
    fields: dict          # 标准字段名 -> 原值字符串
    source: Source


@dataclass
class MailParseResult:
    rows: list = field(default_factory=list)   # list[MailRow]
    issues: list = field(default_factory=list)
    tables_seen: int = 0                       # 非引用区识别到的业务表数量


def _is_quoted(element) -> bool:
    for parent in element.parents:
        name = getattr(parent, "name", None)
        if name in QUOTE_CONTAINERS:
            return True
        if not hasattr(parent, "get"):
            continue
        pid = parent.get("id") or ""
        if pid.startswith("lark-mail-quote") or pid.startswith("divRplyFwdMsg"):
            return True
        cls = " ".join(parent.get("class") or [])
        if "gmail_quote" in cls or "OutlookMessageHeader" in cls:
            return True
    return False


def _cell_text(cell) -> str:
    return " ".join(cell.get_text(" ", strip=True).replace("\xa0", " ").split())


def _is_business_table(table) -> bool:
    first_row = table.find("tr")
    if first_row is None:
        return False
    header = [normalize_header(_cell_text(c)) for c in first_row.find_all(["td", "th"])]
    return any(BUSINESS_KEY_HEADER in h for h in header)


def _parse_table(table, table_index: int, message_id: Optional[str]) -> list:
    trs = table.find_all("tr")
    header_cells = [normalize_header(_cell_text(c)) for c in trs[0].find_all(["td", "th"])]
    col_fields = [COLUMN_MAP.get(h) for h in header_cells]
    rows = []
    for ri, tr in enumerate(trs[1:], start=2):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        values = [_cell_text(c) for c in cells]
        fields = {}
        for ci, value in enumerate(values):
            if ci < len(col_fields) and col_fields[ci]:
                fields[col_fields[ci]] = value
        if not fields.get("mso"):
            continue
        rows.append(MailRow(fields=fields, source=Source(
            type="mail_html", message_id=message_id, table_index=table_index, row=ri)))
    return rows


def _rows_signature(rows: list) -> tuple:
    return tuple(sorted((r.fields.get("mso", ""), tuple(sorted(r.fields.items()))) for r in rows))


def parse_mail_html(html: str, message_id: Optional[str]) -> MailParseResult:
    result = MailParseResult()
    soup = BeautifulSoup(html, "lxml")
    candidates = []
    for idx, table in enumerate(soup.find_all("table")):
        if _is_quoted(table):
            continue
        if _is_business_table(table):
            candidates.append((idx, table))
    result.tables_seen = len(candidates)
    if not candidates:
        return result
    parsed = [(idx, _parse_table(t, idx, message_id)) for idx, t in candidates]
    first_sig = _rows_signature(parsed[0][1])
    for idx, rows in parsed[1:]:
        if _rows_signature(rows) != first_sig:
            result.issues.append(Issue(
                code="MULTIPLE_BUSINESS_TABLES",
                message=f"邮件正文包含 {len(candidates)} 版内容不一致的业务表，无法确定应使用的版本",
                sources=[Source(type="mail_html", message_id=message_id, table_index=i)
                         for i, _ in parsed],
                required_action="请回复明确应使用的业务表版本",
            ))
            result.rows = []
            return result
    result.rows = parsed[0][1]
    return result


def parse_mail_text(text: str, message_id: Optional[str]) -> MailParseResult:
    """可明确分列的纯文本业务表：表头行含业务列名，数据行以 Tab 或 2+ 空格分列。"""
    result = MailParseResult()
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    header_idx = next((i for i, ln in enumerate(lines)
                       if _TEXT_HEADER_HINT in normalize_header(ln)), None)
    if header_idx is None:
        return result
    header_line = lines[header_idx]
    delim = "\t" if "\t" in header_line else None
    if delim is None and re.search(r"\s{2,}", header_line):
        delim = "  "
    if delim is None:
        result.issues.append(Issue(
            code="TEXT_TABLE_NOT_RECOGNIZED",
            message="纯文本正文中的业务表无法明确分列",
            sources=[Source(type="mail_text", message_id=message_id)],
            required_action="请以 HTML 表格或 Excel 附件补充业务资料",
        ))
        return result
    split = (lambda s: s.split("\t")) if delim == "\t" else (lambda s: re.split(r"\s{2,}", s.strip()))
    header = [normalize_header(h) for h in split(header_line)]
    col_fields = [COLUMN_MAP.get(h) for h in header]
    rows = []
    for off, ln in enumerate(lines[header_idx + 1:], start=1):
        parts = split(ln)
        if len(parts) < 4:
            break
        fields = {}
        for ci, value in enumerate(parts):
            if ci < len(col_fields) and col_fields[ci]:
                fields[col_fields[ci]] = value.strip()
        if fields.get("mso"):
            rows.append(MailRow(fields=fields, source=Source(
                type="mail_text", message_id=message_id, row=header_idx + off + 1)))
    result.rows = rows
    result.tables_seen = 1 if rows else 0
    return result
