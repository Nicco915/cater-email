"""邮件业务表格解析（HTML 与可明确分列的纯文本）。

规则（plan.md §2/§5/§9）：
- 忽略转发/引用历史中的表格：位于 blockquote、飞书 lark-mail-quote、
  gmail_quote 等引用容器内的表格不参与业务提取；纯文本中的引用行
  （``>`` 前缀与常见引用边界）同样剔除；
- 同一封邮件中非引用区出现多版业务表时：内容一致去重，不同则暂停询问，
  不能取最后一张当作最新；
- 表头识别走语义别名精确匹配（NFKC/大小写/空白/换行/标点不敏感，但不做
  模糊猜测）：业务表必须含 MSO 列（Cust. Purch. Order 含或不含 (MSO#)
  标记、MSO 等受控同义词）；Order / Dealer Order / MSO 语义不同，
  unitprice 与 TotalSalesPrice 语义不同，均不互相归并；
- 语义别名默认内置，并可经 ``aliases``（process_job 请求 header_aliases，
  与 [core.header_aliases] 同一配置）按语义字段名扩展；
- 表头允许前导标题行与双行/合并（rowspan/colspan）表头：按逻辑网格保守
  组合，组合后仍无法识别或未识别的列如实上报（UNKNOWN_MAIL_HEADER），
  不猜测；同一语义字段映射到多列 → AMBIGUOUS_COLUMN 阻断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Issue, Source
from .headers import normalize_header

QUOTE_CONTAINERS = {"blockquote"}

# 语义字段默认别名集（规范化后的键）。可经 aliases 参数按字段名追加，
# 与 [core.header_aliases] / process_job header_aliases 共用同一配置；
# 只做规范化后的精确匹配，不做模糊匹配。
MAIL_DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "region": ("regional", "region"),
    "order": ("order",),
    "dealer_order": ("dealerorder",),
    # Cust. Purch. Order 含/不含 (MSO#) 标记、MSO：均为 MSO 列受控同义词
    "mso": ("custpurchorder", "custpurchordermso", "custpurchordermso号", "mso"),
    "dealer_code": ("dealer", "dealercode"),
    "ship_to_party": ("shiptoparty",),
    "dealer_name": ("dealername",),
    "material": ("material",),
    # Serial Number / SO Serial Number 同义（生产邮件右列实际写法）
    "serial_number": ("serialnumber", "soserialnumber"),
    "net_value": ("netvalueorditem",),
    "currency": ("doccurrency",),
    "order_status": ("orderstatus",),
    "dealer_net_net": ("dealernetnet",),
    "inland_freight": ("inlandfreight",),
    # 历史样例中的原始拼写 oceanfrieight 保留
    "ocean_freight": ("oceanfrieight", "oceanfreight"),
    "insurance": ("insurance",),
    "others": ("others",),
    "total_sales_price": ("totalsalesprice",),
}

# 表头候选扫描上限（前导标题行 + 双行表头）
_HEADER_SCAN_ROWS = 8
# 数据区直接子行解析上限（有界截止，防异常大表）
_DATA_SCAN_ROWS = 2000

_TEXT_HEADER_SCAN_LINES = 20

# 逻辑网格尺寸上限：外部 HTML 的 rowspan/colspan 是未受信整数，不封顶会
# 造成巨大分配（每格重复文本）。超过上限按结构化问题整体阻断，绝不部分
# 交付。
MAX_COL_SPAN = 64
MAX_ROW_SPAN = 64
MAX_GRID_COLS = 256


class TableStructureError(ValueError):
    """业务表合并单元格尺寸越上限：结构化阻断问题，不部分交付。"""


def mail_aliases(overrides: Optional[dict] = None) -> dict[str, tuple[str, ...]]:
    """默认邮件别名集合并调用方配置：{语义字段: [别名, ...]} 追加到默认键。

    同一归一键出现在多个字段名下不一定是冲突：``[core.header_aliases]``
    同时服务邮件与工作簿两个命名空间，整机/配件的工作簿语义字段（如
    gross_weight）与邮件字段（如 dealer_code）可以各自使用同一个归一
    化键。真正的歧义只发生在同一命名空间内——见 check_alias_collisions
    的 scope 参数。"""
    result = {name: tuple(keys) for name, keys in MAIL_DEFAULT_ALIASES.items()}
    for field_name, extra in (overrides or {}).items():
        if not isinstance(extra, (list, tuple)):
            continue
        keys = tuple(normalize_header(item) for item in extra if str(item).strip())
        base = result.get(field_name, ())
        merged = list(base)
        for key in keys:
            if key and key not in merged:
                merged.append(key)
        result[field_name] = tuple(merged)
    return result


def check_alias_collisions(aliases: dict[str, tuple[str, ...]],
                           scope: Optional[set] = None) -> list[str]:
    """返回映射到多个字段的归一化别名（配置冲突清单，空为健康）。

    ``scope`` 限定参与冲突检测的字段名集合：邮件解析传
    ``set(MAIL_DEFAULT_ALIASES)``——只有同一邮件语义字段名下的别名撞
    车才算歧义；跨命名空间（工作簿字段名与邮件字段名相同键）不算。"""
    seen: dict[str, str] = {}
    conflicts: list[str] = []
    for name, keys in aliases.items():
        if scope is not None and name not in scope:
            continue
        for key in keys:
            owner = seen.get(key)
            if owner is not None and owner != name:
                conflicts.append(
                    f"别名 {key!r} 同时映射到 {owner!r} 与 {name!r}")
            else:
                seen[key] = name
    return conflicts


@dataclass
class MailRow:
    fields: dict          # 标准字段名 -> 原值字符串
    source: Source


@dataclass
class MailParseResult:
    rows: list = field(default_factory=list)   # list[MailRow]
    issues: list = field(default_factory=list)
    tables_seen: int = 0                       # 非引用区识别到的业务表数量


@dataclass
class _HeaderPlan:
    """一张业务表的列计划：列序号 -> 语义字段，及未识别表头原文。"""
    fields: dict                      # col_index(0 基) -> 语义字段
    unknown: list                     # 未识别的表头原文（去重保序）
    duplicates: list                  # 同字段多列的语义名


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


def _own_rows(table, limit: Optional[int] = None) -> list:
    """属于本表（最近 table 祖先就是本表）的行，排除嵌套表的行。"""
    rows = [tr for tr in table.find_all("tr")
            if tr.find_parent("table") is table]
    return rows[:limit] if limit is not None else rows


def _logical_grid(table, max_rows: int = _HEADER_SCAN_ROWS) -> list[list[str]]:
    """前若干行按 rowspan/colspan 展开为逻辑网格（每行等长，合并单元格
    的文本在其跨度内重复出现），用于表头定位；数据行不走此网格。"""
    matrix: list[list[str]] = []
    open_spans: dict[int, list] = {}  # col -> [剩余行数, 文本]
    for tr in _own_rows(table, max_rows):
        row: list[str] = []
        col = 0

        def fill_spans(upto: Optional[int] = None) -> None:
            nonlocal col
            limit = upto if upto is not None else (max(open_spans) + 1 if open_spans else 0)
            while col in open_spans and col < limit:
                row.append(open_spans[col][1])
                open_spans[col][0] -= 1
                if open_spans[col][0] <= 0:
                    del open_spans[col]
                col += 1

        for cell in tr.find_all(["td", "th"]):
            fill_spans()
            text = _cell_text(cell)
            try:
                colspan = max(int(cell.get("colspan") or 1), 1)
                rowspan = max(int(cell.get("rowspan") or 1), 1)
            except (TypeError, ValueError):
                colspan, rowspan = 1, 1
            if colspan > MAX_COL_SPAN or rowspan > MAX_ROW_SPAN:
                raise TableStructureError(
                    f"合并单元格超出上限：colspan={colspan} rowspan={rowspan} "
                    f"（上限 {MAX_COL_SPAN}/{MAX_ROW_SPAN}）")
            if col + colspan > MAX_GRID_COLS:
                raise TableStructureError(
                    f"表格列数超出上限：{col + colspan}（上限 {MAX_GRID_COLS}）")
            for _ in range(colspan):
                row.append(text)
                if rowspan > 1:
                    open_spans[col] = [rowspan - 1, text]
                col += 1
        fill_spans()
        matrix.append(row)
    return matrix


def _resolve_header(cells: list[str], aliases) -> _HeaderPlan:
    """把一行（或合并后的）表头单元格解析为列计划。"""
    fields: dict[int, str] = {}
    unknown: list[str] = []
    duplicates: list[str] = []
    key_to_field = {key: name for name, keys in aliases.items() for key in keys}
    for idx, text in enumerate(cells):
        norm = normalize_header(text)
        if not norm:
            continue
        name = key_to_field.get(norm)
        if name is None:
            if text.strip() and text.strip() not in unknown:
                unknown.append(text.strip())
            continue
        if name in fields.values():
            if name not in duplicates:
                duplicates.append(name)
            continue
        fields[idx] = name
    return _HeaderPlan(fields=fields, unknown=unknown, duplicates=duplicates)


def _find_header_plan(grid: list[list[str]], aliases) -> tuple[int, Optional[_HeaderPlan], list[str]]:
    """在逻辑网格前若干行定位表头：优先单行命中 MSO 列；未映射列用紧随
    的次行按列补全（双行/合并表头）；整行未命中时再尝试相邻两行整行合并。
    返回 (数据起始行号, 列计划, 未识别原文)——次行被用作表头一部分时，
    数据从其后一行开始，次表头行不会被误当数据。"""
    key_to_field = {key: name for name, keys in aliases.items() for key in keys}

    def mapped(text: str) -> bool:
        norm = normalize_header(text)
        return bool(norm) and norm in key_to_field

    combined_unknown: list[str] = []
    for r, cells in enumerate(grid):
        plan = _resolve_header(cells, aliases)
        if "mso" in plan.fields.values():
            if plan.unknown and r + 1 < len(grid):
                # 次行补全：主行未映射的列取次行同列文本（保守，不拼接猜测）
                nxt = grid[r + 1]
                width = max(len(cells), len(nxt))
                filled = [
                    (cells[c] if c < len(cells) and mapped(cells[c])
                     else (nxt[c] if c < len(nxt) else ""))
                    for c in range(width)
                ]
                plan2 = _resolve_header(filled, aliases)
                if ("mso" in plan2.fields.values()
                        and len(plan2.fields) > len(plan.fields)):
                    return r + 2, plan2, plan2.unknown
            return r + 1, plan, plan.unknown
        combined_unknown.extend(plan.unknown)
        # 双行表头：与下一行按列合并后重试（仅当本行已有映射或下一行有映射）
        if r + 1 < len(grid):
            nxt = grid[r + 1]
            width = max(len(cells), len(nxt))
            merged = []
            for c in range(width):
                parts = []
                for src in (cells, nxt):
                    if c < len(src) and src[c].strip():
                        parts.append(src[c].strip())
                merged.append(" ".join(dict.fromkeys(parts)))
            plan2 = _resolve_header(merged, aliases)
            if "mso" in plan2.fields.values():
                return r + 2, plan2, plan2.unknown
    return -1, None, combined_unknown


def _is_business_table(table, aliases) -> bool:
    grid = _logical_grid(table)
    if not grid:
        return False
    header_row, plan, _unknown = _find_header_plan(grid, aliases)
    return plan is not None and header_row >= 0


def _parse_table(table, table_index: int, message_id: Optional[str],
                 aliases) -> tuple[list, list]:
    """解析一张业务表：返回 (MailRow 列表, issue 列表)。"""
    grid = _logical_grid(table)
    header_row, plan, unknown = _find_header_plan(grid, aliases)
    if plan is None or header_row < 0:
        return [], []
    issues: list = []
    sources = [Source(type="mail_html", message_id=message_id,
                      table_index=table_index)]
    if plan.duplicates:
        issues.append(Issue(
            code="AMBIGUOUS_COLUMN",
            message=f"业务表表头中字段 {plan.duplicates} 对应多列，无法确定应使用哪一列",
            sources=sources,
            required_action="请回复只保留一列该字段的完整业务表（保留表头与 MSO 列）",
        ))
        return [], issues
    if unknown:
        issues.append(Issue(
            code="UNKNOWN_MAIL_HEADER",
            message="业务表存在无法识别的表头列：" + "、".join(unknown),
            sources=sources,
            required_action="如这些列包含必要资料，请原样保留表头并回复完整业务表；"
                            "同时可联系维护人员将新表头登记到 [core.header_aliases] 配置",
            blocking=False,
        ))
    header_fields = plan.fields
    data_trs = _own_rows(table)[header_row:][: _DATA_SCAN_ROWS]
    rows = []
    for ri, tr in enumerate(data_trs, start=header_row + 1):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        values = [_cell_text(c) for c in cells]
        fields: dict = {}
        for ci, value in enumerate(values):
            name = header_fields.get(ci)
            if not name or value == "":
                continue
            existing = fields.get(name)
            if existing is None:
                fields[name] = value
            elif existing != value:
                issues.append(Issue(
                    code="AMBIGUOUS_COLUMN",
                    record_key=fields.get("mso"),
                    field=name,
                    message=f"同一行内字段 {name} 出现两个不同的值：{existing!r} ≠ {value!r}",
                    sources=[Source(type="mail_html", message_id=message_id,
                                    table_index=table_index, row=ri)],
                    required_action="请回复该列只有一份取值的完整业务表",
                ))
                fields = {}
                break
        if not fields.get("mso"):
            continue
        rows.append(MailRow(fields=fields, source=Source(
            type="mail_html", message_id=message_id, table_index=table_index, row=ri)))
    return rows, issues


def _rows_signature(rows: list) -> tuple:
    return tuple(sorted((r.fields.get("mso", ""), tuple(sorted(r.fields.items()))) for r in rows))


def mail_has_business_table(html: str, text: str,
                            aliases: Optional[dict] = None) -> bool:
    """轻量判定：新写入（非引用）内容中是否存在业务表。供应用层判断
    table-only 补件是否实质性内容；不解析明细、不产生副作用。"""
    resolved = mail_aliases(aliases)
    if html and "<table" in html.lower():
        soup = BeautifulSoup(html, "lxml")
        for table in soup.find_all("table"):
            if _is_quoted(table):
                continue
            try:
                if _is_business_table(table, resolved):
                    return True
            except TableStructureError:
                # 尺寸越上限的表不可信，按无业务表处理（完整解析处会出
                # 结构化阻断问题）
                continue
    for line in _strip_text_quotes(text or ""):
        cells = [c.strip() for c in re.split(r"\t|\s{2,}", line.strip()) if c.strip()]
        if len(cells) < 2:
            continue
        plan = _resolve_header(cells, resolved)
        if "mso" in plan.fields.values():
            return True
    return False


def parse_mail_html(html: str, message_id: Optional[str],
                    aliases: Optional[dict] = None) -> MailParseResult:
    result = MailParseResult()
    resolved = mail_aliases(aliases)
    conflicts = check_alias_collisions(
        resolved, scope=set(MAIL_DEFAULT_ALIASES))
    if conflicts:
        result.issues.append(Issue(
            code="ALIAS_CONFIG_CONFLICT",
            message="；".join(conflicts),
            sources=[Source(type="mail_html", message_id=message_id)],
            required_action="请维护人员修正 [core.header_aliases] 中映射到"
                            "多个字段的同名别名后重试",
        ))
        return result
    soup = BeautifulSoup(html or "", "lxml")
    all_tables = soup.find_all("table")
    business_flags: dict = {}
    blocked_tables: list = []
    for idx, t in enumerate(all_tables):
        if _is_quoted(t):
            continue
        try:
            business_flags[idx] = _is_business_table(t, resolved)
        except TableStructureError as exc:
            blocked_tables.append((idx, str(exc)))
            business_flags[idx] = False
    candidates = []
    for idx, table in enumerate(all_tables):
        if not business_flags.get(idx):
            continue
        # 嵌套展示表：外层布局表不直接解析，交由内层真正的业务表，
        # 避免同一批行被外层（含内层全文）与内层重复提取。
        if any(business_flags.get(j) for j, inner in enumerate(all_tables)
               if inner is not table and table in inner.parents):
            continue
        candidates.append((idx, table))
    result.tables_seen = len(candidates)
    if blocked_tables:
        # 合并单元格尺寸越上限：结构不可信，整体阻断，绝不部分交付。
        result.issues.append(Issue(
            code="TABLE_STRUCTURE_INVALID",
            message="；".join(f"表 #{idx + 1}：{detail}"
                              for idx, detail in blocked_tables),
            sources=[Source(type="mail_html", message_id=message_id,
                            table_index=idx) for idx, _ in blocked_tables],
            required_action="请回复合并单元格尺寸正常的完整业务表（colspan/"
                            "rowspan 不超过 64）",
        ))
        result.rows = []
        return result
    if not candidates:
        return result
    parsed = []
    for idx, t in candidates:
        rows, issues = _parse_table(t, idx, message_id, resolved)
        result.issues.extend(issues)
        parsed.append((idx, rows))
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


_TEXT_QUOTE_BOUNDARIES = (
    re.compile(r"^\s*On\s+.+\bwrote:\s*$", re.I),
    re.compile(r"^\s*在\s*.+写道\s*[:：]\s*$"),
    re.compile(r"^\s*-{2,}\s*(?:Original Message|原始邮件|转发的邮件|Forwarded message)\s*-*\s*$", re.I),
    re.compile(r"^\s*(?:From|发件人|Sent|发送时间|To|收件人|Subject|主题)\s*[:：]", re.I),
    re.compile(r"^\s*-{5,}\s*$"),
)


def _strip_text_quotes(text: str) -> list[str]:
    """剔除纯文本引用历史（> 前缀行与常见引用边界之后的全部内容）。"""
    kept: list[str] = []
    for line in (text or "").splitlines():
        if any(pattern.match(line) for pattern in _TEXT_QUOTE_BOUNDARIES):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return kept


def parse_mail_text(text: str, message_id: Optional[str],
                    aliases: Optional[dict] = None) -> MailParseResult:
    """可明确分列的纯文本业务表：表头行含 MSO 业务列名，数据行以 Tab
    或 2+ 空格分列；引用历史行不参与。"""
    result = MailParseResult()
    resolved = mail_aliases(aliases)
    conflicts = check_alias_collisions(
        resolved, scope=set(MAIL_DEFAULT_ALIASES))
    if conflicts:
        result.issues.append(Issue(
            code="ALIAS_CONFIG_CONFLICT",
            message="；".join(conflicts),
            sources=[Source(type="mail_text", message_id=message_id)],
            required_action="请维护人员修正 [core.header_aliases] 中映射到"
                            "多个字段的同名别名后重试",
        ))
        return result
    lines = [ln for ln in _strip_text_quotes(text) if ln.strip()]
    header_idx = -1
    delim = None
    split = None
    plan: Optional[_HeaderPlan] = None
    for i, ln in enumerate(lines[:_TEXT_HEADER_SCAN_LINES]):
        candidate_delim = "\t" if "\t" in ln else (
            "  " if re.search(r"\s{2,}", ln) else None)
        if candidate_delim is None:
            continue
        candidate_split = ((lambda s: s.split("\t")) if candidate_delim == "\t"
                           else (lambda s: re.split(r"\s{2,}", s.strip())))
        candidate_plan = _resolve_header(
            [c.strip() for c in candidate_split(ln)], resolved)
        if "mso" in candidate_plan.fields.values():
            header_idx, delim, split, plan = i, candidate_delim, candidate_split, candidate_plan
            break
    if header_idx < 0 or plan is None or split is None:
        return result
    if plan.duplicates:
        result.issues.append(Issue(
            code="AMBIGUOUS_COLUMN",
            message=f"业务表表头中字段 {plan.duplicates} 对应多列，无法确定应使用哪一列",
            sources=[Source(type="mail_text", message_id=message_id)],
            required_action="请回复只保留一列该字段的完整业务表（保留表头与 MSO 列）",
        ))
        return result
    if plan.unknown:
        result.issues.append(Issue(
            code="UNKNOWN_MAIL_HEADER",
            message="业务表存在无法识别的表头列：" + "、".join(plan.unknown),
            sources=[Source(type="mail_text", message_id=message_id)],
            required_action="如这些列包含必要资料，请原样保留表头并回复完整业务表；"
                            "同时可联系维护人员将新表头登记到 [core.header_aliases] 配置",
            blocking=False,
        ))
    rows = []
    for off, ln in enumerate(lines[header_idx + 1:], start=1):
        parts = split(ln)
        if len(parts) < 4:
            break
        fields: dict = {}
        ambiguous = False
        for ci, value in enumerate(parts):
            name = plan.fields.get(ci)
            if not name:
                continue
            value = value.strip()
            if not value:
                continue
            existing = fields.get(name)
            if existing is None:
                fields[name] = value
            elif existing != value:
                result.issues.append(Issue(
                    code="AMBIGUOUS_COLUMN",
                    field=name,
                    message=f"同一行内字段 {name} 出现两个不同的值：{existing!r} ≠ {value!r}",
                    sources=[Source(type="mail_text", message_id=message_id,
                                    row=header_idx + off + 1)],
                    required_action="请回复该列只有一份取值的完整业务表",
                ))
                ambiguous = True
                break
        if ambiguous:
            continue
        if fields.get("mso"):
            rows.append(MailRow(fields=fields, source=Source(
                type="mail_text", message_id=message_id, row=header_idx + off + 1)))
    result.rows = rows
    result.tables_seen = 1 if rows else 0
    return result
