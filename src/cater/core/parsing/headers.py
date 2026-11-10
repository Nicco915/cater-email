"""表头规范化与语义列定位（plan/tasks/claude-flexible-input.md 阶段 2）。

别名集版本标记（``alias_marker``）供核心公共入口（process_job 结构指纹、
inspect_sources 预览指纹）统一标记所用别名配置。

- 表头文本规范化：NFKC、大小写折叠、去空白/换行/标点，使
  ``U'weight/kg``、``NET WEIGHT (KGS)``、``SERIAL #``、``QTY.`` 等写法
  归一到同一键；
- 语义字段 → 表头别名集，默认内置且可配置（process_job 请求
  ``header_aliases`` 可按字段追加别名）；别名只做规范化后的精确匹配，
  不做模糊匹配；
- 支持双行表头：主表头行未命中的字段可在紧随的次表头行解析
  （如 U'weight/kg、T'weight/kg 或 GROSS/NET 子表头）；
- 同一字段在同一搜索行命中多列 → 多候选，调用方必须暂停，禁止猜测。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

_PUNCT_RE = re.compile(r"[^\w一-鿿]+", re.UNICODE)

# 规范化别名键：NFKC + 小写 + 去所有非词字符（含空白、换行、标点）。
def normalize_header(text) -> str:
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).replace("\xa0", " ")
    return _PUNCT_RE.sub("", s.casefold())


def column_letter(col: int) -> str:
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


# 默认别名集（规范化后的键）。可按业务通过 ``merged_aliases`` 追加。
DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    # 配件最终发票表明细
    "part_code": ("partcode", "partnumber", "料号"),
    "qty": ("totalqty", "qty", "quantity", "申报数量"),
    "unit_price": ("unitprice", "单价"),
    "coo": ("coo", "countryoforigin", "原产国"),
    "hs_code_titled": ("hscode", "商品编码"),
    # 双行表头次行（单位重/总净重）
    "unit_weight": ("uweightkg", "uweight", "unitweight", "单件重"),
    "net_weight": ("tweightkg", "tweight", "totalnetweight", "总净重"),
    # 配件箱单 TOTAL 行
    "packages": ("package", "packages", "件数"),
    "gross_weight": ("grossweightkg", "grossweightkgs", "grossweight", "毛重"),
    # 整机机器信息表
    "model": ("model", "机型"),
    "mso": ("mso", "custpurchordermso", "mso号"),
    "serial": ("serial", "serialno", "loader", "loaderno", "序列号"),
    "pin": ("pin", "pinno"),
    "engine": ("engine", "engineno"),
    # 整机箱单重量表
    "description": ("descriptionofgoods", "partname", "description", "goods"),
    "net": ("netweightkgs", "netweight", "net"),
    "gross": ("grossweightkgs", "grossweight", "gross"),
}

# 允许在次表头行解析的字段（双行表头）。
SUBHEADER_FIELDS = frozenset({"unit_weight", "net_weight", "net", "gross"})

# 有效区扫描上限：覆盖第 56 行等深表头，同时给 xls 虚高维度
# （BIFF DIMENSION 可能报 65536 行）一个有界截止。
SCAN_ROW_LIMIT = 5000


def find_total_anchor_rows(ws, start_row: int, anchor_cols, *,
                           row_limit: int = SCAN_ROW_LIMIT):
    """语义 TOTAL 锚点行，返回 (exact, prefix)。

    - exact：anchor 列文本规范化后恰为 ``total``（真正的合计行锚点）；
    - prefix：以 ``total`` 开头但带后缀的行（如 ``TOTAL (IN WORDS)``、
      ``TOTAL AMOUNT``）——只有在语义列带数值时才可能被采用为合计行，
      金额大写/唛头行不会被误用。
    """
    exact: list[int] = []
    prefix: list[int] = []
    last = min(ws.max_row, start_row + row_limit - 1)
    for r in range(start_row, last + 1):
        for col in anchor_cols:
            value = ws.cell(r, col).value
            if not isinstance(value, str):
                continue
            norm = normalize_header(value)
            if norm == "total":
                exact.append(r)
                break
            if norm.startswith("total"):
                prefix.append(r)
                break
    return exact, prefix


def select_total_row(ws, header_row: int, value_cols, *,
                     anchor_cols=(1,), row_limit: int = SCAN_ROW_LIMIT):
    """语义 TOTAL 锚定：返回 (row, error)，error ∈ {None, "multiple"}。

    - 优先带数值的锚点（合计行通常在语义列有数值）：先精确锚点、后
      前缀锚点；都不带数值时退回纯文本精确锚点；
    - 第一个锚点即本表 TOTAL（业务表由 TOTAL 行终止，位置自由）；
    - 其后锚点与第一个之间不存在完全空行 → 同一表多张合计，
      返回 "multiple"，调用方必须暂停，不猜测取哪张；
    - 完全空行之后属于其他表块，不影响本表锚定。
    """
    from ..numbertext import to_decimal

    exact, prefix = find_total_anchor_rows(ws, header_row + 1, anchor_cols,
                                           row_limit=row_limit)
    if not exact and not prefix:
        return None, None

    def has_value(r: int) -> bool:
        return any(col and to_decimal(ws.cell(r, col).value) is not None
                   for col in value_cols)

    pool = ([r for r in exact if has_value(r)]
            or [r for r in prefix if has_value(r)]
            or exact or prefix)
    first = pool[0]
    for other in pool[1:]:
        separated = any(
            all(ws.cell(r, c).value in (None, "")
                for c in range(1, ws.max_column + 1))
            for r in range(first + 1, other)
        )
        if not separated:
            return first, "multiple"
    return first, None


def merged_aliases(overrides: Optional[dict]) -> dict[str, tuple[str, ...]]:
    """默认别名集合并调用方配置：{field: [别名, ...]} 追加到默认键。"""
    result = {name: tuple(keys) for name, keys in DEFAULT_ALIASES.items()}
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


def alias_marker(request_aliases: Optional[dict]) -> str:
    """别名集标记：默认集固定版本号；自定义别名以其规范化并集哈希标记，
    使别名配置变化体现为结构边界（且不含任何正文/秘密）。"""
    import hashlib
    import json

    if not request_aliases:
        return "default-v1"
    canonical = json.dumps(
        {name: sorted(keys) for name, keys in sorted(request_aliases.items())},
        ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"custom-{digest}"


@dataclass
class ColumnResolution:
    """一张表的语义列定位结果。"""
    header_row: int = 0
    subheader_row: Optional[int] = None
    columns: dict = field(default_factory=dict)   # field_name -> 1 基列号
    ambiguous: list = field(default_factory=list)  # [(field_name, [col, ...])]
    missing: list = field(default_factory=list)    # 必需但未找到的字段
    multiple_regions: list = field(default_factory=list)  # 多个候选表头行（不猜测）

    def position(self, field_name: str) -> Optional[str]:
        col = self.columns.get(field_name)
        return column_letter(col) if col else None


def _match_row(ws, row: int, aliases: dict, fields: list) -> dict:
    """在指定行内为每个字段找唯一匹配列；多列命中记入 ambiguous。"""
    found: dict[str, list[int]] = {name: [] for name in fields}
    for col in range(1, ws.max_column + 1):
        key = normalize_header(ws.cell(row, col).value)
        if not key:
            continue
        for name in fields:
            if key in aliases.get(name, ()):
                found[name].append(col)
    columns, ambiguous = {}, []
    for name in fields:
        cols = found[name]
        if len(cols) == 1:
            columns[name] = cols[0]
        elif len(cols) > 1:
            ambiguous.append((name, cols))
    return {"columns": columns, "ambiguous": ambiguous}


def resolve_columns(ws, fields: list, aliases: dict, *,
                    start_row: int = 1, max_scan: Optional[int] = None,
                    require: Optional[list] = None,
                    extra_row_hints: Optional[dict] = None):
    """在有效区域内定位语义字段列，允许表/列/行移动与第 56 行表头。

    - 有界全有效区扫描（默认 SCAN_ROW_LIMIT 行，覆盖深表头；xls 虚高
      维度同样有界）：一行覆盖 require 字段的别名即表头行候选；
    - 候选枚举：覆盖 = 每个主表头必需字段至少命中一列（唯一或多候选）。
      多个候选行 → ``multiple_regions``（多张业务表，调用方必须暂停）；
      唯一候选但必需字段多列命中 → ``ambiguous``（同样暂停），
      禁止因首区多候选而静默落到后一张表；
    - 主表头行未覆盖的字段在下一行（次表头）继续解析，仅限 SUBHEADER_FIELDS；
    - extra_row_hints: {字段: 行集合} 限定某些字段只能在指定行解析
      （如箱单 GROSS/NET 子表头必须与主表头相邻）；
    - 返回 ColumnResolution；调用方按 multiple_regions / ambiguous /
      missing 决定暂停。
    """
    require = require or fields
    # 表头行判定只看主表头行可解析的必需字段；双行表头的次行字段随后补解析。
    primary_require = [name for name in require if name not in SUBHEADER_FIELDS]
    limit = ws.max_row if max_scan is None else min(ws.max_row, start_row + max_scan - 1)
    limit = min(limit, start_row + SCAN_ROW_LIMIT - 1)
    candidates = []
    for row in range(start_row, limit + 1):
        primary = _match_row(ws, row, aliases, fields)
        matched = set(primary["columns"]) | {name for name, _ in primary["ambiguous"]}
        if all(name in matched for name in primary_require):
            candidates.append((row, primary))
    if not candidates:
        return ColumnResolution(missing=list(require))
    if len(candidates) > 1:
        return ColumnResolution(
            multiple_regions=[row for row, _ in candidates],
            missing=list(require),
        )
    row, primary = candidates[0]
    # 命中唯一表头行。多候选（含必需字段）原样返回，由调用方暂停。
    columns = dict(primary["columns"])
    ambiguous = list(primary["ambiguous"])
    subheader_row = None
    unresolved = [name for name in fields if name not in columns]
    sub_fields = [n for n in unresolved if n in SUBHEADER_FIELDS]
    if sub_fields and row + 1 <= ws.max_row:
        hints = extra_row_hints or {}
        allowed = {n for n in sub_fields
                   if not hints.get(n) or (row + 1) in hints[n]}
        if allowed:
            sub = _match_row(ws, row + 1, aliases, sorted(allowed))
            if sub["columns"] or sub["ambiguous"]:
                # 次表头行确实命中子表头字段时才占用；否则它可能是数据行，
                # 不得把 subheader_row 前移到数据行导致明细被跳过。
                for name, col in sub["columns"].items():
                    columns[name] = col
                ambiguous.extend(sub["ambiguous"])
                subheader_row = row + 1
    missing = [name for name in require if name not in columns]
    return ColumnResolution(header_row=row, subheader_row=subheader_row,
                            columns=columns, ambiguous=ambiguous,
                            missing=missing)


def find_header_row(ws, required: dict, max_scan: int = 40) -> Optional[int]:
    """向后兼容的简易表头行定位：required = {列号: 关键词}，全部包含即命中。

    新代码请用 resolve_columns；此函数保留给旧调用方与回归测试。
    """
    for r in range(1, min(ws.max_row, max_scan) + 1):
        ok = True
        for col, keyword in required.items():
            v = ws.cell(r, col).value
            if v is None or keyword.lower() not in str(v).replace("\xa0", " ").strip().lower():
                ok = False
                break
        if ok:
            return r
    return None
