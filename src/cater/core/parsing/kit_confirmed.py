"""已确认配件 Health Care Kit 来源解析（第三阶段 C）。

入口：``parse_confirmed_kit_sources(sources, candidate, mail_rows, aliases=None)``

与旧配件 ``CI-For Dealer`` 解析器的边界
--------------------------------------
- 本模块只在**已确认批次任务快照**的显式来源上工作：``sources`` 必须来自
  调用方对可信快照的逐文件哈希校验，``candidate.source_refs[].regions``
  给出该票已确认的发票页/箱单页/BL 页。这里不会自行挑选文件名、不会按
  sheet 顺序或 ``(2)`` 后缀定稿，也不会回退到整封父邮件。
- 正式明细逐行重新读取工作簿单元格：``candidate.facts`` 只用于事后核对，
  绝不拿 preview 的合计值填充任何行、单价、数量或净重。
- 两页发票（``Invoice`` / ``Invoice (2)``）内容必须逐行一致才可采用；
  不一致时报告 ``INVOICE_SHEET_CONFLICT`` 并不产出记录，绝不替用户选择
  某个差价口径。辅助金额列（如 P 列）与 T'Price 合计不一致时同样阻断，
  且不采用该列。
- BL 页残留旧发票号只用于冲突核对，绝不作为当前发票号来源。

业务结构
--------
发票页（Health Care Kit Invoice）::

    A=Health Care Kit No  B=CHN HS Code  C=COO  D=DESCRIPTION OF GOODS
    F=Qty/SET  G=SET QTY  H=TOTAL Qty  I=U'Price  J=T'Price
    K=单件净重  L=总净重  Q/R/S=HS/中文品名/申报要素（(2) 页为 M/N/O）

- ``D`` 列的值是**料号**；``A`` 列是 kit 号，绝不能被当成所有行的料号。
- ``F``=每套数量，``G``=套数，``H``=总数量（=F×G）。
- ``I``=单价，``J``=行总价；``K``=单件净重，``L``=总净重。
- 无料号的费用行（如 ``APPLY TO CUTOMS CHARGE``）从明细排除，但其金额
  计入发票合计核对。

箱单页（Packing list）首段::

    G=QTY(套数)  H=Packages(件数)  K=Weight(毛重)
    TARE WEIGHT 行仅作“皮重已含在毛重合计内”的核对事实，不再加一次。

箱单页后段是每套零件明细，指向另一个 ``TOTAL``（总数量/净重），只用于
与发票总净重、总数量交叉核对；绝不能把后段 TOTAL 当作整票毛重。

本模块不修改任何既有解析器行为；接口返回既有 ``PartsParseResult``，
额外语义字段放在 ``PartRecord.fields`` 与 ``result.layout`` 中，供后续
集成层按需映射。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..models import FieldValue, Issue, PartRecord, Source
from ..numbertext import (MAX_DP_PRICE, MAX_DP_QTY, MAX_DP_WEIGHT,
                          decimal_to_text, decimals_close, to_decimal)
from .headers import (SCAN_ROW_LIMIT, SUBHEADER_FIELDS, _match_row,
                      column_letter, merged_aliases, normalize_header,
                      resolve_columns, select_total_row)
from .parts import PartsParseResult
from .workbook import (SUPPORTED_SUFFIXES, UnsupportedSourceError,
                       WorkbookDependencyError, cell_value, load_workbook_pair)

# 发票号（与 parts.py 同一业务写法；不导入私有函数，保持模块独立）
INVOICE_NO_RE = re.compile(r"INVOICE\s+NO\.?\s*:?\s*([\w\-/]+)", re.IGNORECASE)
HS_CODE_VALUE_RE = re.compile(r"^\d{8,13}$")

# 预算/扫描上限（与预览一致；xls 虚高维度同样有界）
MAX_HEADER_SCAN_ROWS = 60
MAX_NOTE_SCAN_ROWS = 80

# 数值安全边界：非有限数（NaN/sNaN/Infinity）与极端指数一律结构化拒绝，
# 绝不进入算术/量化/相对容差。18 位十进制量级与 B（machine_confirmed）
# 的受控边界一致，足以覆盖真实金额/数量/重量。
_VALUE_MAX_ADJUSTED = 18

# 只做“安全性校验”、绝不参与配件明细取数的正文数值字段。
_MAIL_NUMERIC_RE = re.compile(
    r"^[+-]?(?:(?:\d[\d,.]*)(?:[eE][+-]?\d+)?"
    r"|(?:s?nan|inf(?:inity)?))$", re.IGNORECASE)

_MAIL_NUMERIC_FIELDS = frozenset({
    "amount", "net_value", "total_sales_price", "dealer_net_net",
    "inland_freight", "ocean_freight", "insurance", "others",
    "qty", "sets", "packages", "unit_price", "total_price",
    "unit_weight", "net_weight", "gross_weight", "tare_weight",
})

# 发票/箱单区域角色（candidate.source_refs[].regions[].role）
ROLE_INVOICE = "kit_invoice"
ROLE_PACKING = "kit_packing"
ROLE_APPLICATION = "kit_application"
ROLE_BL = "bl"

_KIT_ALIASES = {
    # Health Care Kit 发票页
    "kit_no": ("healthcarekitno", "kitno", "kit"),
    "hs_code_titled": ("chnhscode", "hscode", "商品编码"),
    "coo": ("coo", "countryoforigin", "原产国"),
    "part_code": ("descriptionofgoods", "partcode", "partnumber", "料号"),
    "qty_per_set": ("qtyset", "qtyperset", "每套数量"),
    "sets": ("setqty", "sets", "套数"),
    "qty": ("totalqty", "qty", "quantity", "总数量", "申报数量"),
    "unit_price": ("uprice", "unitprice", "单价"),
    "total_price": ("tprice", "totalprice", "总价"),
    "unit_weight": ("单件净重", "unitweight", "uweightkg", "uweight", "单件重"),
    "net_weight": ("总净重", "netweight", "netweightkgs", "tweightkg", "tweight"),
    "hs_code": ("hs", "hscode", "商品编码"),
    "product_name": ("中文品名", "productname", "中文名称"),
    "spec": ("申报要素", "spec", "declaration"),
    "description": ("description", "descriptionofgoods", "goodsdescription"),
    # 箱单首段（H/G/K）与后段零件明细
    "packing_sets": ("qty", "sets", "套数"),
    "packing_packages": ("packages", "package", "件数"),
    "packing_gross_weight": ("weight", "grossweight", "grossweightkgs", "毛重"),
    "packing_kit_no": ("kitno", "kit", "healthcarekitno"),
    "packing_total_qty": ("totalqty", "qty", "总数量"),
    "packing_net_weight": ("netweight", "netweightkgs", "总净重"),
    "order_code": ("ordercode", "订单号"),
    "mso": ("mso", "custpurchordermso", "mso号"),
    "machine_model": ("machinemodel", "model", "机型"),
    "machine_serial_no": ("machineserialno", "serialno", "serial", "序列号"),
    "event_description": ("eventpartdescription", "eventdescription"),
}

# 发票明细语义字段（主表头行 + 次表头行）
_INVOICE_FIELDS = ["kit_no", "hs_code_titled", "coo", "part_code",
                   "qty_per_set", "sets", "qty", "unit_price",
                   "total_price", "unit_weight", "net_weight"]
# CHN HS Code 列是辅助核对列（补充列 HS 才是输出依据），表头缺失不单独特判；
# 其余为正式明细必需字段。
_INVOICE_REQUIRE = ["kit_no", "coo", "part_code", "qty_per_set", "sets",
                    "qty", "unit_price", "total_price", "unit_weight",
                    "net_weight"]
_INVOICE_NUMBER_FIELDS = ["qty_per_set", "sets", "qty", "unit_price",
                          "total_price", "unit_weight", "net_weight"]
_SUPP_FIELDS = ["hs_code", "product_name", "spec"]

# 发票页行级/合计核对规则 ID（与 layout.checks 一起保存，便于审计）
RULE_KIT_QTY_ROW = "KIT_QTY_PER_SET_X_SETS"
RULE_KIT_AMOUNT_ROW = "KIT_PRICE_ROW"
RULE_KIT_WEIGHT_ROW = "KIT_WEIGHT_ROW"
RULE_KIT_INVOICE_AMOUNT_TOTAL = "KIT_INVOICE_TOTAL_AMOUNT"
RULE_KIT_INVOICE_NET_TOTAL = "KIT_INVOICE_TOTAL_NET"
RULE_KIT_INVOICE_SHEETS_AGREE = "KIT_INVOICE_SHEETS_AGREE"
RULE_KIT_INVOICE_AUX = "KIT_INVOICE_AUX_AMOUNT"
RULE_KIT_PACKING_TOTALS = "KIT_PACKING_TOTALS"
RULE_KIT_PACKING_TARE = "KIT_PACKING_TARE_INCLUDED"
RULE_KIT_PACKING_PARTS_TOTAL = "KIT_PACKING_PARTS_TOTAL"
RULE_KIT_NET_CROSSCHECK = "KIT_NET_CROSSCHECK"
RULE_KIT_QTY_CROSSCHECK = "KIT_QTY_CROSSCHECK"
RULE_KIT_IDENTITY = "KIT_IDENTITY"
RULE_KIT_BL_INVOICE = "KIT_BL_INVOICE"
RULE_KIT_FACT = "KIT_CONFIRMED_FACT"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _kit_aliases(overrides: Optional[dict]) -> dict:
    """默认别名 + Health Care Kit 专用别名 + 调用方追加别名（只追加不覆盖）。"""
    aliases = merged_aliases(overrides)
    for name, keys in _KIT_ALIASES.items():
        existing = list(aliases.get(name, ()))
        for key in keys:
            normalized = normalize_header(key)
            if normalized and normalized not in existing:
                existing.append(normalized)
        aliases[name] = tuple(existing)
    return aliases


def _clean_text(value) -> str:
    """表格文本：去 nbsp/首尾空白；整数值浮点不带 ``.0``。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\xa0", " ").strip()


def _identity_values(value) -> list:
    """候选身份取值归一：允许字符串或字符串列表（预览正常为列表）。"""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value
                if item is not None and str(item).strip()]
    return []


def _numeric_text(value: Optional[Decimal], max_dp: int) -> Optional[str]:
    return None if value is None else decimal_to_text(value, max_dp)


def _amount_text(value: Optional[Decimal]) -> Optional[str]:
    return _numeric_text(value, MAX_DP_PRICE)


def _weight_text(value: Optional[Decimal]) -> Optional[str]:
    return _numeric_text(value, MAX_DP_WEIGHT)


def _qty_text(value: Optional[Decimal]) -> Optional[str]:
    return _numeric_text(value, MAX_DP_QTY)


def _is_safe_decimal(value) -> bool:
    """有限、量级受控的 Decimal 才允许进入算术/量化/容差。"""
    if not isinstance(value, Decimal):
        return False
    try:
        if not value.is_finite():
            return False
        return value == 0 or abs(value.adjusted()) <= _VALUE_MAX_ADJUSTED
    except (ArithmeticError, ValueError, TypeError):
        return False


def _common_decimal(values, *, max_dp: int = MAX_DP_WEIGHT,
                   amount: bool = False) -> Optional[Decimal]:
    """多页取值全一致时返回共同值；任何一页缺失/不一致都返回 None。"""
    items = [value for value in values if _is_safe_decimal(value)]
    if not items:
        return None
    first = items[0]
    for value in items[1:]:
        if amount:
            if not _price_equal(first, value):
                return None
        elif not decimals_close(first, value, max_dp=max_dp):
            return None
    return first


def _source(filename: str, sheet: Optional[str] = None,
            cell: Optional[str] = None,
            row: Optional[int] = None) -> Source:
    return Source(type="workbook", filename=filename, sheet=sheet,
                  cell=cell, row=row)


def _is_formula(ws_formula, row: int, col: int) -> bool:
    try:
        value = ws_formula.cell(row, col).value
    except Exception:  # pragma: no cover - 只读接口异常不应中断整票
        return False
    return isinstance(value, str) and value.startswith("=")


def _checked_decimal(raw, *, issues: list, field_name: str,
                     record_key=None, filename: Optional[str] = None,
                     sheet: Optional[str] = None, row: Optional[int] = None,
                     col: Optional[int] = None, blank_issue: bool = False,
                     source: Optional[Source] = None) -> Optional[Decimal]:
    """把单元格值转成**有限、量级受控**的 Decimal；非法值登记 Issue 并返回 None。

    - NaN/sNaN/±Infinity：NON_FINITE_NUMBER；
    - 有限但 ``abs(adjusted) > 18`` 的极端指数：NUMBER_OUT_OF_RANGE；
    - 空值在 ``blank_issue=False`` 时视为可空；非空但不可解析：INVALID_NUMBER。
    任何非 None 返回值都可安全进入算术/quantize/decimals_close。
    """
    cell = f"{column_letter(col)}{row}" if col and row else None

    def _report(code: str, reason: str) -> None:
        if source is not None:
            sources = [source]
            if source.detail:
                location = source.detail
            elif source.sheet and source.cell:
                location = f"{source.sheet}!{source.cell}"
            else:
                location = source.cell or source.type
        elif filename is not None:
            sources = [_source(filename, sheet, cell, row)]
            location = f"{sheet}!{cell}" if cell else f"{sheet or filename}"
        else:
            sources = [Source(type="reference",
                              detail=f"candidate.facts.{field_name}")]
            location = f"candidate.facts.{field_name}"
        issues.append(Issue(
            code=code,
            record_key=record_key,
            field=field_name,
            message=f"{location} 的 {field_name} {reason}（{raw!r}），"
                    "未进入算术/量化/容差比较",
            sources=sources,
            required_action="请提供有限的合理量级数值后重发",
        ))

    text = _clean_text(raw)
    value = to_decimal(raw)
    if value is None:
        if not text and not blank_issue:
            return None
        _report("INVALID_NUMBER", "缺失或无法解析为数字")
        return None
    if not value.is_finite():
        _report("NON_FINITE_NUMBER", "不是有限数值（NaN/Infinity）")
        return None
    try:
        if value != 0 and abs(value.adjusted()) > _VALUE_MAX_ADJUSTED:
            _report("NUMBER_OUT_OF_RANGE",
                    f"超出支持范围（|adjusted|>{_VALUE_MAX_ADJUSTED}）")
            return None
    except (ArithmeticError, ValueError, TypeError):
        _report("INVALID_NUMBER", "无法可靠判定数值量级")
        return None
    return value


def _read_number(ws, ws_formula, row: int, col: int, *, filename: str,
                 sheet: str, field_name: str, record_key,
                 issues: list, blank_issue: bool = True) -> Optional[Decimal]:
    """读取数值单元格：公式缓存缺失由 cell_value 登记；非有限/极端值拦截。"""
    if not col:
        return None
    raw = cell_value(ws, ws_formula, row, col, record_key=record_key,
                     field=field_name, filename=filename, issues=issues)
    return _checked_decimal(
        raw, issues=issues, field_name=field_name, record_key=record_key,
        filename=filename, sheet=sheet, row=row, col=col,
        blank_issue=blank_issue and not _is_formula(ws_formula, row, col))


def _match_row_fields(ws, row: int, aliases: dict, names: list) -> tuple:
    """在指定行内匹配字段；返回 (columns, ambiguous)。"""
    if row < 1:
        return {}, []
    matched = _match_row(ws, row, aliases, list(names))
    return matched["columns"], matched["ambiguous"]


def _find_header_with_keys(ws, keys: tuple, *, start_row: int = 1,
                           end_row: Optional[int] = None) -> Optional[tuple]:
    """查找同时包含全部规范化表头键的行；返回 (row, {key: col})。"""
    end = min(ws.max_row, end_row or (start_row + MAX_HEADER_SCAN_ROWS - 1))
    for row in range(start_row, end + 1):
        found: dict[str, int] = {}
        for col in range(1, ws.max_column + 1):
            key = normalize_header(ws.cell(row, col).value)
            if key and key in keys and key not in found:
                found[key] = col
        if all(key in found for key in keys):
            return row, found
    return None


def _first_total_row(ws, start_row: int, *, end_row: Optional[int] = None,
                     anchor_col: int = 1) -> Optional[int]:
    """第一个语义 ``TOTAL`` 行（col1 规范化后恰为 total），有界扫描。

    箱单同一 sheet 有上下两个 TOTAL 表块，不能用 ``select_total_row``
    一次性扫描整页（它会把两张合计行判为 multiple）；这里按区域边界找。
    """
    limit = min(ws.max_row, start_row + SCAN_ROW_LIMIT - 1)
    if end_row is not None:
        limit = min(limit, end_row)
    for row in range(start_row, limit + 1):
        if normalize_header(ws.cell(row, anchor_col).value) == "total":
            return row
    return None


def _sheet_has_kit_structure(wb_values, aliases: dict) -> bool:
    """结构辅助：该工作簿是否像 Health Care Kit（用于未确认区域的告警）。"""
    invoice_keys = ("healthcarekitno",)
    packing_keys = ("kit", "packages")
    for name in wb_values.sheetnames:
        ws = wb_values[name]
        if _find_header_with_keys(ws, invoice_keys):
            return True
        if _find_header_with_keys(ws, packing_keys):
            return True
        if (_find_header_with_keys(ws, ("kitno", "netweight", "totalqty"))):
            return True
    return False


def _json_source(source: Optional[Source]) -> Optional[dict]:
    return source.to_dict() if source is not None else None


def _source_from_dict(value) -> Optional[Source]:
    if not isinstance(value, dict):
        return None
    return Source(
        type=str(value.get("type") or "workbook"),
        message_id=value.get("message_id"),
        filename=value.get("filename"),
        sheet=value.get("sheet"),
        cell=value.get("cell"),
        table_index=value.get("table_index"),
        row=value.get("row"),
        detail=value.get("detail"),
    )


# ---------------------------------------------------------------------------
# 解析结果（模块内部）
# ---------------------------------------------------------------------------


@dataclass
class _InvoiceParse:
    filename: str
    sheet: str
    region: dict
    header_row: int = 0
    subheader_row: Optional[int] = None
    total_row: Optional[int] = None
    columns: dict = field(default_factory=dict)
    supplementary: dict = field(default_factory=dict)
    invoice_no: Optional[str] = None
    invoice_source: Optional[Source] = None
    records: list = field(default_factory=list)
    fee_rows: list = field(default_factory=list)
    total_amount: Optional[Decimal] = None
    total_net: Optional[Decimal] = None
    total_qty: Optional[Decimal] = None
    amount_consistent: Optional[bool] = None
    net_consistent: Optional[bool] = None
    aux_conflicts: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    valid: bool = False
    structure_ok: bool = True
    identity_ok: bool = True
    region_ok: bool = True

    def summary(self) -> dict:
        return {
            "filename": self.filename,
            "sheet": self.sheet,
            "header_row": self.header_row or None,
            "subheader_row": self.subheader_row,
            "total_row": self.total_row,
            "columns": {name: column_letter(col)
                        for name, col in sorted(self.columns.items())},
            "supplementary": {name: column_letter(col)
                              for name, col in sorted(self.supplementary.items())},
            "invoice_no": self.invoice_no,
            "record_count": len(self.records),
            "fee_rows": self.fee_rows,
            "total_amount": _amount_text(self.total_amount),
            "total_net": _weight_text(self.total_net),
            "total_qty": _qty_text(self.total_qty),
            "amount_consistent": self.amount_consistent,
            "net_consistent": self.net_consistent,
            "aux_conflicts": self.aux_conflicts,
            "valid": self.valid,
            "structure_ok": self.structure_ok,
            "identity_ok": self.identity_ok,
            "region_ok": self.region_ok,
            "region": self.region,
        }

    def business_records(self) -> list:
        """用于两页发票逐行比较的规范业务行（不含辅助列/内部计算列）。"""
        return [{
            "part_code": r.part_code,
            "kit_no": _value_text(r.fields.get("kit_no")),
            "qty_per_set": _value_dec(r.fields.get("qty_per_set")),
            "sets": _value_dec(r.fields.get("sets")),
            "qty": _value_dec(r.fields.get("qty")),
            "unit_price": _value_dec(r.fields.get("unit_price")),
            "total_price": _value_dec(r.fields.get("total_price")),
            "unit_weight": _value_dec(r.fields.get("unit_weight")),
            "net_weight": _value_dec(r.fields.get("net_weight")),
            "coo": _value_text(r.fields.get("coo")),
            "hs_code": _value_text(r.fields.get("hs_code")),
            "product_name": _value_text(r.fields.get("product_name")),
            "spec": _value_text(r.fields.get("spec")),
            "description": _value_text(r.fields.get("description")),
        } for r in self.records]


@dataclass
class _PackingParse:
    filename: str
    sheet: str
    region: dict
    header_row: int = 0
    total_row: Optional[int] = None
    columns: dict = field(default_factory=dict)
    gaps: dict = field(default_factory=dict)
    sets: Optional[Decimal] = None
    packages: Optional[Decimal] = None
    gross_weight: Optional[Decimal] = None
    sets_sum: Optional[Decimal] = None
    packages_sum: Optional[Decimal] = None
    gross_sum: Optional[Decimal] = None
    gross_sum_without_tare: Optional[Decimal] = None
    tare_weight: Optional[Decimal] = None
    tare_row: Optional[int] = None
    tare_included: Optional[bool] = None
    parts_header_row: Optional[int] = None
    parts_total_row: Optional[int] = None
    parts_columns: dict = field(default_factory=dict)
    parts_total_qty: Optional[Decimal] = None
    parts_net_weight: Optional[Decimal] = None
    parts_qty_sum: Optional[Decimal] = None
    parts_net_sum: Optional[Decimal] = None
    parts_rows: int = 0
    checks: list = field(default_factory=list)
    valid: bool = False
    structure_ok: bool = True
    region_ok: bool = True

    def summary(self) -> dict:
        return {
            "filename": self.filename,
            "sheet": self.sheet,
            "header_row": self.header_row or None,
            "total_row": self.total_row,
            "columns": {name: column_letter(col)
                        for name, col in sorted(self.columns.items())},
            "sets": _qty_text(self.sets),
            "packages": _qty_text(self.packages),
            "gross_weight": _weight_text(self.gross_weight),
            "tare_weight": _weight_text(self.tare_weight),
            "tare_row": self.tare_row,
            "tare_included": self.tare_included,
            "parts_header_row": self.parts_header_row,
            "parts_total_row": self.parts_total_row,
            "parts_columns": {name: column_letter(col)
                              for name, col in sorted(self.parts_columns.items())
                              if col},
            "parts_total_qty": _qty_text(self.parts_total_qty),
            "parts_net_weight": _weight_text(self.parts_net_weight),
            "parts_rows": self.parts_rows,
            "parts_qty_sum": _qty_text(self.parts_qty_sum),
            "parts_net_sum": _weight_text(self.parts_net_sum),
            "valid": self.valid,
            "structure_ok": self.structure_ok,
            "region_ok": self.region_ok,
            "region": self.region,
        }


def _value_text(field_value: Optional[FieldValue]) -> str:
    if field_value is None or field_value.value is None:
        return ""
    return _clean_text(field_value.value)


def _value_dec(field_value: Optional[FieldValue]) -> Optional[Decimal]:
    if field_value is None or field_value.value is None:
        return None
    value = to_decimal(field_value.value)
    return value if _is_safe_decimal(value) else None


# ---------------------------------------------------------------------------
# 来源/区域校验
# ---------------------------------------------------------------------------


def _normalise_refs(candidate: dict, issues: list) -> list:
    refs = candidate.get("source_refs") if isinstance(candidate, dict) else None
    if not isinstance(refs, list) or not refs:
        issues.append(Issue(
            code="CONFIRMED_SOURCE_REFS_MISSING",
            message="已确认候选缺少 source_refs，无法确定本票资产的已确认区域",
            required_action="请重新确认本票并生成可信快照",
        ))
        return []
    result = []
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            issues.append(Issue(
                code="CONFIRMED_SOURCE_REF_INVALID",
                message=f"source_refs[{index}] 不是对象",
                required_action="请重新确认本票并生成可信快照",
            ))
            continue
        sha = str(ref.get("sha256") or "").strip().lower()
        filenames = ref.get("filenames")
        if not isinstance(filenames, list):
            single = ref.get("filename")
            filenames = [single] if single else []
        filenames = [str(name) for name in filenames if str(name).strip()]
        regions = ref.get("regions")
        if regions is not None and not isinstance(regions, list):
            issues.append(Issue(
                code="CONFIRMED_REGION_INVALID",
                message=f"source_refs[{index}] 的 regions 不是列表",
                required_action="请重新确认本票并生成可信快照",
            ))
            regions = []
        if not sha:
            issues.append(Issue(
                code="CONFIRMED_SOURCE_REF_INVALID",
                message=f"source_refs[{index}] 缺少 sha256，无法与可信快照对齐",
                required_action="请重新确认本票并生成可信快照",
            ))
        result.append({"sha256": sha, "filenames": filenames,
                       "regions": list(regions or []), "index": index})
    return result


def _normalise_sources(sources, refs: list, issues: list) -> list:
    """把调用方 sources 对齐到 candidate.source_refs，并做哈希/重复校验。"""
    if not isinstance(sources, list) or not sources:
        issues.append(Issue(
            code="MISSING_CONFIRMED_SOURCE",
            message="已确认本票未提供任何资产来源",
            required_action="请重新确认本票并生成可信快照",
        ))
        return []
    by_sha = {ref["sha256"]: ref for ref in refs if ref.get("sha256")}
    matched: list = []
    seen_sha: set = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            issues.append(Issue(
                code="UNAUTHORIZED_SOURCE",
                message=f"sources[{index}] 不是对象，已拒绝",
                required_action="请重新确认本票并生成可信快照",
            ))
            continue
        raw_path = str(source.get("path") or "").strip()
        filename = str(source.get("filename")
                       or (Path(raw_path).name if raw_path else "")).strip()
        sha = str(source.get("sha256") or "").strip().lower()
        ref = by_sha.get(sha) if sha else None
        if ref is None and not sha:
            candidates = [r for r in refs if filename and filename in r["filenames"]]
            if len(candidates) == 1:
                ref = candidates[0]
        if ref is None:
            issues.append(Issue(
                code="UNAUTHORIZED_SOURCE",
                message=f"来源 {filename or raw_path or index} 不在已确认本票资产清单内"
                        "或哈希不符，已拒绝",
                sources=[_source(filename or raw_path or None)],
                required_action="请重新确认本票并生成可信快照",
            ))
            continue
        if sha and sha in seen_sha:
            issues.append(Issue(
                code="DUPLICATE_SOURCE",
                message=f"来源 {filename} 与已处理资产重复（sha256={sha[:12]}），"
                        "只解析一次",
                sources=[_source(filename)],
                blocking=False,
            ))
            continue
        if not raw_path or not Path(raw_path).is_file():
            issues.append(Issue(
                code="MISSING_CONFIRMED_SOURCE",
                message=f"已确认资产 {filename} 的文件不存在或不可读：{raw_path!r}",
                sources=[_source(filename)],
                required_action="请重新确认本票并生成可信快照",
            ))
            continue
        try:
            actual_sha = hashlib.sha256(Path(raw_path).read_bytes()).hexdigest()
        except OSError as exc:
            issues.append(Issue(
                code="MISSING_CONFIRMED_SOURCE",
                message=f"已确认资产 {filename} 无法读取：{type(exc).__name__}",
                sources=[_source(filename)],
                required_action="请重新确认本票并生成可信快照",
            ))
            continue
        if ref.get("sha256") and actual_sha != ref["sha256"]:
            issues.append(Issue(
                code="SOURCE_HASH_MISMATCH",
                message=f"资产 {filename} 实际哈希 {actual_sha[:12]} 与已确认快照 "
                        f"{ref['sha256'][:12]} 不符，已拒绝解析",
                sources=[_source(filename)],
                required_action="请重新确认本票并生成可信快照",
            ))
            continue
        seen_sha.add(actual_sha)
        matched.append({"source": source, "ref": ref,
                        "path": raw_path, "filename": filename,
                        "sha256": actual_sha})
    matched_refs = {id(item["ref"]) for item in matched}
    for ref in refs:
        if id(ref) not in matched_refs:
            name = ", ".join(ref["filenames"]) or (ref["sha256"][:12] or "?")
            issues.append(Issue(
                code="MISSING_CONFIRMED_SOURCE",
                message=f"已确认资产 {name} 未出现在 sources 中，不能跳过",
                sources=[_source(name)],
                required_action="请重新确认本票并生成可信快照",
            ))
    return matched


def _check_region_contract(region: dict, *, filename: str, sheet: str,
                           header_row: int, total_row: Optional[int],
                           columns: dict, issues: list) -> bool:
    """已确认区域坐标复核：sheet/表头行/合计行/列偏移任何不符都必须可见。"""
    problems = []
    expected_header = region.get("header_row")
    if isinstance(expected_header, int) and expected_header > 0 \
            and expected_header != header_row:
        problems.append(f"表头行 已确认{expected_header} 实际{header_row}")
    expected_total = region.get("total_row")
    if isinstance(expected_total, int) and expected_total > 0 \
            and total_row is not None and expected_total != total_row:
        problems.append(f"合计行 已确认{expected_total} 实际{total_row}")
    expected_columns = region.get("columns")
    if isinstance(expected_columns, dict):
        for name, expected_letter in expected_columns.items():
            if not expected_letter:
                continue
            actual_col = columns.get(name)
            if not actual_col:
                problems.append(
                    f"{name} 列 已确认{str(expected_letter).strip().upper()} "
                    "实际缺失")
                continue
            actual_letter = column_letter(actual_col)
            if str(expected_letter).strip().upper() != actual_letter:
                problems.append(
                    f"{name} 列 已确认{str(expected_letter).strip().upper()} "
                    f"实际{actual_letter}")
    if problems:
        issues.append(Issue(
            code="CONFIRMED_REGION_MISMATCH",
            message=f"已确认区域 {sheet} 与当前工作簿结构不一致："
                    + "；".join(problems) + "；未按偏移猜测继续定稿",
            sources=[_source(filename, sheet)],
            required_action="请重新确认本票区域后重试",
        ))
        return False
    return True


# ---------------------------------------------------------------------------
# 发票页
# ---------------------------------------------------------------------------


def _parse_invoice_no(ws, filename: str, issues: list) -> tuple:
    seen: dict[str, Source] = {}
    for row in range(1, min(ws.max_row, 16) + 1):
        for col in range(1, min(ws.max_column, 12) + 1):
            value = ws.cell(row, col).value
            if not isinstance(value, str) or "INVOICE" not in value.upper():
                continue
            match = INVOICE_NO_RE.search(value.replace("\n", " "))
            if not match:
                continue
            number = match.group(1).strip()
            seen.setdefault(number, _source(
                filename, ws.title, ws.cell(row, col).coordinate, row))
    if len(seen) > 1:
        issues.append(Issue(
            code="INVOICE_NO_CONFLICT",
            message=f"发票页 {ws.title} 出现多个不同发票号：{sorted(seen)}",
            sources=list(seen.values()),
            required_action="请确认唯一当前发票号后重发",
        ))
        return None, None
    if not seen:
        return None, None
    number, source = next(iter(seen.items()))
    return number, source


def _supplementary_columns(ws, aliases: dict, header_row: int,
                           subheader_row: Optional[int],
                           filename: str, issues: list) -> dict:
    """定位 HS/中文品名/申报要素（发票页表头区右侧有标题列）。"""
    candidates = []
    if subheader_row:
        candidates.append(subheader_row)
    candidates.append(header_row)
    for row in candidates:
        columns, ambiguous = _match_row_fields(ws, row, aliases, _SUPP_FIELDS)
        if ambiguous:
            issues.append(Issue(
                code="SUPPLEMENTARY_COLUMNS_INVALID",
                message=f"发票页 {ws.title} 第 {row} 行补充列存在多候选：{ambiguous}，"
                        "不能猜测采用哪一列",
                sources=[_source(filename, ws.title)],
                required_action="请确认 HS/中文品名/申报要素 唯一列后重发",
            ))
            return {}
        if len(columns) == len(_SUPP_FIELDS):
            return columns
    missing = [name for name in _SUPP_FIELDS]
    issues.append(Issue(
        code="SUPPLEMENTARY_COLUMNS_MISSING",
        message=f"发票页 {ws.title} 未定位到完整补充列（HS/中文品名/申报要素），"
                f"缺失 {missing}；缺中文品名/HS 不能输出",
        sources=[_source(filename, ws.title)],
        required_action="请补齐 HS/中文品名/申报要素 列后重发",
    ))
    return {}


def _parse_invoice_sheet(wb_values, wb_formula, filename: str, sheet: str,
                         region: dict, aliases: dict, issues: list,
                         expected_invoice_nos: set) -> _InvoiceParse:
    info = _InvoiceParse(filename=filename, sheet=sheet, region=region)
    ws = wb_values[sheet]
    ws_f = wb_formula[sheet]

    resolution = resolve_columns(ws, _INVOICE_FIELDS, aliases,
                                 require=_INVOICE_REQUIRE)
    if resolution.multiple_regions:
        issues.append(Issue(
            code="KIT_INVOICE_INVALID",
            message=f"发票页 {sheet} 存在多个候选表头区 "
                    f"{resolution.multiple_regions}，无法唯一确定明细区域",
            sources=[_source(filename, sheet)],
            required_action="请确认唯一发票明细表后重发",
        ))
        info.valid = info.structure_ok = False
        return info
    if resolution.missing or resolution.ambiguous or not resolution.header_row:
        issues.append(Issue(
            code="KIT_INVOICE_INVALID",
            message=f"发票页 {sheet} 表头结构不完整：缺失={resolution.missing} "
                    f"多候选={resolution.ambiguous}",
            sources=[_source(filename, sheet)],
            required_action="请确认 Health Care Kit 发票表头 "
                            "（Kit No/COO/料号/Qty per set/Set qty/Total qty/"
                            "U'Price/T'Price/单件净重/总净重）完整后重发",
        ))
        info.valid = info.structure_ok = False
        return info

    info.header_row = resolution.header_row
    info.subheader_row = resolution.subheader_row
    info.columns = dict(resolution.columns)
    info.supplementary = _supplementary_columns(
        ws, aliases, resolution.header_row, resolution.subheader_row,
        filename, issues)
    structure_ok = bool(info.supplementary)
    if not info.supplementary:
        info.structure_ok = False

    # 合计行：TOTAL AMOUNT (IN WORDS) 是前缀锚点，J/L 有值才采用
    total_row, total_error = select_total_row(
        ws, resolution.subheader_row or resolution.header_row,
        [info.columns["total_price"], info.columns["net_weight"]])
    if total_error == "multiple":
        issues.append(Issue(
            code="MULTIPLE_TOTAL_ROWS",
            message=f"发票页 {sheet} 表头下方存在多张同表块合计行，不能确定 TOTAL 锚点",
            sources=[_source(filename, sheet)],
            required_action="请确认本票唯一发票合计行后重发",
        ))
        info.structure_ok = False
    if total_row is None:
        issues.append(Issue(
            code="INVOICE_TOTAL_NOT_FOUND",
            message=f"发票页 {sheet} 未找到 TOTAL AMOUNT 合计行，不能核对整票金额/净重",
            sources=[_source(filename, sheet)],
            required_action="请确认发票页包含 TOTAL AMOUNT 合计行",
        ))
        info.structure_ok = False
    info.total_row = total_row

    region_ok = _check_region_contract(
        region, filename=filename, sheet=sheet,
        header_row=info.header_row, total_row=total_row,
        columns=info.columns, issues=issues)
    info.region_ok = region_ok

    info.invoice_no, info.invoice_source = _parse_invoice_no(
        ws, filename, issues)
    if not info.invoice_no:
        issues.append(Issue(
            code="INVOICE_NO_NOT_FOUND",
            message=f"发票页 {sheet} 未读取到 INVOICE NO.，不能用预览事实代替",
            sources=[_source(filename, sheet)],
            required_action="请确认发票页包含 INVOICE NO. 后重发",
        ))
        info.identity_ok = False
    elif expected_invoice_nos and info.invoice_no not in expected_invoice_nos:
        issues.append(Issue(
            code="SOURCE_IDENTITY_MISMATCH",
            message=f"发票页 {sheet} 的发票号 {info.invoice_no} 与已确认身份 "
                    f"{sorted(expected_invoice_nos)} 不一致，疑似错票来源，未采用",
            sources=[info.invoice_source] if info.invoice_source else
                    [_source(filename, sheet)],
            required_action="请确认附件属于本票后重新确认",
        ))
        info.identity_ok = False

    if not total_row or not info.supplementary:
        info.valid = False
        return info

    columns = info.columns
    supp = info.supplementary
    first_row = (resolution.subheader_row or resolution.header_row) + 1
    last_row = min(total_row - 1, first_row + SCAN_ROW_LIMIT - 1)

    def cell_src(col: int, row: int) -> Source:
        return _source(filename, sheet, f"{column_letter(col)}{row}", row)

    amount_cell_sum = Decimal("0")
    net_cell_sum = Decimal("0")
    qty_cell_sum = Decimal("0")
    fee_amount_sum = Decimal("0")
    kit_nos: set = set()
    detail_last = first_row - 1

    for row in range(first_row, last_row + 1):
        part_raw = ws.cell(row, columns["part_code"]).value
        if _clean_text(part_raw) == "":
            # 无料号：费用行/说明行不属于明细；费用金额仍须进入合计核对
            amount = _read_number(
                ws, ws_f, row, columns["total_price"], filename=filename,
                sheet=sheet, field_name="total_price", record_key=None,
                issues=issues, blank_issue=False)
            net = _read_number(
                ws, ws_f, row, columns["net_weight"], filename=filename,
                sheet=sheet, field_name="net_weight", record_key=None,
                issues=issues, blank_issue=False)
            if amount is not None or net is not None:
                fee = {
                    "row": row,
                    "amount": _amount_text(amount),
                    "net_weight": _weight_text(net),
                    "source": _json_source(cell_src(columns["total_price"], row)),
                }
                info.fee_rows.append(fee)
                if amount is not None:
                    amount_cell_sum += amount
                    fee_amount_sum += amount
                if net is not None:
                    net_cell_sum += net
            continue

        detail_last = row
        part_code = _clean_text(part_raw)
        kit_raw = ws.cell(row, columns["kit_no"]).value
        kit_no = _clean_text(kit_raw)
        if kit_no:
            kit_nos.add(kit_no)

        raw_values = {}
        numbers = {}
        incomplete = []
        for name in _INVOICE_NUMBER_FIELDS:
            raw = cell_value(ws, ws_f, row, columns[name],
                             record_key=part_code, field=name,
                             filename=filename, issues=issues)
            raw_values[name] = raw
            value = _checked_decimal(
                raw, issues=issues, field_name=name, record_key=part_code,
                filename=filename, sheet=sheet, row=row,
                col=columns[name],
                blank_issue=not _is_formula(ws_f, row, columns[name]))
            numbers[name] = value
            if value is None:
                incomplete.append(name)
        if incomplete:
            issues.append(Issue(
                code="INVALID_DETAIL_ROW",
                record_key=part_code,
                field=",".join(incomplete),
                message=f"发票页 {sheet} 第 {row} 行料号 {part_code!r} 的 "
                        f"{incomplete} 缺失或不可解析，该行未纳入明细",
                sources=[cell_src(columns[name], row) for name in incomplete],
                required_action="请补齐该行数量/单价/金额/重量后重发",
            ))

        # 单元格级合计（即使该行因缺字段未建记录，也要用于合计核对）
        if numbers["total_price"] is not None:
            amount_cell_sum += numbers["total_price"]
        if numbers["net_weight"] is not None:
            net_cell_sum += numbers["net_weight"]
        if numbers["qty"] is not None:
            qty_cell_sum += numbers["qty"]

        if incomplete:
            continue

        qty = numbers["qty"]
        price = numbers["unit_price"]
        total_price = numbers["total_price"]
        unit_weight = numbers["unit_weight"]
        net_weight = numbers["net_weight"]
        qty_per_set = numbers["qty_per_set"]
        sets = numbers["sets"]

        if qty_per_set is not None and sets is not None:
            ok = decimals_close(qty, qty_per_set * sets, max_dp=MAX_DP_QTY)
            _check(issues, info, RULE_KIT_QTY_ROW, part_code, ok,
                   f"每套数量{qty_per_set}×套数{sets}={qty_per_set * sets} vs 总数量{qty}")
            if not ok:
                _row_issue(issues, "QTY_MISMATCH", part_code, "qty",
                           f"料号 {part_code} 总数量 {qty} 与 每套数量×套数 "
                           f"{qty_per_set * sets} 不一致", filename, sheet,
                           columns, row)
        if price is not None and total_price is not None:
            ok = _price_equal(price * qty, total_price)
            _check(issues, info, RULE_KIT_AMOUNT_ROW, part_code, ok,
                   f"单价{price}×总数量{qty}={price * qty} vs 行总价{total_price}")
            if not ok:
                _row_issue(issues, "AMOUNT_MISMATCH", part_code, "total_price",
                           f"料号 {part_code} 行总价 {total_price} 与 单价×总数量 "
                           f"{price * qty} 不一致", filename, sheet, columns, row,
                           extra_fields=("unit_price", "qty"))
        if unit_weight is not None and net_weight is not None:
            ok = decimals_close(unit_weight * qty, net_weight,
                                max_dp=MAX_DP_WEIGHT)
            _check(issues, info, RULE_KIT_WEIGHT_ROW, part_code, ok,
                   f"单件净重{unit_weight}×总数量{qty}={unit_weight * qty} "
                   f"vs 总净重{net_weight}")
            if not ok:
                _row_issue(issues, "WEIGHT_MISMATCH", part_code, "net_weight",
                           f"料号 {part_code} 总净重 {net_weight} 与 单件净重×总数量 "
                           f"{unit_weight * qty} 不一致", filename, sheet, columns,
                           row, extra_fields=("unit_weight", "qty"))

        coo_raw = ws.cell(row, columns["coo"]).value
        coo = _clean_text(coo_raw)
        hs_raw = ws.cell(row, supp["hs_code"]).value
        hs = _clean_text(hs_raw)
        name_raw = ws.cell(row, supp["product_name"]).value
        name = _clean_text(name_raw)
        spec_raw = ws.cell(row, supp["spec"]).value
        spec = _clean_text(spec_raw)
        desc_raw = None
        desc_col = columns.get("description")
        if desc_col:
            desc_raw = ws.cell(row, desc_col).value
        desc = _clean_text(desc_raw)

        if not coo:
            _missing_issue(issues, part_code, "coo",
                           f"料号 {part_code} 缺少原产国（COO）",
                           [cell_src(columns["coo"], row)])
        if not hs:
            _missing_issue(issues, part_code, "hs_code",
                           f"料号 {part_code} 缺少 HS 编码（补充列），不能输出",
                           [cell_src(supp["hs_code"], row)])
        elif not HS_CODE_VALUE_RE.match(hs):
            issues.append(Issue(
                code="HS_FORMAT_INVALID",
                record_key=part_code,
                field="hs_code",
                message=f"料号 {part_code} 的 HS 编码 {hs!r} 格式无效（应为 8-13 位数字）",
                sources=[cell_src(supp["hs_code"], row)],
                required_action="请核对 HS 编码后重发",
            ))
        if not name:
            _missing_issue(issues, part_code, "product_name",
                           f"料号 {part_code} 缺少中文品名，不能输出",
                           [cell_src(supp["product_name"], row)])
        if not spec:
            _missing_issue(issues, part_code, "spec",
                           f"料号 {part_code} 缺少申报要素，不能输出",
                           [cell_src(supp["spec"], row)])

        hs_titled_col = columns.get("hs_code_titled")
        hs_titled_raw = (ws.cell(row, hs_titled_col).value
                         if hs_titled_col else None)
        hs_titled = _clean_text(hs_titled_raw)
        if hs and hs_titled and hs != hs_titled:
            issues.append(Issue(
                code="HS_CODE_DVERSUS_M",
                record_key=part_code,
                field="hs_code",
                message=f"料号 {part_code} 的 CHN HS Code {hs_titled} 与补充列 HS "
                        f"{hs} 不一致，按既有规则以补充列为准",
                sources=[(cell_src(hs_titled_col, row)
                          if hs_titled_col else _source(filename, sheet, row=row)),
                         cell_src(supp["hs_code"], row)],
                required_action="无需操作；如需以 CHN HS Code 列为准请明确回复",
                blocking=False,
            ))
            _check(issues, info, "KIT_HS_TITLED_VS_SUPP", part_code, False,
                   f"CHN HS Code={hs_titled} 补充列HS={hs}")
        elif hs and hs_titled:
            _check(issues, info, "KIT_HS_TITLED_VS_SUPP", part_code, True,
                   f"CHN HS Code={hs_titled} 补充列HS={hs}")

        rec = PartRecord(part_code=part_code, row_index=row)
        rec.fields = {
            "part_code": FieldValue(part_raw, part_code,
                                    cell_src(columns["part_code"], row)),
            "kit_no": FieldValue(kit_raw, kit_no or None,
                                 cell_src(columns["kit_no"], row)),
            "qty_per_set": FieldValue(raw_values["qty_per_set"], qty_per_set,
                                      cell_src(columns["qty_per_set"], row)),
            "sets": FieldValue(raw_values["sets"], sets,
                               cell_src(columns["sets"], row)),
            "qty": FieldValue(raw_values["qty"], qty,
                              cell_src(columns["qty"], row)),
            "unit_price": FieldValue(raw_values["unit_price"], price,
                                     cell_src(columns["unit_price"], row)),
            "total_price": FieldValue(raw_values["total_price"], total_price,
                                      cell_src(columns["total_price"], row)),
            "unit_weight": FieldValue(raw_values["unit_weight"], unit_weight,
                                      cell_src(columns["unit_weight"], row)),
            "net_weight": FieldValue(raw_values["net_weight"], net_weight,
                                     cell_src(columns["net_weight"], row)),
            "coo": FieldValue(coo_raw, coo or None,
                              cell_src(columns["coo"], row)),
            "hs_code": FieldValue(hs_raw, hs or None,
                                  cell_src(supp["hs_code"], row)),
            "hs_code_titled": FieldValue(
                hs_titled_raw, hs_titled or None,
                cell_src(hs_titled_col, row) if hs_titled_col
                else _source(filename, sheet, row=row)),
            "product_name": FieldValue(name_raw, name or None,
                                       cell_src(supp["product_name"], row)),
            "spec": FieldValue(spec_raw, spec or None,
                               cell_src(supp["spec"], row)),
        }
        if desc_col:
            rec.fields["description"] = FieldValue(
                desc_raw, desc or None, cell_src(desc_col, row))
        info.records.append(rec)

    if len(kit_nos) > 1:
        issues.append(Issue(
            code="KIT_NO_CONFLICT",
            message=f"发票页 {sheet} 明细出现多个 kit 号：{sorted(kit_nos)}，"
                    "不能把任一 kit 号当作所有行料号",
            sources=[_source(filename, sheet)],
            required_action="请确认本票唯一的 Health Care Kit No 后重发",
        ))
        info.structure_ok = False

    info.total_amount = _read_number(
        ws, ws_f, total_row, columns["total_price"],
        filename=filename, sheet=sheet, field_name="total_amount",
        record_key=None, issues=issues)
    info.total_net = _read_number(
        ws, ws_f, total_row, columns["net_weight"],
        filename=filename, sheet=sheet, field_name="total_net_weight",
        record_key=None, issues=issues)
    info.total_qty = qty_cell_sum

    if info.total_amount is not None:
        ok = _price_equal(amount_cell_sum, info.total_amount)
        _check(issues, info, RULE_KIT_INVOICE_AMOUNT_TOTAL, None, ok,
               f"明细/费用金额合计{amount_cell_sum} vs TOTAL行{info.total_amount}"
               f"（含费用 {fee_amount_sum}）")
        info.amount_consistent = ok
        if not ok:
            issues.append(Issue(
                code="INVOICE_AMOUNT_TOTAL_MISMATCH",
                message=f"发票页 {sheet} 明细+费用金额合计 {amount_cell_sum} 与 "
                        f"TOTAL 行 {info.total_amount} 不一致",
                sources=[_source(filename, sheet,
                                 f"{column_letter(columns['total_price'])}{total_row}",
                                 total_row)],
                required_action="请核对行总价、费用行与 TOTAL 金额",
            ))
    if info.total_net is not None:
        ok = decimals_close(net_cell_sum, info.total_net,
                            max_dp=MAX_DP_WEIGHT)
        _check(issues, info, RULE_KIT_INVOICE_NET_TOTAL, None, ok,
               f"明细净重合计{net_cell_sum} vs TOTAL行{info.total_net}")
        info.net_consistent = ok
        if not ok:
            issues.append(Issue(
                code="INVOICE_NET_TOTAL_MISMATCH",
                message=f"发票页 {sheet} 明细净重合计 {net_cell_sum} 与 TOTAL 行 "
                        f"{info.total_net} 不一致",
                sources=[_source(filename, sheet,
                                 f"{column_letter(columns['net_weight'])}{total_row}",
                                 total_row)],
                required_action="请核对单件净重、总数量与总净重",
            ))

    # 辅助金额列：合计行上已知语义列以外的数值列；与金额/净重/数量都不符
    # 才视为未判定口径的辅助金额，绝不采用，必须阻断。
    known_cols = set(columns.values()) | set(supp.values())
    for col in range(1, ws.max_column + 1):
        if col in known_cols:
            continue
        value = _checked_decimal(
            ws.cell(total_row, col).value, issues=issues,
            field_name="auxiliary_amount", record_key=None,
            filename=filename, sheet=sheet, row=total_row, col=col,
            blank_issue=False)
        if value is None or value == 0:
            continue
        matches_amount = info.total_amount is not None \
            and _price_equal(value, info.total_amount)
        matches_net = info.total_net is not None \
            and decimals_close(value, info.total_net, max_dp=MAX_DP_WEIGHT)
        matches_qty = info.total_qty is not None \
            and decimals_close(value, info.total_qty, max_dp=MAX_DP_QTY)
        if matches_amount or matches_net or matches_qty:
            continue
        coordinate = f"{column_letter(col)}{total_row}"
        conflict = {
            "cell": coordinate,
            "value": _amount_text(value),
            "amount": _amount_text(info.total_amount),
        }
        info.aux_conflicts.append(conflict)
        issues.append(Issue(
            code="AUXILIARY_AMOUNT_CONFLICT",
            message=f"发票页 {sheet} 合计行存在与 T'Price 合计 "
                    f"{_amount_text(info.total_amount) or '（缺）'} 不一致的辅助金额列："
                    f"{coordinate}={_amount_text(value)}，未判定该列业务含义，未采用",
            sources=[_source(filename, sheet, coordinate, total_row)],
            required_action="请人工核对辅助金额列含义与行级单价、精度、公式；"
                            "系统不会自动选择某个金额口径",
        ))
        _check(issues, info, RULE_KIT_INVOICE_AUX, None, False,
               f"{coordinate}={value} vs T'Price合计={info.total_amount}")

    info.valid = bool(
        info.structure_ok and info.identity_ok and info.region_ok
        and info.total_row and info.records)
    info.last_row = detail_last
    return info


def _check(issues: list, info, rule: str, record_key, ok: bool,
           detail: str) -> None:
    info.checks.append({
        "rule": rule,
        "record_key": record_key,
        "result": "pass" if ok else "fail",
        "detail": detail,
    })


def _row_issue(issues: list, code: str, part_code: str, field_name: str,
               message: str, filename: str, sheet: str, columns: dict,
               row: int, *, extra_fields: tuple = ()) -> None:
    sources = []
    if field_name in columns:
        sources.append(_source(filename, sheet,
                               f"{column_letter(columns[field_name])}{row}", row))
    for name in extra_fields:
        if name in columns:
            sources.append(_source(filename, sheet,
                                   f"{column_letter(columns[name])}{row}", row))
    issues.append(Issue(
        code=code,
        record_key=part_code,
        field=field_name,
        message=message,
        sources=sources,
        required_action="请核对行级单价/数量/金额/重量后重发",
    ))


def _missing_issue(issues: list, part_code: str, field_name: str,
                   message: str, sources: list) -> None:
    issues.append(Issue(
        code="MISSING_REQUIRED_FIELD",
        record_key=part_code,
        field=field_name,
        message=message,
        sources=sources,
        required_action="请补齐该字段后重发",
    ))


def _price_equal(a: Decimal, b: Decimal) -> bool:
    """金额相等：完全相同，或相对差 < 1e-9（Excel 二进制尾差）。"""
    if not _is_safe_decimal(a) or not _is_safe_decimal(b):
        return False
    if a == b:
        return True
    if a == 0 or b == 0:
        return False
    return abs(a - b) <= max(abs(a), abs(b)) * Decimal("1e-9")


# ---------------------------------------------------------------------------
# 箱单页
# ---------------------------------------------------------------------------


def _parse_packing_sheet(wb_values, wb_formula, filename: str, sheet: str,
                         region: dict, aliases: dict,
                         issues: list) -> _PackingParse:
    info = _PackingParse(filename=filename, sheet=sheet, region=region)
    ws = wb_values[sheet]
    ws_f = wb_formula[sheet]

    fields = ["packing_sets", "packing_packages", "packing_gross_weight"]
    resolution = resolve_columns(ws, fields, aliases, require=fields)
    if resolution.multiple_regions or resolution.missing \
            or resolution.ambiguous or not resolution.header_row:
        issues.append(Issue(
            code="KIT_PACKING_INVALID",
            message=f"箱单 {sheet} 首段表头无法唯一定位："
                    f"缺失={resolution.missing} 多候选={resolution.ambiguous} "
                    f"多表区={resolution.multiple_regions}",
            sources=[_source(filename, sheet)],
            required_action="请确认箱单首段包含唯一的 QTY/Packages/Weight 列",
        ))
        info.valid = info.structure_ok = False
        return info

    info.header_row = resolution.header_row
    info.columns = dict(resolution.columns)
    sets_col = info.columns["packing_sets"]
    pkgs_col = info.columns["packing_packages"]
    gross_col = info.columns["packing_gross_weight"]

    # 先定位后段零件明细表头，用它作为首段 TOTAL 的搜索上界，避免把
    # 后段 TOTAL（净重合计）误当整票毛重合计。
    parts_header, parts_cols_map = _find_header_with_keys(
        ws, ("kitno", "totalqty", "netweight"),
        start_row=resolution.header_row + 1,
        end_row=min(ws.max_row,
                    resolution.header_row + SCAN_ROW_LIMIT - 1))
    parts_header_row = parts_header if parts_header else None
    info.parts_header_row = parts_header_row
    info.parts_columns = {
        "kit_no": parts_cols_map.get("kitno"),
        "total_qty": parts_cols_map.get("totalqty"),
        "net_weight": parts_cols_map.get("netweight"),
        "part_code": parts_cols_map.get("descriptionofgoods"),
        "qty_per_set": parts_cols_map.get("qtyset"),
        "sets": parts_cols_map.get("setqty"),
        "hs_code": parts_cols_map.get("chnhscode"),
        "coo": parts_cols_map.get("coo"),
    } if parts_cols_map else {}

    total_row = _first_total_row(
        ws, resolution.header_row + 1,
        end_row=(parts_header_row - 1) if parts_header_row else None)
    if total_row is None:
        issues.append(Issue(
            code="PACKING_TOTAL_NOT_FOUND",
            message=f"箱单 {sheet} 首段未找到套件合计 TOTAL 行",
            sources=[_source(filename, sheet)],
            required_action="请确认箱单首段存在唯一 TOTAL 行",
        ))
        info.structure_ok = False
    info.total_row = total_row

    region_ok = _check_region_contract(
        region, filename=filename, sheet=sheet,
        header_row=info.header_row, total_row=total_row,
        columns={"sets": sets_col, "packages": pkgs_col,
                 "gross_weight": gross_col}, issues=issues)
    info.region_ok = region_ok

    if total_row is None:
        info.valid = False
        return info

    def src(col: int, row: int) -> Source:
        return _source(filename, sheet, f"{column_letter(col)}{row}", row)

    sets_sum = Decimal("0")
    pkgs_sum = Decimal("0")
    gross_sum = Decimal("0")
    gross_without_tare = Decimal("0")
    tare = None
    tare_row = None

    for row in range(resolution.header_row + 1, total_row):
        sets_value = _read_number(
            ws, ws_f, row, sets_col, filename=filename, sheet=sheet,
            field_name="packing_sets", record_key=None, issues=issues,
            blank_issue=False)
        pkgs_value = _read_number(
            ws, ws_f, row, pkgs_col, filename=filename, sheet=sheet,
            field_name="packing_packages", record_key=None, issues=issues,
            blank_issue=False)
        gross_value = _read_number(
            ws, ws_f, row, gross_col, filename=filename, sheet=sheet,
            field_name="packing_gross_weight", record_key=None, issues=issues,
            blank_issue=False)
        if sets_value is not None:
            sets_sum += sets_value
        if pkgs_value is not None:
            pkgs_sum += pkgs_value
        if gross_value is not None:
            gross_sum += gross_value
            if normalize_header(ws.cell(row, 1).value) != "tareweight":
                gross_without_tare += gross_value
        if normalize_header(ws.cell(row, 1).value) == "tareweight":
            tare = gross_value
            tare_row = row

    info.sets_sum = sets_sum
    info.packages_sum = pkgs_sum
    info.gross_sum = gross_sum
    info.gross_sum_without_tare = gross_without_tare
    info.tare_weight = tare
    info.tare_row = tare_row

    info.sets = _read_number(
        ws, ws_f, total_row, sets_col, filename=filename, sheet=sheet,
        field_name="packing_sets", record_key=None, issues=issues)
    info.packages = _read_number(
        ws, ws_f, total_row, pkgs_col, filename=filename, sheet=sheet,
        field_name="packing_packages", record_key=None, issues=issues)
    info.gross_weight = _read_number(
        ws, ws_f, total_row, gross_col, filename=filename, sheet=sheet,
        field_name="packing_gross_weight", record_key=None, issues=issues)

    checks = []
    if info.sets is not None:
        ok = decimals_close(sets_sum, info.sets, max_dp=MAX_DP_QTY)
        checks.append(("套数", sets_sum, info.sets, ok, sets_col))
    if info.packages is not None:
        ok = decimals_close(pkgs_sum, info.packages, max_dp=MAX_DP_QTY)
        checks.append(("件数", pkgs_sum, info.packages, ok, pkgs_col))
    if info.gross_weight is not None:
        ok = decimals_close(gross_sum, info.gross_weight, max_dp=MAX_DP_WEIGHT)
        checks.append(("毛重", gross_sum, info.gross_weight, ok, gross_col))
        if not ok and tare is not None and info.gross_weight is not None \
                and decimals_close(gross_without_tare, info.gross_weight,
                                   max_dp=MAX_DP_WEIGHT):
            info.tare_included = False
            issues.append(Issue(
                code="TARE_NOT_INCLUDED",
                message=f"箱单 {sheet} 的 TOTAL 毛重 {info.gross_weight} 不包含 "
                        f"TARE WEIGHT {tare}，毛重合计被低估；系统不会自行相加",
                sources=[src(gross_col, total_row)] if tare_row is None else
                        [src(gross_col, total_row), src(gross_col, tare_row)],
                required_action="请核对箱单毛重合计是否包含皮重",
            ))
    for label, summed, total, ok, col in checks:
        _check(issues, info, RULE_KIT_PACKING_TOTALS, None, ok,
               f"首段{label}明细合计{summed} vs TOTAL行{total}")
        if not ok:
            issues.append(Issue(
                code="PACKING_TOTAL_MISMATCH",
                message=f"箱单 {sheet} 首段{label}明细合计 {summed} 与 TOTAL 行 "
                        f"{total} 不一致",
                sources=[src(col, total_row)],
                required_action=f"请核对箱单{label}明细与合计",
            ))

    if tare is not None:
        included = (info.gross_weight is not None
                    and decimals_close(gross_sum, info.gross_weight,
                                       max_dp=MAX_DP_WEIGHT))
        info.tare_included = included
        _check(issues, info, RULE_KIT_PACKING_TARE, None, included,
               f"皮重 {tare} kg 已包含在毛重合计内，不得再加")
    elif info.gross_weight is not None:
        info.tare_included = None

    # 后段零件明细：独立 TOTAL（总数量/净重），只做交叉核对
    if parts_header_row:
        parts_total = _first_total_row(ws, parts_header_row + 1)
        info.parts_total_row = parts_total
        total_qty_col = info.parts_columns.get("total_qty")
        net_col = info.parts_columns.get("net_weight")
        if parts_total is None or not total_qty_col or not net_col:
            issues.append(Issue(
                code="PACKING_PARTS_SECTION_INVALID",
                message=f"箱单 {sheet} 后段零件明细缺少唯一 TOTAL 行或 "
                        "TOTAL QTY/NET WEIGHT 列",
                sources=[_source(filename, sheet)],
                required_action="请确认零件明细表头与合计行完整",
            ))
        else:
            qty_sum = Decimal("0")
            net_sum = Decimal("0")
            parts_rows = 0
            for row in range(parts_header_row + 1, parts_total):
                qty_value = _read_number(
                    ws, ws_f, row, total_qty_col, filename=filename,
                    sheet=sheet, field_name="parts_total_qty",
                    record_key=None, issues=issues, blank_issue=False)
                net_value = _read_number(
                    ws, ws_f, row, net_col, filename=filename,
                    sheet=sheet, field_name="parts_net_weight",
                    record_key=None, issues=issues, blank_issue=False)
                if qty_value is not None:
                    qty_sum += qty_value
                if net_value is not None:
                    net_sum += net_value
                if qty_value is not None or net_value is not None:
                    parts_rows += 1
            info.parts_rows = parts_rows
            info.parts_qty_sum = qty_sum
            info.parts_net_sum = net_sum
            info.parts_total_qty = _read_number(
                ws, ws_f, parts_total, total_qty_col, filename=filename,
                sheet=sheet, field_name="parts_total_qty",
                record_key=None, issues=issues)
            info.parts_net_weight = _read_number(
                ws, ws_f, parts_total, net_col, filename=filename, sheet=sheet,
                field_name="parts_net_weight", record_key=None, issues=issues)
            for label, summed, total, col in (
                    ("零件总数量", qty_sum, info.parts_total_qty, total_qty_col),
                    ("零件净重", net_sum, info.parts_net_weight, net_col)):
                if total is None:
                    continue
                ok = decimals_close(summed, total, max_dp=(
                    MAX_DP_QTY if "数量" in label else MAX_DP_WEIGHT))
                _check(issues, info, RULE_KIT_PACKING_PARTS_TOTAL, None, ok,
                       f"{label}明细合计{summed} vs 后段TOTAL行{total}")
                if not ok:
                    issues.append(Issue(
                        code="PACKING_PARTS_TOTAL_MISMATCH",
                        message=f"箱单 {sheet} 后段 {label} 明细合计 {summed} 与 "
                                f"TOTAL 行 {total} 不一致",
                        sources=[src(col, parts_total)],
                        required_action="请核对零件明细与后段 TOTAL",
                    ))
    else:
        issues.append(Issue(
            code="PACKING_PARTS_SECTION_MISSING",
            message=f"箱单 {sheet} 未定位到后段零件净重区（Kit No/TOTAL QTY/"
                    "NET WEIGHT），无法与发票净重交叉核对",
            sources=[_source(filename, sheet)],
            required_action="请确认箱单包含每套零件明细及 TOTAL 行",
        ))

    info.valid = bool(info.structure_ok and info.region_ok
                      and total_row is not None and info.sets is not None
                      and info.packages is not None
                      and info.gross_weight is not None)
    return info


# ---------------------------------------------------------------------------
# 身份应用表 / BL
# ---------------------------------------------------------------------------


def _parse_application_identity(wb_values, filename: str, sheet: str,
                                region: dict, aliases: dict,
                                issues: list) -> dict:
    """读取 KIT/PO/Order Code/MSO/序列号 应用表，返回身份集合。

    与预览一致：有效行至少需两项身份字段；数据段开始后遇到仅单项字段的
    行视为模板残留并终止扫描，绝不把残留 Order Code 当成本票身份。
    """
    ws = wb_values[sheet]
    keys = ("ordercode", "mso", "machineserialno")
    found = _find_header_with_keys(ws, keys, start_row=1)
    result = {"order_code": set(), "mso": set(), "serial": set(),
              "sheet": sheet, "filename": filename, "header_row": None}
    if found is None:
        return result
    header_row, cols = found
    result["header_row"] = header_row
    order_col = cols.get("ordercode")
    mso_col = cols.get("mso")
    serial_col = cols.get("machineserialno")
    started = False
    limit = min(ws.max_row, header_row + MAX_HEADER_SCAN_ROWS * 2)
    for row in range(header_row + 1, limit + 1):
        values = [ws.cell(row, col).value if col else None
                  for col in (order_col, mso_col, serial_col)]
        filled = [value for value in values if _clean_text(value)]
        if not filled:
            if started:
                break
            continue
        if len(filled) < 2:
            if started:
                break
            continue
        started = True
        if order_col and _clean_text(values[0]):
            result["order_code"].add(_clean_text(values[0]))
        if mso_col and _clean_text(values[1]):
            result["mso"].add(_clean_text(values[1]))
        if serial_col and _clean_text(values[2]):
            result["serial"].add(_clean_text(values[2]))
    return result


def _scan_bl_sheet(wb_values, filename: str, sheet: str,
                   current_invoice_no: Optional[str],
                   issues: list) -> dict:
    """BL 页只核对残留 INVOICE NO.，绝不作为当前发票号来源。"""
    ws = wb_values[sheet]
    checks: list = []
    seen: dict[str, Source] = {}
    for row in range(1, min(ws.max_row, MAX_NOTE_SCAN_ROWS) + 1):
        for col in range(1, min(ws.max_column, 16) + 1):
            value = ws.cell(row, col).value
            if not isinstance(value, str) or "INVOICE" not in value.upper():
                continue
            match = INVOICE_NO_RE.search(value.replace("\n", " "))
            if match:
                number = match.group(1).strip()
                seen.setdefault(number, _source(
                    filename, sheet, ws.cell(row, col).coordinate, row))
    stale = []
    if current_invoice_no:
        for number, source in seen.items():
            if number == current_invoice_no:
                _check_bl(checks, True,
                          f"BL 发票号 {number} 与当前发票一致")
                continue
            stale.append(number)
            issues.append(Issue(
                code="STALE_BL_INVOICE",
                message=f"BL 页 {sheet} 出现与本次发票 {current_invoice_no} 不同的票号 "
                        f"{number}（疑似旧模板残留），未作为当前发票号来源",
                sources=[source],
                required_action="请人工确认 BL 页票号为旧模板残留；系统不据此改票号",
            ))
            _check_bl(checks, False,
                      f"BL 发票号 {number} 与当前发票 {current_invoice_no} 不一致")
    return {"filename": filename, "sheet": sheet,
            "invoice_nos": sorted(seen), "stale": sorted(stale),
            "checks": checks}


def _check_bl(checks: list, ok: bool, detail: str) -> None:
    checks.append({"rule": RULE_KIT_BL_INVOICE,
                   "record_key": None,
                   "result": "pass" if ok else "fail",
                   "detail": detail})


def _discover_bl_sheets(wb_values, aliases: dict) -> list:
    names = []
    for name in wb_values.sheetnames:
        ws = wb_values[name]
        normalized = normalize_header(name)
        if normalized in ("bl", "billoflading"):
            names.append(name)
            continue
        for row in range(1, min(ws.max_row, 12) + 1):
            hit = False
            for col in range(1, min(ws.max_column, 8) + 1):
                value = ws.cell(row, col).value
                if isinstance(value, str) and "BILL" in value.upper() \
                        and "LADING" in value.upper():
                    names.append(name)
                    hit = True
                    break
            if hit:
                break
    return names


# ---------------------------------------------------------------------------
# 两页发票逐行比较
# ---------------------------------------------------------------------------


def _record_sort_key(record: dict) -> tuple:
    return (
        record.get("part_code") or "",
        _amount_text(record.get("total_price")) or "",
        _weight_text(record.get("net_weight")) or "",
        _qty_text(record.get("qty")) or "",
        record.get("coo") or "",
        record.get("hs_code") or "",
        record.get("spec") or "",
    )


def _values_agree(record_a: dict, record_b: dict, field_name: str) -> bool:
    a = record_a.get(field_name)
    b = record_b.get(field_name)
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        if field_name in ("unit_price", "total_price"):
            return _price_equal(a, b)
        if field_name in ("qty", "qty_per_set", "sets"):
            return decimals_close(a, b, max_dp=MAX_DP_QTY)
        return decimals_close(a, b, max_dp=MAX_DP_WEIGHT)
    return _clean_text(a) == _clean_text(b)


def _compare_invoice_parses(left: _InvoiceParse,
                            right: _InvoiceParse) -> tuple:
    """返回 (agree, details)。逐行逐业务字段比较，不做任何口径选择。"""
    details = []
    if _clean_text(left.invoice_no) != _clean_text(right.invoice_no):
        details.append({"field": "invoice_no",
                        "values": {left.sheet: left.invoice_no,
                                   right.sheet: right.invoice_no}})
    for name, a, b in (
            ("total_amount", left.total_amount, right.total_amount),
            ("total_net", left.total_net, right.total_net)):
        if a is None or b is None or not (
                _price_equal(a, b) if name == "total_amount"
                else decimals_close(a, b, max_dp=MAX_DP_WEIGHT)):
            details.append({"field": name,
                            "values": {left.sheet: _amount_text(a),
                                       right.sheet: _amount_text(b)}})
    left_records = sorted(left.business_records(), key=_record_sort_key)
    right_records = sorted(right.business_records(), key=_record_sort_key)
    if len(left_records) != len(right_records):
        details.append({"field": "record_count",
                        "values": {left.sheet: len(left_records),
                                   right.sheet: len(right_records)}})
    compare_fields = ("part_code", "kit_no", "qty_per_set", "sets", "qty",
                      "unit_price", "total_price", "unit_weight",
                      "net_weight", "coo", "hs_code", "product_name",
                      "spec", "description")
    for index, (record_a, record_b) in enumerate(
            zip(left_records, right_records)):
        if record_a.get("part_code") != record_b.get("part_code"):
            details.append({
                "field": "part_code",
                "values": {left.sheet: record_a.get("part_code"),
                           right.sheet: record_b.get("part_code")},
            })
            continue
        for field_name in compare_fields:
            if not _values_agree(record_a, record_b, field_name):
                details.append({
                    "field": field_name,
                    "part_code": record_a.get("part_code"),
                    "values": {
                        left.sheet: _display_business_value(
                            record_a.get(field_name)),
                        right.sheet: _display_business_value(
                            record_b.get(field_name)),
                    },
                })
    left_fees = sorted((fee.get("amount") or "") for fee in left.fee_rows)
    right_fees = sorted((fee.get("amount") or "") for fee in right.fee_rows)
    if left_fees != right_fees:
        details.append({"field": "fee_rows",
                        "values": {left.sheet: left_fees,
                                   right.sheet: right_fees}})
    return (not details), details[:20]


def _display_business_value(value):
    if isinstance(value, Decimal):
        return _amount_text(value)
    return value


# ---------------------------------------------------------------------------
# 候选事实核对（只核对，不填充）
# ---------------------------------------------------------------------------


def _fact_agrees(name: str, candidate_value: Decimal,
                 parsed_value: Decimal) -> bool:
    if not _is_safe_decimal(candidate_value) \
            or not _is_safe_decimal(parsed_value):
        return False
    if name == "amount":
        return _price_equal(candidate_value, parsed_value)
    if name in ("qty", "packages", "sets"):
        return decimals_close(candidate_value, parsed_value, max_dp=MAX_DP_QTY)
    return decimals_close(candidate_value, parsed_value, max_dp=MAX_DP_WEIGHT)


def _check_candidate_facts(candidate: dict, parsed: dict,
                           layout: dict, issues: list) -> None:
    facts = candidate.get("facts") if isinstance(candidate, dict) else None
    if not isinstance(facts, dict):
        return
    checks = layout.setdefault("checks", [])
    for name in ("amount", "qty", "net_weight", "gross_weight",
                 "packages", "sets", "tare_weight"):
        fact = facts.get(name)
        if not isinstance(fact, dict):
            continue
        if fact.get("conflict"):
            variants = fact.get("variants")
            first_variant = (variants[0] if isinstance(variants, list)
                             and variants and isinstance(variants[0], dict)
                             else {})
            issues.append(Issue(
                code="CANDIDATE_FACT_CONFLICT",
                message=f"已确认候选自身的 {name} 事实存在多个不一致取值，"
                        "不能作为本票核对基准",
                sources=[source for source in
                         (_source_from_dict(item)
                          for item in first_variant.get("sources", []))
                         if source is not None],
                required_action="请重新确认本票事实版本",
            ))
            continue
        fact_sources = [source for source in
                        (_source_from_dict(item)
                         for item in fact.get("sources") or [])
                        if source is not None]
        candidate_value = _checked_decimal(
            fact.get("value"), issues=issues, field_name=name,
            record_key=None,
            source=fact_sources[0] if fact_sources else None,
            blank_issue=False)
        parsed_value = parsed.get(name)
        if candidate_value is None or parsed_value is None:
            continue
        ok = _fact_agrees(name, candidate_value, parsed_value)
        checks.append({
            "rule": RULE_KIT_FACT,
            "record_key": None,
            "result": "pass" if ok else "fail",
            "detail": f"候选确认 {name}={candidate_value} vs 正式读取 {parsed_value}",
        })
        if not ok:
            issues.append(Issue(
                code="CONFIRMED_FACT_DRIFT",
                message=f"已确认候选 {name}={candidate_value} 与正式读取工作簿的 "
                        f"{parsed_value} 不一致；未用预览值覆盖明细",
                sources=fact_sources,
                required_action="请重新确认本票（附件或内容已变化）",
            ))


def _validate_mail_row_numbers(mail_rows: list, issues: list) -> None:
    """正文行数值安全校验（只校验，不取数）。

    Health Care Kit 明细只从工作簿读取；但 confirmed_input.mail_rows 也是
    本票受确认的输入，若夹带 NaN/Infinity/极端指数，必须在进入任何后续
    比较前如实阻断，而不是等下游量化/容差处理时才抛技术异常。
    """
    for index, row in enumerate(mail_rows):
        if not isinstance(row, dict):
            continue
        fields = row.get("fields")
        if not isinstance(fields, dict):
            continue
        for name, value in fields.items():
            if name not in _MAIL_NUMERIC_FIELDS:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if isinstance(value, str) and not _MAIL_NUMERIC_RE.fullmatch(
                    value.strip()):
                # 非数值文本（如 "1 SET"/"N/A"）不属于受控数值字段，忽略；
                # 本模块从不把它当作配件明细。
                continue
            source = Source(
                type="mail_html",
                message_id=row.get("message_id"),
                table_index=row.get("table_index"),
                row=row.get("row"),
                detail=f"正文行 fields.{name}（第 {index + 1} 行）",
            )
            _checked_decimal(value, issues=issues, field_name=name,
                             record_key=None, source=source,
                             blank_issue=False)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def parse_confirmed_kit_sources(sources: list[dict], candidate: dict,
                                mail_rows: list[dict],
                                aliases=None) -> PartsParseResult:
    """按可信快照的已确认来源解析一票 Health Care Kit 配件。

    参数
    ----
    sources:
        本票资产列表，元素至少含 ``path``/``filename``，建议含 ``sha256``。
        只接受与 ``candidate.source_refs`` 对齐的资产；额外资产拒绝。
    candidate:
        已确认候选（candidate_id/identity/source_refs/regions/facts）。
        ``source_refs[].regions`` 是唯一区域依据；不使用 preview 的总数当明细。
    mail_rows:
        本票正文行（快照深拷贝）。配件明细只从工作簿读取，本函数不据此
        填充任何行；仅校验类型并记录数量。
    aliases:
        调用方追加的表头别名（与现有 merged_aliases 语义一致，只追加）。
    """
    result = PartsParseResult()
    issues: list = result.issues
    layout = result.layout
    layout.setdefault("checks", [])
    layout["schema_version"] = 1
    layout["mail_rows_count"] = len(mail_rows) if isinstance(mail_rows, list) else 0
    if not isinstance(candidate, dict):
        issues.append(Issue(
            code="CANDIDATE_INVALID",
            message="已确认候选不是对象，不能解析",
            required_action="请重新确认本票并生成可信快照",
        ))
        return result
    if not isinstance(mail_rows, list):
        issues.append(Issue(
            code="MAIL_ROWS_INVALID",
            message="mail_rows 不是列表，已拒绝；配件明细仍只从工作簿读取",
            required_action="请重新确认本票并生成可信快照",
        ))
    else:
        _validate_mail_row_numbers(mail_rows, issues)

    alias_map = _kit_aliases(aliases)
    result.aliases = alias_map

    identity = candidate.get("identity") if isinstance(
        candidate.get("identity"), dict) else {}
    expected_invoice_nos = {
        _clean_text(value) for value in _identity_values(
            identity.get("invoice_no")) if _clean_text(value)}
    if len(expected_invoice_nos) > 1:
        issues.append(Issue(
            code="CANDIDATE_IDENTITY_CONFLICT",
            message=f"已确认候选自身存在多个发票号 {sorted(expected_invoice_nos)}，"
                    "不能自动选择",
            required_action="请重新确认本票唯一发票号",
        ))

    refs = _normalise_refs(candidate, issues)
    if not refs:
        return result
    matched = _normalise_sources(sources, refs, issues)
    if not matched:
        return result

    loaded = []
    for item in matched:
        path = Path(item["path"])
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            issues.append(Issue(
                code="UNSUPPORTED_FILE",
                message=f"附件 {item['filename']} 不是 xlsx/xls，无法解析",
                sources=[_source(item["filename"])],
                required_action="请提供未加密的 xlsx 或 xls 附件",
            ))
            continue
        try:
            wb_values, wb_formula = load_workbook_pair(str(path))
        except WorkbookDependencyError as exc:
            issues.append(Issue(
                code="WORKBOOK_DEPENDENCY",
                message=f"附件 {item['filename']} 已收到，但运行环境缺少解析组件：{exc}",
                sources=[_source(item["filename"])],
                required_action="附件无需重发；请联系维护人员安装缺失组件",
            ))
            continue
        except UnsupportedSourceError as exc:
            issues.append(Issue(
                code="UNSUPPORTED_FILE",
                message=str(exc),
                sources=[_source(item["filename"])],
                required_action="请提供未加密、未损坏的 xlsx 或 xls 附件",
            ))
            continue
        except Exception as exc:  # 未知读取错误：保留有限信息，不伪装成功
            issues.append(Issue(
                code="UNSUPPORTED_FILE",
                message=f"附件 {item['filename']} 读取失败：{type(exc).__name__}",
                sources=[_source(item["filename"])],
                required_action="请提供未加密、未损坏的 xlsx 或 xls 附件",
            ))
            continue
        regions = item["ref"].get("regions") or []
        source_regions = item["source"].get("regions")
        if not regions and isinstance(source_regions, list):
            # 快照资产也保存了 regions；仅当 candidate 未给出时兜底使用
            regions = source_regions
        item["regions"] = []
        for index, region in enumerate(regions or []):
            if not isinstance(region, dict):
                issues.append(Issue(
                    code="CONFIRMED_REGION_INVALID",
                    message=f"资产 {item['filename']} 的 regions[{index}] 不是对象",
                    sources=[_source(item["filename"])],
                    required_action="请重新确认本票并生成可信快照",
                ))
                continue
            role = str(region.get("role") or "").strip()
            # sheet 名允许有尾随空格（真实模板 "Invoice " 即如此），不能 strip
            sheet = str(region.get("sheet") or "")
            if not role or not sheet:
                issues.append(Issue(
                    code="CONFIRMED_REGION_INVALID",
                    message=f"资产 {item['filename']} 的 regions[{index}] 缺少 "
                            "role/sheet",
                    sources=[_source(item["filename"])],
                    required_action="请重新确认本票并生成可信快照",
                ))
                continue
            item["regions"].append({"role": role, "sheet": sheet, "raw": region})
        if not item["regions"]:
            if _sheet_has_kit_structure(wb_values, alias_map):
                issues.append(Issue(
                    code="CONFIRMED_REGION_MISSING",
                    message=f"资产 {item['filename']} 含 Health Care Kit 结构，但已确认"
                            "候选未给出任何区域；不能自行选择 sheet",
                    sources=[_source(item["filename"])],
                    required_action="请重新确认本票区域后重试",
                ))
        loaded.append({**item, "wb_values": wb_values,
                       "wb_formula": wb_formula})

    invoice_infos: list = []
    packing_infos: list = []
    application_regions: list = []
    bl_sheets: list = []
    processed_roles: set = set()

    for item in loaded:
        wb_values = item["wb_values"]
        wb_formula = item["wb_formula"]
        for entry in item["regions"]:
            role = entry["role"]
            sheet = entry["sheet"]
            if role not in (ROLE_INVOICE, ROLE_PACKING, ROLE_APPLICATION,
                            ROLE_BL):
                continue
            if sheet not in wb_values.sheetnames:
                issues.append(Issue(
                    code="CONFIRMED_REGION_MISSING",
                    message=f"已确认区域 {sheet} 在资产 {item['filename']} 中不存在，"
                            "不能按其他 sheet 替代",
                    sources=[_source(item["filename"])],
                    required_action="请重新确认本票区域后重试",
                ))
                continue
            key = (item["filename"], sheet, role)
            if key in processed_roles:
                continue
            processed_roles.add(key)
            if role == ROLE_INVOICE:
                invoice_infos.append(_parse_invoice_sheet(
                    wb_values, wb_formula, item["filename"], sheet,
                    entry["raw"], alias_map, issues, expected_invoice_nos))
            elif role == ROLE_PACKING:
                packing_infos.append(_parse_packing_sheet(
                    wb_values, wb_formula, item["filename"], sheet,
                    entry["raw"], alias_map, issues))
            elif role == ROLE_APPLICATION:
                application_regions.append(
                    (wb_values, item["filename"], sheet, entry["raw"]))
            elif role == ROLE_BL:
                bl_sheets.append((wb_values, item["filename"], sheet))

    # 已确认 BL 区域之外，仍做结构识别：BL 可能被人工区域漏掉，旧票号
    # 必须可见，但绝不把 BL 当发票号来源。
    confirmed_bl_keys = {(filename, sheet)
                         for _wb, filename, sheet in bl_sheets}
    for item in loaded:
        for sheet in _discover_bl_sheets(item["wb_values"], alias_map):
            key = (item["filename"], sheet)
            if key in confirmed_bl_keys:
                continue
            bl_sheets.append((item["wb_values"], item["filename"], sheet))
            confirmed_bl_keys.add(key)

    # ---- 发票页：唯一/一致才采用；不一致报告冲突，不选口径 ----
    common_invoice_no = None
    chosen_invoice: Optional[_InvoiceParse] = None
    invoice_conflict = False
    if not invoice_infos:
        issues.append(Issue(
            code="CONFIRMED_REGION_MISSING",
            message="已确认本票区域中没有可用的 Health Care Kit 发票页",
            required_action="请重新确认本票发票区域后重试",
        ))
    else:
        invoice_nos = {info.invoice_no for info in invoice_infos
                       if info.invoice_no}
        if len(invoice_nos) == 1:
            common_invoice_no = next(iter(invoice_nos))
        valid_infos = [info for info in invoice_infos if info.valid]
        if len(valid_infos) != len(invoice_infos):
            invoice_conflict = True
            # 单页结构问题已由该页问题覆盖；多页时明确不因某页不可用而
            # 采用另一页（无“可用者胜出”规则）。
            if len(invoice_infos) > 1:
                issues.append(Issue(
                    code="INVOICE_SHEET_CONFLICT",
                    message="已确认的多张发票页中存在结构/身份校验未通过的页面，"
                            "不能选择另一页自动定稿",
                    sources=[_source(info.filename, info.sheet)
                             for info in invoice_infos],
                    required_action="请人工确认应采用的发票页与价格口径",
                ))
        elif len(invoice_infos) > 1:
            base = invoice_infos[0]
            details = []
            agree = True
            for other in invoice_infos[1:]:
                ok, diff = _compare_invoice_parses(base, other)
                if not ok:
                    agree = False
                    details.extend(diff)
            layout["kit_invoice_sheet_diff"] = details
            if agree:
                chosen_invoice = sorted(
                    invoice_infos, key=lambda info: (info.filename, info.sheet))[0]
                _check(issues, chosen_invoice, RULE_KIT_INVOICE_SHEETS_AGREE,
                       None, True,
                       f"{len(invoice_infos)} 张已确认发票页逐行业务内容一致")
            else:
                invoice_conflict = True
                issues.append(Issue(
                    code="INVOICE_SHEET_CONFLICT",
                    message="已确认的多张发票页业务内容不一致："
                            + "；".join(
                                f"{left} vs {right}"
                                for left, right in _sheet_pairs(invoice_infos))
                            + f"；差异 {len(details)} 项（"
                            + "、".join(sorted({str(d.get('field')) for d in details}))
                            + "），未自动选择任何价格口径",
                    sources=[_source(info.filename, info.sheet)
                             for info in invoice_infos],
                    required_action="请人工确认应采用的发票页、行总价与单价口径",
                ))
                _check(issues, base, RULE_KIT_INVOICE_SHEETS_AGREE,
                       None, False, f"差异 {len(details)} 项")
        else:
            chosen_invoice = valid_infos[0] if valid_infos else None

        # 两页一致/单页时采用记录；冲突时不产出任何记录
        if chosen_invoice is not None and not invoice_conflict:
            result.sheet_name = chosen_invoice.sheet
            result.records = list(chosen_invoice.records)
            result.invoice_source = chosen_invoice.invoice_source
            layout["parts_invoice"] = {
                "sheet": chosen_invoice.sheet,
                "header_row": chosen_invoice.header_row,
                "subheader_row": chosen_invoice.subheader_row,
                "columns": {name: column_letter(col) for name, col in
                            sorted(chosen_invoice.columns.items())},
                "supplementary": {name: column_letter(col) for name, col in
                                  sorted(chosen_invoice.supplementary.items())},
                "data_first_row": (chosen_invoice.subheader_row
                                   or chosen_invoice.header_row) + 1,
                "data_last_row": chosen_invoice.total_row - 1
                if chosen_invoice.total_row else None,
            }
        # 发票号只要各页一致且身份校验通过即可作为身份；即使行级价格
        # 冲突也不选口径。身份不符（错票）时不回填任何发票号。
        if common_invoice_no and all(info.identity_ok
                                     for info in invoice_infos):
            result.invoice_no = common_invoice_no

    common_amount = _common_decimal(
        [info.total_amount for info in invoice_infos],
        max_dp=MAX_DP_PRICE, amount=True)
    common_net = _common_decimal(
        [info.total_net for info in invoice_infos], max_dp=MAX_DP_WEIGHT)
    common_qty = _common_decimal(
        [info.total_qty for info in invoice_infos], max_dp=MAX_DP_QTY)
    layout["kit_invoice"] = {
        "sheets": [info.summary() for info in invoice_infos],
        "selected_sheet": chosen_invoice.sheet if chosen_invoice else None,
        "selected_filename": chosen_invoice.filename if chosen_invoice else None,
        "conflict": invoice_conflict,
        "invoice_no": common_invoice_no,
        # 多页冲突时仍展示各页一致的合计，仅用于审计；记录为空，绝不选口径
        "total_amount": _amount_text(common_amount),
        "total_net": _weight_text(common_net),
        "total_qty": _qty_text(common_qty),
    }
    layout.setdefault("parts_invoice", {})

    # 发票记录核对：单页/一致多页时逐行 pass/fail 已写入各自 info.checks
    for info in invoice_infos:
        layout["checks"].extend(info.checks)

    # ---- 箱单页：唯一/一致才采用；整票毛重/件数/套数 ----
    chosen_packing: Optional[_PackingParse] = None
    packing_conflict = False
    if not packing_infos:
        issues.append(Issue(
            code="CONFIRMED_REGION_MISSING",
            message="已确认本票区域中没有可用的 Health Knot 箱单页（Packing list）",
            required_action="请重新确认本票箱单区域后重试",
        ))
    else:
        valid_packings = [info for info in packing_infos if info.valid]
        if len(valid_packings) != len(packing_infos):
            packing_conflict = True
            if len(packing_infos) > 1:
                issues.append(Issue(
                    code="PACKING_SHEET_CONFLICT",
                    message="已确认的多张箱单页中存在结构校验未通过的页面，"
                            "不能选择另一页自动定稿",
                    sources=[_source(info.filename, info.sheet)
                             for info in packing_infos],
                    required_action="请人工确认应采用的箱单页",
                ))
        elif len(packing_infos) > 1:
            base = packing_infos[0]
            agree = True
            for other in packing_infos[1:]:
                if not _packing_totals_agree(base, other):
                    agree = False
            if agree:
                chosen_packing = sorted(
                    packing_infos,
                    key=lambda info: (info.filename, info.sheet))[0]
            else:
                packing_conflict = True
                issues.append(Issue(
                    code="PACKING_SHEET_CONFLICT",
                    message="已确认的多张箱单页合计（套数/件数/毛重/零件净重）"
                            "不一致，未自动选择",
                    sources=[_source(info.filename, info.sheet)
                             for info in packing_infos],
                    required_action="请人工确认应采用的箱单页",
                ))
        else:
            chosen_packing = valid_packings[0] if valid_packings else None

        if chosen_packing is not None and not packing_conflict:
            result.gross_weight = chosen_packing.gross_weight
            result.packages = chosen_packing.packages
            result.gross_source = _source(
                chosen_packing.filename, chosen_packing.sheet,
                f"{column_letter(chosen_packing.columns['packing_gross_weight'])}"
                f"{chosen_packing.total_row}", chosen_packing.total_row)
            result.packages_source = _source(
                chosen_packing.filename, chosen_packing.sheet,
                f"{column_letter(chosen_packing.columns['packing_packages'])}"
                f"{chosen_packing.total_row}", chosen_packing.total_row)
            layout["packing_total"] = {
                "sheet": chosen_packing.sheet,
                "header_row": chosen_packing.header_row,
                "total_row": chosen_packing.total_row,
                "columns": {
                    "sets": column_letter(
                        chosen_packing.columns["packing_sets"]),
                    "packages": column_letter(
                        chosen_packing.columns["packing_packages"]),
                    "gross_weight": column_letter(
                        chosen_packing.columns["packing_gross_weight"]),
                },
            }
    layout.setdefault("packing_total", {})
    layout["kit_packing"] = {
        "sheets": [info.summary() for info in packing_infos],
        "selected_sheet": chosen_packing.sheet if chosen_packing else None,
        "conflict": packing_conflict,
        "sets": _qty_text(chosen_packing.sets) if chosen_packing else None,
        "packages": _qty_text(chosen_packing.packages) if chosen_packing else None,
        "gross_weight": _weight_text(chosen_packing.gross_weight)
        if chosen_packing else None,
        "tare_weight": _weight_text(chosen_packing.tare_weight)
        if chosen_packing else None,
        "tare_included": chosen_packing.tare_included
        if chosen_packing else None,
        "parts_total_row": chosen_packing.parts_total_row
        if chosen_packing else None,
        "parts_total_qty": _qty_text(chosen_packing.parts_total_qty)
        if chosen_packing else None,
        "parts_net_weight": _weight_text(chosen_packing.parts_net_weight)
        if chosen_packing else None,
        "parts_rows": chosen_packing.parts_rows if chosen_packing else None,
        "parts_qty_sum": _qty_text(chosen_packing.parts_qty_sum)
        if chosen_packing else None,
        "parts_net_sum": _weight_text(chosen_packing.parts_net_sum)
        if chosen_packing else None,
    }
    for info in packing_infos:
        layout["checks"].extend(info.checks)

    # ---- 发票与箱单后段交叉核对：净重/总数量 ----
    invoice_net_values = []
    invoice_qty_values = []
    for info in invoice_infos:
        if info.total_net is not None:
            invoice_net_values.append(info.total_net)
        if info.total_qty is not None:
            invoice_qty_values.append(info.total_qty)
    if chosen_packing and chosen_packing.parts_net_weight is not None \
            and invoice_net_values:
        # 多页冲突时只有全部页面净重一致才可交叉核对；否则冲突已覆盖
        if all(decimals_close(value, invoice_net_values[0],
                              max_dp=MAX_DP_WEIGHT)
               for value in invoice_net_values):
            ok = decimals_close(chosen_packing.parts_net_weight,
                                invoice_net_values[0], max_dp=MAX_DP_WEIGHT)
            layout["checks"].append({
                "rule": RULE_KIT_NET_CROSSCHECK,
                "record_key": None,
                "result": "pass" if ok else "fail",
                "detail": f"箱单后段零件净重合计{chosen_packing.parts_net_weight} "
                          f"vs 发票总净重{invoice_net_values[0]}",
            })
            if not ok:
                issues.append(Issue(
                    code="KIT_NET_CROSSCHECK_MISMATCH",
                    message=f"箱单后段零件净重合计 {chosen_packing.parts_net_weight} "
                            f"与发票总净重 {invoice_net_values[0]} 不一致",
                    sources=[_source(chosen_packing.filename,
                                     chosen_packing.sheet,
                                     f"{column_letter(chosen_packing.parts_columns['net_weight'])}"
                                     f"{chosen_packing.parts_total_row}",
                                     chosen_packing.parts_total_row)],
                    required_action="请核对发票净重与箱单零件明细",
                ))
    if chosen_packing and chosen_packing.parts_total_qty is not None \
            and invoice_qty_values:
        if all(decimals_close(value, invoice_qty_values[0], max_dp=MAX_DP_QTY)
               for value in invoice_qty_values):
            ok = decimals_close(chosen_packing.parts_total_qty,
                                invoice_qty_values[0], max_dp=MAX_DP_QTY)
            layout["checks"].append({
                "rule": RULE_KIT_QTY_CROSSCHECK,
                "record_key": None,
                "result": "pass" if ok else "fail",
                "detail": f"箱单后段零件总数量{chosen_packing.parts_total_qty} "
                          f"vs 发票明细总数量{invoice_qty_values[0]}",
            })
            if not ok:
                issues.append(Issue(
                    code="KIT_QTY_CROSSCHECK_MISMATCH",
                    message=f"箱单后段零件总数量 {chosen_packing.parts_total_qty} "
                            f"与发票明细总数量 {invoice_qty_values[0]} 不一致",
                    sources=[_source(chosen_packing.filename,
                                     chosen_packing.sheet,
                                     f"{column_letter(chosen_packing.parts_columns['total_qty'])}"
                                     f"{chosen_packing.parts_total_row}",
                                     chosen_packing.parts_total_row)],
                    required_action="请核对发票总数量与箱单零件明细",
                ))

    # ---- 应用表身份：Order Code/MSO/序列号 与已确认身份逐项核对 ----
    app_union = {"order_code": set(), "mso": set(), "serial": set()}
    app_sheets = []
    for wb_values, filename, sheet, region in application_regions:
        parsed_app = _parse_application_identity(
            wb_values, filename, sheet, region, alias_map, issues)
        app_sheets.append(parsed_app)
        for name in ("order_code", "mso", "serial"):
            app_union[name].update(parsed_app.get(name) or set())
    identity_expected = {
        "order_code": {_clean_text(v) for v in _identity_values(
            identity.get("order_code")) if _clean_text(v)},
        "mso": {_clean_text(v) for v in _identity_values(
            identity.get("mso")) if _clean_text(v)},
        "serial": {_clean_text(v) for v in _identity_values(
            identity.get("serial")) if _clean_text(v)},
    }
    for name in ("order_code", "mso", "serial"):
        expected = identity_expected[name]
        parsed_set = app_union[name]
        if not expected or not parsed_set:
            continue
        ok = parsed_set == expected
        layout["checks"].append({
            "rule": RULE_KIT_IDENTITY,
            "record_key": None,
            "result": "pass" if ok else "fail",
            "detail": f"{name} 已确认{sorted(expected)} vs 应用表{sorted(parsed_set)}",
        })
        if not ok:
            issues.append(Issue(
                code="SOURCE_IDENTITY_MISMATCH",
                message=f"应用表 {name} 集合 {sorted(parsed_set)} 与已确认身份 "
                        f"{sorted(expected)} 不一致，疑似错票/混票，未采用",
                sources=[_source(item.get("filename"), item.get("sheet"))
                         for item in app_sheets if item.get("header_row")],
                required_action="请确认附件属于本票后重新确认",
            ))
    layout["kit_application"] = [{
        **item,
        "order_code": sorted(item.get("order_code") or []),
        "mso": sorted(item.get("mso") or []),
        "serial": sorted(item.get("serial") or []),
    } for item in app_sheets]

    # ---- BL 页旧发票号核对 ----
    bl_results = []
    for wb_values, filename, sheet in bl_sheets:
        bl_results.append(_scan_bl_sheet(
            wb_values, filename, sheet, common_invoice_no, issues))
    layout["kit_bl"] = {"sheets": bl_results,
                        "stale_invoice_nos": sorted({
                            number for item in bl_results
                            for number in item["stale"]})}
    for item in bl_results:
        layout["checks"].extend(item.pop("checks", []))

    # ---- 候选事实只核对，不填充 ----
    parsed_facts = {
        "amount": chosen_invoice.total_amount if chosen_invoice
        else common_amount,
        "qty": chosen_invoice.total_qty if chosen_invoice else None,
        "net_weight": chosen_invoice.total_net if chosen_invoice
        else common_net,
        "gross_weight": chosen_packing.gross_weight if chosen_packing else None,
        "packages": chosen_packing.packages if chosen_packing else None,
        "sets": chosen_packing.sets if chosen_packing else None,
        "tare_weight": chosen_packing.tare_weight if chosen_packing else None,
    }
    _check_candidate_facts(candidate, parsed_facts, layout, issues)

    # ---- 最终一致性 ----
    if not result.records:
        conflict_codes = {issue.code for issue in issues}
        if not conflict_codes & {
                "INVOICE_SHEET_CONFLICT", "CONFIRMED_REGION_MISSING",
                "KIT_INVOICE_INVALID", "INVOICE_TOTAL_NOT_FOUND",
                "MULTIPLE_TOTAL_ROWS", "SOURCE_IDENTITY_MISMATCH",
                "INVOICE_NO_NOT_FOUND", "INVOICE_NO_CONFLICT"}:
            issues.append(Issue(
                code="NO_RECORDS",
                message="已确认来源中未解析到任何有效发票明细行",
                sources=[_source(info.filename, info.sheet)
                         for info in invoice_infos],
                required_action="请确认发票明细包含料号/数量/单价/净重",
            ))

    layout["kit_invoice"]["selected_record_count"] = len(result.records)
    layout["kit_invoice"]["conflicts"] = [
        issue.to_dict() for issue in issues
        if issue.code in ("INVOICE_SHEET_CONFLICT",
                          "AUXILIARY_AMOUNT_CONFLICT",
                          "STALE_BL_INVOICE",
                          "SOURCE_IDENTITY_MISMATCH")]
    _close_loaded(loaded)
    return result


def _close_loaded(loaded: list) -> None:
    for item in loaded:
        for workbook in (item.get("wb_values"), item.get("wb_formula")):
            close = getattr(workbook, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _sheet_pairs(infos: list) -> list:
    pairs = []
    for index, left in enumerate(infos):
        for right in infos[index + 1:]:
            pairs.append((left.sheet, right.sheet))
    return pairs


def _packing_totals_agree(left: _PackingParse, right: _PackingParse) -> bool:
    pairs = [
        (left.sets, right.sets, MAX_DP_QTY),
        (left.packages, right.packages, MAX_DP_QTY),
        (left.gross_weight, right.gross_weight, MAX_DP_WEIGHT),
        (left.parts_total_qty, right.parts_total_qty, MAX_DP_QTY),
        (left.parts_net_weight, right.parts_net_weight, MAX_DP_WEIGHT),
    ]
    for a, b, dp in pairs:
        if a is None or b is None or not decimals_close(a, b, max_dp=dp):
            return False
    return True
