"""已确认整机单票快照解析（阶段 3，入口 ``parse_confirmed_machine_sources``）。

边界
----
调用方（E 的离线执行服务）先用 ``BatchService.verify_batch_task_snapshot``
复核任务快照，再经 A 的 ``build_confirmed_request`` 生成请求。本模块只接受
该可信快照里的三样东西：

- ``sources``：本票资产（``path``/``filename``，可带 ``sha256``）；
- ``candidate``：已确认候选（``source_refs`` 区域、``identity``、
  ``mail_row_refs``）；
- ``mail_rows``：本票正文业务行（可传 A 的 confirmed_input.mail_rows，
  也可传已解析的 MailRow 对象；缺失时退回 candidate.mail_row_refs）。

本模块只按 candidate.source_refs 明确列出的文件/工作表/区域读取，绝不扫描
未列入候选的工作表，也不读取父邮件或批次其他票的附件；文件哈希的最终
核验属于 A/E 的可信边界，本模块只做候选引用与调用方传入 sha256 的一致性
防线。

业务规则
--------
- 机器身份逐机核对：MODEL/序列号/PIN/ENGINE/MSO 来自候选区域对应单元格；
  PIN 必须内嵌本机序列号；序列号、MSO 必须与已确认 candidate.identity
  一致；跨机重复序列号、引擎重复、PIN 含他机序列号均显式阻断。
- 缺 MSO 的旧布局（如 BIFF CI 的 ``DEALER ORDER`` 列实为序列号、无 MSO
  列）只在“唯一序列号 ↔ 唯一正文行”时由 mail_rows 补认，机器行来源仍
  保留原工作簿单元格；正文行之间 MSO 不一致或与源表冲突一律拒绝。
- 重量只取本票对应箱单：逐表核对数量/净重/毛重与 TOTAL 行；数量文本
  必须是完整、有限、非负、整数（可带 UNIT/UNITS 等受控单位后缀），绝不
  从任意文本截取数字，明细与 TOTAL 共用同一校验；组内无 MSO 时，仅当
  来源机器清单可唯一映射（本票仅一台机器，或描述中的唯一机型/序列号/
  MSO 精确指向一台）才建 WeightGroup，不按行号、不平均分配；多机组合
  不唯一、重复重量区覆盖同一机器、TOTAL 冲突全部显式报告。
- 价格不在此读取：发票 U'PRICE / T'Price / 合计金额绝不当作逐机单价；
  Total Sales Price 仍由本票确认正文行经 reconcile_machines 提供。
- 不推断国家：发票收票地址、正文 ``region``/``ship_to_party`` 与最终目的
  国可能不同，本模块只返回机器/重量事实，目的国由调用方确认参数提供。

接线建议
--------
``api.process_job`` 的显式 confirmed_input 路径应：

1. 直接使用本模块返回的 ``MachineWorkbookResult``（``invoice_rows`` /
   ``packing_rows`` / ``weight_groups`` / ``invoice_no`` / ``layout`` /
   ``issues``）；
2. 继续走现有 ``reconcile_machines`` → ``build_machine_rows`` → 模板 writer，
   status/layout/provenance 等正式校验保持原样；
3. 不要回退到全父邮件或整个批次重新选表：候选区域就是唯一解析范围。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..models import Issue, Source, WeightGroup
from ..numbertext import (MAX_DP_WEIGHT, decimals_close, normalize_identifier,
                          to_decimal)
from .headers import column_letter, normalize_header, select_total_row
from .machine_wb import (INVOICE_NAME_KEYS, INVOICE_NO_RE, PACKING_NAME_KEYS,
                         MachineRow, MachineWorkbookResult,
                         _extract_group_msos, _text_net_weight)
from .workbook import (SUPPORTED_SUFFIXES, UnsupportedSourceError,
                       WorkbookDependencyError, load_workbook_pair)

# 与既有解析器共享的规则 ID（layout.checks 供上层展示）
RULE_TOTAL_ROW = "MACHINE_PACKING_TOTAL_ROW"
RULE_GROUP_QTY = "MACHINE_WEIGHT_GROUP_QTY"

# confirmed 解析专属规则 ID
RULE_SOURCE_SCOPE = "MACHINE_CONFIRMED_SOURCE_SCOPE"
RULE_REGION_SCOPE = "MACHINE_CONFIRMED_REGION_SCOPE"
RULE_IDENTITY = "MACHINE_CONFIRMED_IDENTITY"
RULE_MSO_FROM_MAIL = "MACHINE_CONFIRMED_MSO_FROM_MAIL"
RULE_PIN_SERIAL = "MACHINE_CONFIRMED_PIN_SERIAL"
RULE_WEIGHT_UNIQUE_MAP = "MACHINE_CONFIRMED_WEIGHT_UNIQUE_MAP"
RULE_WEIGHT_TOTAL = "MACHINE_CONFIRMED_WEIGHT_TOTAL"
RULE_PRICE_SOURCE = "MACHINE_CONFIRMED_PRICE_SOURCE"
RULE_DESTINATION = "MACHINE_CONFIRMED_DESTINATION_NOT_INFERRED"

MACHINE_REGION_ROLES = ("machine_table", "machine_info_partial")
WEIGHT_REGION_ROLE = "weight_table"
SUPPORTED_REGION_ROLES = frozenset(
    MACHINE_REGION_ROLES + (WEIGHT_REGION_ROLE,))

# 有界扫描：区域外的历史 sheet 一律不读
_MAX_REGION_ROWS = 500
_MAX_INVOICE_SCAN_ROWS = 20
_MAX_INVOICE_SCAN_COLS = 20

# 数量文本：完整数值（可带标准千分位/小数）+ 可选的受控单位后缀。
# 不使用 search 截取任意数字：1.5 UNITS、-1 UNIT、bad 1 text 必须拒绝。
_QTY_TEXT_RE = re.compile(
    r"^(?P<number>[+-]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]*)?"
    r"|\.[0-9]+))"
    r"(?:\s*(?P<unit>[A-Za-z]+))?$")
_QTY_UNITS = frozenset({
    "unit", "units", "pcs", "pc", "piece", "pieces", "set", "sets",
})
_QTY_MAX_ADJUSTED = 18  # 防止超大指数转 int 造成技术异常/资源耗尽
_VALUE_MAX_ADJUSTED = 18  # 重量/合计的合理量级；超出即结构化拒绝，避免 Decimal 溢出
_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _issue(code: str, message: str, *, sources=None, record_key=None,
           field_name=None, action="", blocking=True) -> Issue:
    return Issue(code=code, record_key=record_key, field=field_name,
                 message=message, sources=list(sources or []),
                 required_action=action, blocking=blocking)


def _check(layout: dict, rule: str, result: str, detail: str,
           record_key=None) -> None:
    entry = {"rule": rule, "result": result, "detail": detail}
    if record_key is not None:
        entry["record_key"] = record_key
    layout.setdefault("checks", []).append(entry)


def _fold(value) -> str:
    return normalize_identifier(value).casefold()


def _text(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    text = str(value).strip()
    return text or None


def _as_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _col(letter) -> int:
    """列字母 → 1 基列号；非法返回 0。"""
    text = str(letter or "").strip().upper()
    if not text or not text.isalpha():
        return 0
    number = 0
    for char in text:
        number = number * 26 + (ord(char) - ord("A") + 1)
    return number


def _sheet_cell(ws, row: int, col: int):
    if row < 1 or col < 1 or row > ws.max_row or col > ws.max_column:
        return None
    return ws.cell(row, col)


def _cell_text(ws, row: int, col: int) -> Optional[str]:
    cell = _sheet_cell(ws, row, col)
    return _text(cell.value) if cell is not None else None


def _cell_coord(ws, row: int, col: int) -> str:
    cell = _sheet_cell(ws, row, col)
    if cell is not None:
        return cell.coordinate
    return f"{column_letter(max(col, 1))}{max(row, 1)}"


def _row_blank(ws, row: int) -> bool:
    for col in range(1, ws.max_column + 1):
        if _text(ws.cell(row, col).value) is not None:
            return False
    return True


def _scan_block_end(ws, start: int) -> int:
    """连续块结束行（首个全空行前）；有界，不越过区域上限。"""
    last = start - 1
    stop = min(ws.max_row, start + _MAX_REGION_ROWS - 1)
    for row in range(start, stop + 1):
        if _row_blank(ws, row):
            break
        last = row
    return last


def _is_total_row(ws, row: int, anchor_cols) -> bool:
    for col in anchor_cols:
        value = _cell_text(ws, row, col)
        if value is None:
            continue
        norm = normalize_header(value)
        if norm == "total" or norm.startswith("total"):
            return True
    return False


def _qty_from_decimal(number: Decimal, raw) -> tuple:
    """有限、非负、整数校验；不做截断/四舍五入，失败返回结构化错误。"""
    try:
        if not number.is_finite():
            return None, f"数量不是有限数：{raw!r}"
        if number < 0:
            return None, f"数量不能为负：{raw!r}"
        integral = number.to_integral_value()
        if number != integral:
            return None, f"数量必须是整数，未截断小数：{raw!r}"
        if number != 0 and number.adjusted() > _QTY_MAX_ADJUSTED:
            return None, f"数量数值过大：{raw!r}"
        return int(integral), None
    except (ArithmeticError, ValueError, TypeError):
        return None, f"数量无法解析：{raw!r}"


def _parse_qty(value) -> tuple:
    """严格解析整行数量文本，返回 (int|None, error)。

    - 数值单元格：必须有限、非负、整数（不截断小数、不忽略符号）；
    - 文本：完整匹配 ``数字 [+ 受控单位]``，允许标准千分位与整数小数尾；
      单位仅限 UNIT/UNITS/PCS/PC/PIECE/PIECES/SET/SETS；
    - 不搜索/截取任意数字，科学计数法文本、NaN/Infinity、多数字文本、
      任意前后缀全部拒绝；零是否可用由重量组规则（qty == MSO 数）判定。
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, f"数量不是数值：{value!r}"
    if isinstance(value, (int, float, Decimal)):
        number = to_decimal(value)
        if number is None:
            return None, f"数量无法解析：{value!r}"
        return _qty_from_decimal(number, value)
    text = _text(value)
    if text is None:
        return None, None
    match = _QTY_TEXT_RE.fullmatch(text)
    if match is None:
        return None, f"数量不是完整的数值/受控单位：{value!r}"
    unit = match.group("unit")
    if unit is not None and unit.casefold() not in _QTY_UNITS:
        return None, f"数量单位不受支持：{unit!r}（{value!r}）"
    number = to_decimal(match.group("number").replace(",", ""))
    if number is None:
        return None, f"数量无法解析：{value!r}"
    return _qty_from_decimal(number, value)


def _parse_decimal(value) -> tuple:
    """有限、量级受控的 Decimal；非有限/极端指数绝不进入算术。

    NaN/Infinity 直接拒绝；有限但 adjusted 超过 18 位的极端值同样拒绝，
    防止后续求和/decimals_close 触发 Decimal Overflow 等技术异常。
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, f"数值不是数值：{value!r}"
    number = to_decimal(value)
    if number is None:
        if _text(value) is None:
            return None, None
        return None, f"数值无法解析：{value!r}"
    if not number.is_finite():
        return None, f"数值不是有限数：{value!r}"
    try:
        if number != 0 and abs(number.adjusted()) > _VALUE_MAX_ADJUSTED:
            return None, f"数值超出支持范围：{value!r}"
    except (ArithmeticError, ValueError):
        return None, f"数值无法解析：{value!r}"
    return number, None


_STORED_PREFIX_RE = re.compile(r"^[0-9a-f]{16}_")


def _strip_stored_prefix(name: str) -> str:
    base = Path(str(name)).name
    return _STORED_PREFIX_RE.sub("", base, count=1)


def _name_matches(name: str, ref_names: list) -> bool:
    """调用方文件名与候选文件名匹配（容忍 ``<sha16>_原名`` 存储前缀）。"""
    candidate = Path(str(name)).name
    if not candidate:
        return False
    candidate_plain = _strip_stored_prefix(candidate)
    for raw in ref_names:
        ref = Path(str(raw)).name
        if not ref:
            continue
        ref_plain = _strip_stored_prefix(ref)
        if candidate == ref or candidate_plain == ref_plain:
            return True
        if candidate.endswith("_" + ref_plain):
            return True
    return False


def _identity_values(candidate: dict, name: str) -> list:
    identity = candidate.get("identity")
    if not isinstance(identity, dict):
        return []
    raw = identity.get(name)
    if raw is None:
        return []
    if isinstance(raw, (str, int, float)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    values = []
    for item in raw:
        value = normalize_identifier(item)
        if value:
            values.append(value)
    return values


# ---------------------------------------------------------------------------
# 快照资产 / 区域
# ---------------------------------------------------------------------------


@dataclass
class _Region:
    sheet: str
    role: str
    header_row: int
    subheader_row: Optional[int] = None
    columns: dict = field(default_factory=dict)  # field -> 列字母
    data_first_row: Optional[int] = None
    data_last_row: Optional[int] = None
    total_row: Optional[int] = None
    rows: int = 0


@dataclass
class _ConfirmedRef:
    sha256: str
    filenames: list = field(default_factory=list)
    raw_regions: list = field(default_factory=list)
    path: str = ""
    filename: str = ""
    regions: list = field(default_factory=list)


@dataclass
class _ProvidedSource:
    path: str
    filename: str
    sha256: str = ""
    regions: list = field(default_factory=list)


@dataclass
class _MailEvidence:
    mso: str = ""
    serial: str = ""
    table_index: Optional[int] = None
    row: Optional[int] = None
    fields: dict = field(default_factory=dict)
    source: Optional[Source] = None


@dataclass
class _MachineDraft:
    mso: str
    model: Optional[str]
    serial: Optional[str]
    pin: Optional[str]
    engine: Optional[str]
    source: Source
    role: str = "machine_table"

    @property
    def key(self) -> str:
        if self.serial:
            return f"serial:{_fold(self.serial)}"
        if self.mso:
            return f"mso:{_fold(self.mso)}"
        return f"row:{self.source.filename}:{self.source.sheet}:{self.source.row}"


@dataclass
class _WeightRow:
    msos: list
    label: str
    qty: Optional[int]
    net: Optional[Decimal]
    gross: Optional[Decimal]
    source: Source


@dataclass
class _WeightTable:
    region: _Region
    filename: str
    rows: list = field(default_factory=list)
    total_row: Optional[int] = None
    total_qty: Optional[int] = None
    total_net: Optional[Decimal] = None
    total_gross: Optional[Decimal] = None
    total_source: Optional[Source] = None
    sum_qty: int = 0
    sum_net: Decimal = Decimal("0")
    sum_gross: Decimal = Decimal("0")
    complete: bool = True
    gross_complete: bool = True
    valid: bool = False
    qty_invalid: bool = False


@dataclass
class _TicketMachine:
    mso: str
    serial: str = ""
    model: Optional[str] = None
    source: Optional[Source] = None


def _provided_sources(sources) -> list:
    provided = []
    for raw in sources or []:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "")
        filename = str(raw.get("filename") or Path(path).name)
        sha = str(raw.get("sha256") or "").strip().lower()
        regions = raw.get("regions")
        provided.append(_ProvidedSource(
            path=path, filename=filename, sha256=sha,
            regions=regions if isinstance(regions, list) else []))
    return provided


def _resolve_confirmed_sources(sources, candidate: dict, result: MachineWorkbookResult,
                               layout: dict) -> list:
    raw_refs = candidate.get("source_refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        result.issues.append(_issue(
            "CONFIRMED_CANDIDATE_INVALID",
            "已确认候选缺少 source_refs，无法确定本票附件范围",
            action="请重新从已确认批次任务快照生成请求"))
        return []

    provided = _provided_sources(sources)
    refs: dict = {}
    for raw in raw_refs:
        if not isinstance(raw, dict):
            result.issues.append(_issue(
                "CONFIRMED_CANDIDATE_INVALID",
                "候选 source_refs 含非对象条目，无法确定附件范围"))
            continue
        sha = str(raw.get("sha256") or "").strip().lower()
        if not sha:
            result.issues.append(_issue(
                "CONFIRMED_CANDIDATE_INVALID",
                "候选 source_refs 条目缺少 sha256，无法绑定附件"))
            continue
        names = raw.get("filenames")
        if not isinstance(names, (list, tuple)):
            names = [raw.get("filename")] if raw.get("filename") else []
        names = [str(name) for name in names if str(name).strip()]
        regions = raw.get("regions")
        if not isinstance(regions, list):
            regions = []
        if sha in refs:
            ref = refs[sha]
            for name in names:
                if name not in ref.filenames:
                    ref.filenames.append(name)
            ref.raw_regions.extend(regions)
        else:
            refs[sha] = _ConfirmedRef(
                sha256=sha, filenames=names, raw_regions=list(regions))

    used = set()
    resolved = []
    for sha in sorted(refs):
        ref = refs[sha]
        match = None
        for src in provided:
            if src.sha256 and src.sha256 == sha:
                match = src
                break
        if match is None:
            name_hits = [src for src in provided
                         if _name_matches(src.filename, ref.filenames)]
            if len(name_hits) == 1:
                match = name_hits[0]
            elif len(name_hits) > 1:
                result.issues.append(_issue(
                    "CONFIRMED_SOURCE_AMBIGUOUS",
                    f"候选材料 {sha[:12]} 同时匹配多份传入附件，无法唯一绑定",
                    action="请重新从已确认快照生成本票附件清单"))
                continue
        if match is None:
            result.issues.append(_issue(
                "CONFIRMED_SOURCE_MISSING",
                f"候选材料 {sha[:12]}（{', '.join(ref.filenames) or '未命名'}）"
                "不在本票传入附件中",
                action="请重新从已确认批次任务快照读取本票材料"))
            continue
        if match.sha256 and match.sha256 != sha:
            result.issues.append(_issue(
                "SOURCE_HASH_MISMATCH",
                f"附件 {match.filename} 的 sha256 与候选引用 {sha[:12]} 不一致",
                action="材料可能被替换，请重新校验已确认快照"))
            continue
        path = Path(match.path) if match.path else None
        if path is None or not path.is_file():
            result.issues.append(_issue(
                "CONFIRMED_SOURCE_UNREADABLE",
                f"候选材料 {match.filename or sha[:12]} 不存在或不是普通文件",
                action="请重新发布本票快照后再执行"))
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            result.issues.append(_issue(
                "UNSUPPORTED_FILE",
                f"候选材料 {match.filename or path.name} 不是 xlsx/xls 工作簿",
                action="本票整机材料必须包含可读取的 xlsx 或 xls 发票/箱单"))
            continue
        used.add(id(match))
        ref.path = str(path)
        display_name = match.filename or path.name
        # 快照落盘名可能是 ``<sha16>_原始文件名``：来源展示优先用候选
        # source_refs 记录的原始文件名，但读取路径仍用实际落盘文件。
        for original in ref.filenames:
            if _name_matches(display_name, [original]):
                display_name = original
                break
        ref.filename = display_name
        raw_regions = ref.raw_regions or match.regions
        ref.regions = _parse_regions(ref, raw_regions, result, layout)
        if not ref.regions:
            result.issues.append(_issue(
                "CONFIRMED_REGION_MISSING",
                f"候选材料 {ref.filename} 没有可用的整机解析区域",
                sources=[Source(type="workbook", filename=ref.filename)],
                action="请重新确认该附件的发票/箱单区域"))
        resolved.append(ref)

    extras = [src for src in provided if id(src) not in used]
    if extras:
        _check(layout, RULE_SOURCE_SCOPE, "skipped",
               f"忽略 {len(extras)} 份未列入候选 source_refs 的传入附件")
        result.issues.append(_issue(
            "SOURCE_OUTSIDE_CANDIDATE",
            f"传入附件中有 {len(extras)} 份不属于本候选 source_refs，已忽略，"
            "未解析、未并入本票",
            action="请只传本票快照列出的材料",
            blocking=False))
    _check(layout, RULE_REGION_SCOPE, "pass",
           "只解析候选 source_refs 明确列出的文件/工作表/区域，未扫描其他票材料")
    return resolved


def _parse_regions(ref: _ConfirmedRef, raw_regions, result: MachineWorkbookResult,
                   layout: dict) -> list:
    regions = []
    seen = set()
    for raw in raw_regions or []:
        if not isinstance(raw, dict):
            result.issues.append(_issue(
                "CONFIRMED_REGION_INVALID",
                f"候选材料 {ref.filename or ref.sha256[:12]} 含非对象区域条目"))
            continue
        role = str(raw.get("role") or "").strip()
        sheet = str(raw.get("sheet") or "").strip()
        if role not in SUPPORTED_REGION_ROLES:
            continue
        if not sheet:
            result.issues.append(_issue(
                "CONFIRMED_REGION_INVALID",
                f"候选材料 {ref.filename or ref.sha256[:12]} 的区域缺少工作表名"))
            continue
        header_row = _as_int(raw.get("header_row"))
        if header_row is None or header_row < 1:
            result.issues.append(_issue(
                "CONFIRMED_REGION_INVALID",
                f"区域 {sheet}/{role} 缺少有效表头行"))
            continue
        columns_raw = raw.get("columns")
        if not isinstance(columns_raw, dict) or not columns_raw:
            result.issues.append(_issue(
                "CONFIRMED_REGION_INVALID",
                f"区域 {sheet}/{role} 缺少语义列定位"))
            continue
        columns = {}
        invalid = False
        for name, letter in columns_raw.items():
            if _col(letter) < 1:
                invalid = True
                break
            columns[str(name)] = str(letter).strip().upper()
        if invalid:
            result.issues.append(_issue(
                "CONFIRMED_REGION_INVALID",
                f"区域 {sheet}/{role} 的列字母非法：{columns_raw!r}"))
            continue
        if role in MACHINE_REGION_ROLES:
            if "model" not in columns:
                result.issues.append(_issue(
                    "CONFIRMED_REGION_INVALID",
                    f"机器区域 {sheet}/{role} 缺少 MODEL 列定位"))
                continue
        else:
            missing = [name for name in ("description", "qty", "net")
                       if name not in columns]
            if missing:
                result.issues.append(_issue(
                    "WEIGHT_TABLE_INVALID",
                    f"重量区域 {sheet}/{role} 缺少必需列：{', '.join(missing)}",
                    sources=[Source(type="workbook", filename=ref.filename,
                                    sheet=sheet)]))
                continue
        region = _Region(
            sheet=sheet, role=role, header_row=header_row,
            subheader_row=_as_int(raw.get("subheader_row")),
            columns=columns,
            data_first_row=_as_int(raw.get("data_first_row")),
            data_last_row=_as_int(raw.get("data_last_row")),
            total_row=_as_int(raw.get("total_row")),
            rows=_as_int(raw.get("rows")) or 0,
        )
        key = (region.sheet, region.role, region.header_row,
               region.subheader_row, region.total_row,
               tuple(sorted(region.columns.items())))
        if key in seen:
            continue
        seen.add(key)
        regions.append(region)
    return regions


# ---------------------------------------------------------------------------
# 邮件正文行（仅用于缺 MSO 补认与跨机核对）
# ---------------------------------------------------------------------------


def _source_from_dict(raw) -> Optional[Source]:
    if isinstance(raw, Source):
        return raw
    if not isinstance(raw, dict):
        return None
    allowed = ("type", "message_id", "filename", "sheet", "cell",
               "table_index", "row", "detail")
    values = {key: raw.get(key) for key in allowed if raw.get(key) is not None}
    values.setdefault("type", "mail_html")
    return Source(**values)


def _evidence_from_ref(raw: dict) -> _MailEvidence:
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    mso = normalize_identifier(raw.get("mso") or fields.get("mso"))
    serial = normalize_identifier(
        raw.get("serial") or fields.get("serial_number") or fields.get("serial"))
    table_index = _as_int(raw.get("table_index"))
    row = _as_int(raw.get("row"))
    source = Source(type="mail_html", table_index=table_index, row=row,
                    detail="候选 mail_row_refs 已确认正文行")
    return _MailEvidence(mso=mso, serial=serial, table_index=table_index,
                         row=row, fields=dict(fields), source=source)


def _normalise_provided_mail_row(raw) -> Optional[_MailEvidence]:
    if isinstance(raw, dict):
        fields = raw.get("fields")
        if not isinstance(fields, dict):
            fields = {key: value for key, value in raw.items()
                      if key not in ("source", "mso", "serial",
                                     "table_index", "row", "fields")}
        source = _source_from_dict(raw.get("source"))
        mso = normalize_identifier(raw.get("mso") or fields.get("mso"))
        serial = normalize_identifier(
            raw.get("serial") or fields.get("serial_number")
            or fields.get("serial"))
        table_index = _as_int(raw.get("table_index"))
        row = _as_int(raw.get("row"))
        if source is None:
            source = Source(type="mail_html", table_index=table_index, row=row,
                            detail="调用方传入的本票正文行")
        return _MailEvidence(mso=mso, serial=serial, table_index=table_index,
                             row=row, fields=dict(fields), source=source)
    fields = getattr(raw, "fields", None)
    if not isinstance(fields, dict):
        return None
    source = getattr(raw, "source", None)
    if not isinstance(source, Source):
        source = None
    table_index = getattr(source, "table_index", None)
    row = getattr(source, "row", None)
    mso = normalize_identifier(fields.get("mso"))
    serial = normalize_identifier(
        fields.get("serial_number") or fields.get("serial"))
    return _MailEvidence(mso=mso, serial=serial, table_index=table_index,
                         row=row, fields=dict(fields), source=source)


def _collect_mail_evidence(candidate: dict, mail_rows, machines: list,
                           result: MachineWorkbookResult, layout: dict) -> list:
    """只保留本票候选正文行；其他票的正文行绝不进入补认/核对。"""
    raw_refs = candidate.get("mail_row_refs")
    refs = [item for item in raw_refs or [] if isinstance(item, dict)] \
        if isinstance(raw_refs, list) else []
    provided = [ev for ev in (_normalise_provided_mail_row(item)
                              for item in mail_rows or []) if ev is not None]

    evidence: list = []
    if refs:
        ref_evidence = [_evidence_from_ref(item) for item in refs]
        used = set()
        for ref_ev in ref_evidence:
            match = None
            ref_key = (ref_ev.table_index, ref_ev.row)
            if ref_key[0] is not None and ref_key[1] is not None:
                for index, item in enumerate(provided):
                    if index in used:
                        continue
                    if (item.table_index, item.row) == ref_key:
                        match = (index, item)
                        break
            if match is None and (ref_ev.mso or ref_ev.serial):
                for index, item in enumerate(provided):
                    if index in used:
                        continue
                    if ref_ev.mso and item.mso and \
                            _fold(ref_ev.mso) == _fold(item.mso) and \
                            (not ref_ev.serial or not item.serial
                             or _fold(ref_ev.serial) == _fold(item.serial)):
                        match = (index, item)
                        break
                    if ref_ev.serial and item.serial and \
                            _fold(ref_ev.serial) == _fold(item.serial) and \
                            (not ref_ev.mso or not item.mso
                             or _fold(ref_ev.mso) == _fold(item.mso)):
                        match = (index, item)
                        break
            if match is not None:
                used.add(match[0])
                item = match[1]
                conflict = []
                if ref_ev.mso and item.mso and \
                        _fold(ref_ev.mso) != _fold(item.mso):
                    conflict.append(f"MSO {ref_ev.mso!r}≠{item.mso!r}")
                if ref_ev.serial and item.serial and \
                        _fold(ref_ev.serial) != _fold(item.serial):
                    conflict.append(
                        f"序列号 {ref_ev.serial!r}≠{item.serial!r}")
                if conflict:
                    result.issues.append(_issue(
                        "MAIL_ROW_MISMATCH",
                        "调用方正文行与已确认候选快照不一致："
                        + "；".join(conflict) + "；以确认快照为准，未采用传入值",
                        sources=[s for s in (ref_ev.source, item.source)
                                 if s is not None],
                        action="请重新从已确认快照重建正文行，不要改写身份字段"))
                ref_ev.fields = {**(ref_ev.fields or {}), **(item.fields or {})}
                if item.source is not None:
                    ref_ev.source = item.source
            evidence.append(ref_ev)
        ignored = len(provided) - len(used)
    else:
        # 候选没有正文行快照时，仍只接受能落到本票机器身份的传入行。
        machine_msos = {_fold(d.mso) for d in machines if d.mso}
        machine_serials = {_fold(d.serial) for d in machines if d.serial}
        machine_msos.update(_fold(v) for v in _identity_values(candidate, "mso"))
        machine_serials.update(
            _fold(v) for v in _identity_values(candidate, "serial"))
        for item in provided:
            if ((item.mso and _fold(item.mso) in machine_msos)
                    or (item.serial and _fold(item.serial) in machine_serials)):
                evidence.append(item)
        ignored = len(provided) - len(evidence)

    if ignored > 0:
        _check(layout, RULE_SOURCE_SCOPE, "skipped",
               f"忽略 {ignored} 条不属于本候选的正文业务行")
        result.issues.append(_issue(
            "MAIL_ROW_OUTSIDE_CANDIDATE",
            f"传入正文行中有 {ignored} 条不属于本候选，已忽略，"
            "未用于补认 MSO 或跨机核对",
            action="请只传本票 mail_row_refs 对应的正文行",
            blocking=False))

    # 同一序列号出现多条不同 MSO：正文自相矛盾，禁止补认
    by_serial: dict = {}
    for item in evidence:
        if item.serial:
            by_serial.setdefault(_fold(item.serial), []).append(item)
    for serial, items in sorted(by_serial.items()):
        msos = {_fold(item.mso) for item in items if item.mso}
        if len(msos) > 1:
            result.issues.append(_issue(
                "MSO_CONFLICT",
                f"正文行中序列号 {serial} 同时对应多个 MSO "
                f"{sorted({item.mso for item in items if item.mso})}，"
                "无法补认，未采用任何一侧",
                sources=[item.source for item in items if item.source],
                action="请核对正文业务行的 MSO 与序列号配对"))
    return evidence


# ---------------------------------------------------------------------------
# 机器信息区
# ---------------------------------------------------------------------------


def _locate_serial(ws, row: int, serials: list, exclude_cols: set) -> tuple:
    """在数据行内按已确认序列号定位单元格（不按列位置猜测）。"""
    hits = {}
    for col in range(1, min(ws.max_column, 200) + 1):
        if col in exclude_cols:
            continue
        text = _cell_text(ws, row, col)
        if text is None:
            continue
        for serial in serials:
            if text == serial or _fold(text) == _fold(serial):
                hits.setdefault(_fold(serial), (serial, col))
    if len(hits) == 1:
        return next(iter(hits.values()))
    if len(hits) > 1:
        return None, None
    return None, None


def _serial_from_pin(pin: str, serials: list, pin_col: int) -> tuple:
    pin_fold = _fold(pin)
    hits = [s for s in serials if _fold(s) and _fold(s) in pin_fold]
    unique = {_fold(s): s for s in hits}
    if len(unique) == 1:
        return next(iter(unique.values())), pin_col
    return None, None


def _parse_machine_region(ws, region: _Region, ref: _ConfirmedRef,
                          serials: list, result: MachineWorkbookResult,
                          layout: dict) -> list:
    cols = {name: _col(letter) for name, letter in region.columns.items()}
    if region.header_row > ws.max_row:
        result.issues.append(_issue(
            "CONFIRMED_REGION_INVALID",
            f"机器区域 {region.sheet} 表头行 {region.header_row} 超出工作表范围",
            sources=[Source(type="workbook", filename=ref.filename,
                            sheet=region.sheet)]))
        return []
    for name, col in cols.items():
        if col < 1 or col > ws.max_column:
            result.issues.append(_issue(
                "CONFIRMED_REGION_INVALID",
                f"机器区域 {region.sheet} 的 {name} 列超出工作表范围",
                sources=[Source(type="workbook", filename=ref.filename,
                                sheet=region.sheet)]))
            return []
    if region.role == "machine_table":
        start = region.data_first_row or region.header_row + 1
        end = region.data_last_row
        if end is None:
            end = _scan_block_end(ws, start)
        end = min(end, ws.max_row)
    else:
        start = region.header_row + 1
        if region.rows and region.rows > 0:
            end = min(start + region.rows - 1, ws.max_row)
        else:
            end = _scan_block_end(ws, start)
    start = max(start, region.header_row + 1)
    if end < start:
        result.issues.append(_issue(
            "MACHINE_TABLE_INVALID",
            f"机器区域 {region.sheet} 没有数据行（表头 {region.header_row}）",
            sources=[Source(type="workbook", filename=ref.filename,
                            sheet=region.sheet)]))
        return []

    drafts = []
    for row in range(start, end + 1):
        values = {name: _cell_text(ws, row, col) for name, col in cols.items()}
        model = values.get("model")
        serial = values.get("serial")
        mso = normalize_identifier(values.get("mso"))
        pin = values.get("pin")
        engine = values.get("engine")
        if not any((model, serial, mso, pin, engine)):
            continue
        header_keys = {normalize_header(value) for value in
                       (model, serial, mso, pin, engine) if value}
        if header_keys & {
                "model", "mso", "msonumber", "serial", "serialno",
                "loader", "loaderno", "pin", "pinno", "engine",
                "engineno"}:
            continue  # 数据区里重复表头，不是机器行
        serial_col = cols.get("serial") if serial else None
        if not serial:
            exclude = {col for col in cols.values() if col}
            serial, serial_col = _locate_serial(ws, row, serials, exclude)
            if serial is None and pin:
                serial, serial_col = _serial_from_pin(
                    pin, serials, cols.get("pin"))
        if not model and not serial and not mso:
            result.issues.append(_issue(
                "MACHINE_TABLE_INVALID",
                f"机器区域 {region.sheet} 第 {row} 行缺少机型、序列号与 MSO，"
                "无法作为一台机器",
                sources=[Source(type="workbook", filename=ref.filename,
                                sheet=region.sheet,
                                cell=_cell_coord(ws, row, 1), row=row)]))
            continue
        primary_col = (serial_col or cols.get("mso") or cols.get("model")
                       or min(cols.values()))
        source = Source(type="workbook", filename=ref.filename,
                        sheet=region.sheet,
                        cell=_cell_coord(ws, row, primary_col), row=row)
        drafts.append(_MachineDraft(
            mso=mso, model=model, serial=normalize_identifier(serial),
            pin=pin, engine=engine, source=source, role=region.role))

    if drafts:
        first = min(draft.source.row for draft in drafts)
        last = max(draft.source.row for draft in drafts)
        layout["machine_table"].append({
            "sheet": region.sheet,
            "filename": ref.filename,
            "header_row": region.header_row,
            "columns": dict(region.columns),
            "data_first_row": first,
            "data_last_row": last,
            **({"partial": True} if region.role == "machine_info_partial" else {}),
        })
    return drafts


# ---------------------------------------------------------------------------
# MSO 补认 / 机器行合并 / 身份核对
# ---------------------------------------------------------------------------


def _serial_mso_map(evidence: list) -> tuple:
    """序列号 → 唯一 MSO；返回 (map, conflict_serials)。"""
    by_serial: dict = {}
    for item in evidence:
        if item.serial:
            by_serial.setdefault(_fold(item.serial), []).append(item)
    mapping = {}
    conflicts = {}
    for serial, items in by_serial.items():
        msos = {_fold(item.mso) for item in items if item.mso}
        if len(msos) == 1:
            mapping[serial] = next(item.mso for item in items if item.mso)
        elif len(msos) > 1:
            conflicts[serial] = sorted({item.mso for item in items if item.mso})
    return mapping, conflicts


def _fill_missing_msos(drafts: list, evidence: list, candidate: dict,
                       result: MachineWorkbookResult, layout: dict) -> None:
    mapping, conflicts = _serial_mso_map(evidence)
    cand_msos = {_fold(v) for v in _identity_values(candidate, "mso")}
    cand_serials = {_fold(v) for v in _identity_values(candidate, "serial")}
    for draft in drafts:
        if draft.mso:
            continued = mapping.get(_fold(draft.serial)) if draft.serial else None
            if continued and _fold(continued) != _fold(draft.mso):
                result.issues.append(_issue(
                    "MSO_CONFLICT",
                    f"机器序列号 {draft.serial} 在源表中 MSO={draft.mso}，"
                    f"本票正文行给出 MSO={continued}，两者冲突，"
                    "保留源表值且不补认正文值",
                    sources=[draft.source]
                    + [item.source for item in evidence
                       if item.serial and _fold(item.serial) == _fold(draft.serial)
                       and item.source is not None],
                    record_key=draft.mso,
                    field_name="mso",
                    action="请核对正文行与发票机器信息表的 MSO"))
            continue
        if not draft.serial:
            result.issues.append(_issue(
                "MISSING_MSO",
                f"机器行（{draft.source.sheet}!{draft.source.cell}）缺 MSO 且"
                "无法定位唯一序列号，不能由正文补认",
                sources=[draft.source],
                action="请补充含 MSO 或序列号的机器信息"))
            continue
        serial_key = _fold(draft.serial)
        if serial_key in conflicts:
            result.issues.append(_issue(
                "MSO_CONFLICT",
                f"序列号 {draft.serial} 在正文行中对应多个 MSO "
                f"{conflicts[serial_key]}，无法唯一补认",
                sources=[draft.source],
                action="请核对正文业务行的 MSO 与序列号配对"))
            continue
        filled = mapping.get(serial_key)
        if filled is None:
            # 旧布局兜底：候选身份本身已确认且只有这一台/一个 MSO，
            # 且序列号与候选序列号一致时，才允许按已确认身份补认。
            distinct_serials = {_fold(d.serial) for d in drafts if d.serial}
            single_machine = (bool(drafts) and all(d.serial for d in drafts)
                              and len(distinct_serials) == 1)
            if (single_machine and len(cand_msos) == 1
                    and (not cand_serials or serial_key in cand_serials)):
                filled = next(v for v in _identity_values(candidate, "mso"))
                detail = (f"机器行缺 MSO，正文无对应行；按已确认候选唯一 MSO "
                          f"{filled} 补认；机器行来源保持 "
                          f"{draft.source.sheet}!{draft.source.cell}")
                _check(layout, RULE_MSO_FROM_MAIL, "pass", detail,
                       record_key=draft.serial)
            else:
                result.issues.append(_issue(
                    "MISSING_MSO",
                    f"机器序列号 {draft.serial} 缺 MSO，正文行无法唯一补认",
                    sources=[draft.source],
                    action="请补充正文业务表中的 MSO 或机器信息表中的 MSO 列"))
                continue
        else:
            if cand_msos and _fold(filled) not in cand_msos:
                result.issues.append(_issue(
                    "CONFIRMED_IDENTITY_MISMATCH",
                    f"正文补认 MSO {filled} 不在已确认候选身份 "
                    f"{sorted(_identity_values(candidate, 'mso'))} 中，未采用",
                    sources=[draft.source],
                    action="请核对候选身份与机器序列号的对应关系"))
                continue
            detail = (f"机器序列号 {draft.serial} 缺 MSO，按唯一正文行补认 "
                      f"{filled}；机器行来源保持 "
                      f"{draft.source.sheet}!{draft.source.cell}")
            _check(layout, RULE_MSO_FROM_MAIL, "pass", detail,
                   record_key=draft.serial)
        draft.mso = filled


def _merge_machine_drafts(drafts: list, result: MachineWorkbookResult,
                          layout: dict) -> list:
    ordered = sorted(
        drafts,
        key=lambda d: (d.source.filename or "", d.source.sheet or "",
                       d.source.row or 0, _fold(d.serial or ""),
                       _fold(d.mso or "")))
    machines: list = []
    by_serial: dict = {}
    by_mso: dict = {}

    def values_conflict(existing: _MachineDraft, item: _MachineDraft) -> list:
        problems = []
        for name in ("mso", "model", "serial", "pin", "engine"):
            left = getattr(existing, name)
            right = getattr(item, name)
            if left and right and _fold(left) != _fold(right):
                problems.append(f"{name}: {left!r}≠{right!r}")
        return problems

    for item in ordered:
        existing = None
        if item.serial:
            existing = by_serial.get(_fold(item.serial))
        if existing is None and item.mso:
            existing = by_mso.get(_fold(item.mso))
        if existing is None:
            machines.append(item)
            if item.serial:
                by_serial[_fold(item.serial)] = item
            if item.mso:
                by_mso[_fold(item.mso)] = item
            continue

        problems = values_conflict(existing, item)
        same_table = (existing.source.filename == item.source.filename
                      and existing.source.sheet == item.source.sheet)
        if problems:
            if (existing.serial and item.serial
                    and _fold(existing.serial) == _fold(item.serial)):
                result.issues.append(_issue(
                    "DUPLICATE_SERIAL",
                    f"序列号 {item.serial} 同时对应不同机器取值（"
                    f"MSO {existing.mso or '-'} vs {item.mso or '-'}），"
                    "未按文件顺序合并",
                    sources=[existing.source, item.source],
                    record_key=existing.mso or item.mso or item.serial,
                    field_name="serial_number",
                    action="请核对序列号与 MSO 的对应关系"))
            if (existing.mso and item.mso
                    and _fold(existing.mso) == _fold(item.mso)
                    and existing.serial and item.serial
                    and _fold(existing.serial) != _fold(item.serial)):
                result.issues.append(_issue(
                    "MSO_CONFLICT",
                    f"同一 MSO {item.mso} 对应多个序列号："
                    f"{existing.serial} vs {item.serial}，未按文件顺序取舍",
                    sources=[existing.source, item.source],
                    record_key=item.mso, field_name="mso",
                    action="请核对 MSO 与序列号的对应关系"))
            result.issues.append(_issue(
                "MACHINE_ROW_CONFLICT",
                f"同一机器在候选多个区域中取值不一致（"
                f"{existing.source.filename}!{existing.source.sheet}"
                f"!{existing.source.cell} vs {item.source.filename}"
                f"!{item.source.sheet}!{item.source.cell}）："
                + "；".join(problems) + "；未按文件顺序静默取一侧",
                sources=[existing.source, item.source],
                action="请人工确认该机器以哪份材料为准"))
            continue
        if same_table:
            result.issues.append(_issue(
                "DUPLICATE_MACHINE_ROW",
                f"同一机器区域中重复出现机器 "
                f"{existing.mso or existing.serial or existing.source.cell}",
                sources=[existing.source, item.source],
                action="请确认机器信息表不存在重复行"))
            continue

        filled = []
        for name in ("mso", "model", "serial", "pin", "engine"):
            if not getattr(existing, name) and getattr(item, name):
                setattr(existing, name, getattr(item, name))
                filled.append(name)
        _check(layout, RULE_IDENTITY, "pass",
               f"同一机器跨来源合并：{existing.source.filename}!"
               f"{existing.source.sheet}!{existing.source.cell} + "
               f"{item.source.filename}!{item.source.sheet}!"
               f"{item.source.cell}"
               + (f"；补齐 {', '.join(filled)}" if filled else ""))
        if existing.serial:
            by_serial.setdefault(_fold(existing.serial), existing)
        if existing.mso:
            by_mso.setdefault(_fold(existing.mso), existing)
    return machines


def _canonicalise_identity(machines: list, candidate: dict,
                          layout: dict) -> None:
    """工作簿与已确认身份仅大小写不同 → 采用确认身份的规范写法。

    reconcile 的标识符比较大小写敏感；这里只做 fold 相同的大小写归一，
    不改变任何业务字符，也不接受不同值。
    """
    mso_by_fold = {_fold(v): v for v in _identity_values(candidate, "mso")}
    serial_by_fold = {
        _fold(v): v for v in _identity_values(candidate, "serial")}
    for draft in machines:
        if draft.mso:
            canonical = mso_by_fold.get(_fold(draft.mso))
            if canonical and canonical != draft.mso:
                _check(layout, RULE_IDENTITY, "pass",
                       f"机器 MSO 大小写按已确认身份归一：{draft.mso!r} → "
                       f"{canonical!r}（值相同，仅大小写）",
                       record_key=draft.mso)
                draft.mso = canonical
        if draft.serial:
            canonical = serial_by_fold.get(_fold(draft.serial))
            if canonical and canonical != draft.serial:
                _check(layout, RULE_IDENTITY, "pass",
                       f"机器序列号大小写按已确认身份归一：{draft.serial!r} → "
                       f"{canonical!r}（值相同，仅大小写）",
                       record_key=draft.serial)
                draft.serial = canonical


def _validate_identity(machines: list, candidate: dict,
                       result: MachineWorkbookResult, layout: dict) -> None:
    cand_msos = {_fold(v) for v in _identity_values(candidate, "mso")}
    cand_serials = {_fold(v) for v in _identity_values(candidate, "serial")}
    machine_msos = {_fold(d.mso) for d in machines if d.mso}
    machine_serials = {_fold(d.serial) for d in machines if d.serial}
    machine_sources = [d.source for d in machines if d.source]

    if cand_msos:
        missing = sorted(cand_msos - machine_msos)
        extra = sorted(machine_msos - cand_msos)
        for value in missing:
            result.issues.append(_issue(
                "CONFIRMED_IDENTITY_MISMATCH",
                f"已确认候选 MSO {value} 未在机器信息区域中找到",
                sources=machine_sources,
                action="请核对候选身份与发票机器信息表"))
        for value in extra:
            result.issues.append(_issue(
                "CONFIRMED_IDENTITY_MISMATCH",
                f"机器信息区域 MSO {value} 不在已确认候选身份 "
                f"{sorted(cand_msos)} 中",
                sources=machine_sources,
                action="请核对机器信息表是否属于本票"))
        _check(layout, RULE_IDENTITY,
               "pass" if not missing and not extra else "fail",
               f"候选 MSO {sorted(cand_msos)} vs 机器表 MSO "
               f"{sorted(machine_msos)}")
    if cand_serials:
        missing = sorted(cand_serials - machine_serials)
        extra = sorted(machine_serials - cand_serials)
        for value in missing:
            result.issues.append(_issue(
                "CONFIRMED_IDENTITY_MISMATCH",
                f"已确认候选序列号 {value} 未在机器信息区域中找到",
                sources=machine_sources,
                action="请核对候选身份与发票机器信息表"))
        for value in extra:
            result.issues.append(_issue(
                "CONFIRMED_IDENTITY_MISMATCH",
                f"机器信息区域序列号 {value} 不在已确认候选身份 "
                f"{sorted(cand_serials)} 中",
                sources=machine_sources,
                action="请核对机器信息表是否属于本票"))
        _check(layout, RULE_IDENTITY,
               "pass" if not missing and not extra else "fail",
               f"候选序列号 {sorted(cand_serials)} vs 机器表序列号 "
               f"{sorted(machine_serials)}")
    if not cand_msos and not cand_serials:
        _check(layout, RULE_IDENTITY, "skipped",
               "候选未提供 MSO/序列号身份，仅按工作簿自身核对机器唯一性")


def _pin_contains(pin: str, serial: str) -> bool:
    pin_text = _NON_ALNUM_RE.sub("", _fold(pin))
    serial_text = _NON_ALNUM_RE.sub("", _fold(serial))
    return bool(serial_text) and serial_text in pin_text


def _per_machine_checks(machines: list, result: MachineWorkbookResult,
                        layout: dict) -> None:
    serial_map: dict = {}
    engine_map: dict = {}
    for draft in machines:
        record_key = draft.mso or draft.serial or \
            f"{draft.source.sheet}!{draft.source.cell}"
        if not draft.model:
            # 机型可由本票正文 Material 提供；缺机型不单独阻断，但显式留痕，
            # 避免合并单元格只写首行时把合法布局判死。
            result.issues.append(_issue(
                "MACHINE_ROW_INCOMPLETE",
                f"机器 {record_key} 缺少机型 MODEL，机型只由本票正文行提供",
                sources=[draft.source], record_key=record_key,
                field_name="model",
                action="请确认机器信息表 MODEL 取值或正文 Material",
                blocking=False))
        if not draft.serial:
            result.issues.append(_issue(
                "MACHINE_TABLE_INVALID",
                f"机器 {record_key} 缺少序列号，无法逐机核对",
                sources=[draft.source], record_key=record_key,
                field_name="serial_number",
                action="请补充机器序列号"))
        else:
            serial_key = _fold(draft.serial)
            if serial_key in serial_map:
                result.issues.append(_issue(
                    "DUPLICATE_SERIAL",
                    f"序列号 {draft.serial} 同时出现在两台机器上："
                    f"{serial_map[serial_key].source.sheet}!"
                    f"{serial_map[serial_key].source.cell} 与 "
                    f"{draft.source.sheet}!{draft.source.cell}",
                    sources=[serial_map[serial_key].source, draft.source],
                    record_key=record_key,
                    field_name="serial_number",
                    action="请核对序列号与 MSO 的对应关系"))
            else:
                serial_map[serial_key] = draft
        if draft.engine:
            engine_key = _fold(draft.engine)
            if engine_key in engine_map:
                result.issues.append(_issue(
                    "ENGINE_DUPLICATE",
                    f"发动机号 {draft.engine} 同时出现在两台机器上："
                    f"{engine_map[engine_key].source.sheet}!"
                    f"{engine_map[engine_key].source.cell} 与 "
                    f"{draft.source.sheet}!{draft.source.cell}",
                    sources=[engine_map[engine_key].source, draft.source],
                    record_key=record_key, field_name="engine",
                    action="请核对机器发动机号"))
            else:
                engine_map[engine_key] = draft
        if draft.pin and draft.serial:
            own = _pin_contains(draft.pin, draft.serial)
            other = None
            if not own:
                for candidate in machines:
                    if candidate is draft or not candidate.serial:
                        continue
                    if _pin_contains(draft.pin, candidate.serial):
                        other = candidate
                        break
            if own:
                _check(layout, RULE_PIN_SERIAL, "pass",
                       f"PIN {draft.pin} 内嵌序列号 {draft.serial}",
                       record_key=record_key)
            elif other is not None:
                result.issues.append(_issue(
                    "SERIAL_CONFLICT",
                    f"机器 {record_key} 的 PIN {draft.pin} 内嵌的是另一台"
                    f"机器的序列号 {other.serial}，与本机序列号 "
                    f"{draft.serial} 不一致",
                    sources=[draft.source, other.source],
                    record_key=record_key, field_name="serial_number",
                    action="请核对 PIN 与序列号的对应关系"))
                _check(layout, RULE_PIN_SERIAL, "fail",
                       f"PIN {draft.pin} 与序列号 {draft.serial} 不一致",
                       record_key=record_key)
            else:
                result.issues.append(_issue(
                    "SERIAL_CONFLICT",
                    f"机器 {record_key} 的 PIN {draft.pin} 未内嵌本机序列号 "
                    f"{draft.serial}",
                    sources=[draft.source], record_key=record_key,
                    field_name="serial_number",
                    action="请核对 PIN 与序列号的对应关系"))
                _check(layout, RULE_PIN_SERIAL, "fail",
                       f"PIN {draft.pin} 未包含序列号 {draft.serial}",
                       record_key=record_key)
        elif draft.pin:
            _check(layout, RULE_PIN_SERIAL, "skipped",
                   f"机器 {record_key} 缺序列号，PIN 无法核对",
                   record_key=record_key)


def _check_mail_cross(evidence: list, machines: list,
                      result: MachineWorkbookResult, layout: dict) -> None:
    by_mso: dict = {}
    by_serial: dict = {}
    for draft in machines:
        if draft.mso:
            by_mso.setdefault(_fold(draft.mso), []).append(draft)
        if draft.serial:
            by_serial.setdefault(_fold(draft.serial), []).append(draft)
    for item in evidence:
        mso_hits = by_mso.get(_fold(item.mso), []) if item.mso else []
        serial_hits = by_serial.get(_fold(item.serial), []) if item.serial else []
        if len(mso_hits) > 1 or len(serial_hits) > 1:
            result.issues.append(_issue(
                "MAIL_ROW_MACHINE_AMBIGUOUS",
                f"正文行（MSO={item.mso or '-'} 序列号="
                f"{item.serial or '-'}）命中多台本票机器，无法唯一映射",
                sources=[item.source] if item.source else [],
                action="请核对正文行与附件机器清单"))
            continue
        if item.mso and item.serial:
            if mso_hits and serial_hits and mso_hits[0] is not serial_hits[0]:
                result.issues.append(_issue(
                    "MSO_SERIAL_CROSS",
                    f"正文行 MSO={item.mso} 与序列号 {item.serial} 分属本票"
                    "不同机器，行内交叉，未采用",
                    sources=[item.source] if item.source else [],
                    action="请核对正文行的 MSO 与序列号配对"))
            elif mso_hits and not serial_hits:
                machine = mso_hits[0]
                if machine.serial:
                    result.issues.append(_issue(
                        "SERIAL_CONFLICT",
                        f"正文行 MSO={item.mso} 的序列号 {item.serial} 与"
                        f"机器信息表序列号 {machine.serial} 不一致",
                        sources=[item.source, machine.source]
                        if item.source else [machine.source],
                        record_key=item.mso,
                        field_name="serial_number",
                        action="请核对机器序列号与 MSO 的对应关系"))
            elif serial_hits and not mso_hits:
                machine = serial_hits[0]
                if machine.mso and _fold(machine.mso) != _fold(item.mso):
                    result.issues.append(_issue(
                        "MSO_CONFLICT",
                        f"正文行序列号 {item.serial} 命中机器 MSO="
                        f"{machine.mso}，但正文行 MSO={item.mso} 与之冲突，"
                        "未补认、未采用",
                        sources=[item.source, machine.source]
                        if item.source else [machine.source],
                        record_key=machine.mso,
                        field_name="mso",
                        action="请核对正文行的 MSO 与机器序列号归属"))
            elif not mso_hits and not serial_hits:
                result.issues.append(_issue(
                    "MAIL_ROW_MACHINE_AMBIGUOUS",
                    f"正文行（MSO={item.mso} 序列号={item.serial}）无法映射到"
                    "本票任何机器，未采用",
                    sources=[item.source] if item.source else [],
                    action="请核对正文行业务身份与附件机器清单"))
        elif item.mso and not mso_hits:
            result.issues.append(_issue(
                "MAIL_ROW_MACHINE_AMBIGUOUS",
                f"正文行 MSO={item.mso} 不在本票机器清单中，未采用",
                sources=[item.source] if item.source else [],
                action="请核对正文行是否属于本票"))
        elif item.serial and not serial_hits:
            result.issues.append(_issue(
                "MAIL_ROW_MACHINE_AMBIGUOUS",
                f"正文行序列号 {item.serial} 不在本票机器清单中，未采用",
                sources=[item.source] if item.source else [],
                action="请核对正文行是否属于本票"))


# ---------------------------------------------------------------------------
# 重量表
# ---------------------------------------------------------------------------


def _parse_weight_region(ws, region: _Region, ref: _ConfirmedRef,
                         result: MachineWorkbookResult,
                         layout: dict) -> _WeightTable:
    table = _WeightTable(region=region, filename=ref.filename)
    cols = {name: _col(letter) for name, letter in region.columns.items()}
    for name, col in cols.items():
        if col < 1 or col > ws.max_column:
            result.issues.append(_issue(
                "WEIGHT_TABLE_INVALID",
                f"重量区域 {region.sheet} 的 {name} 列超出工作表范围",
                sources=[Source(type="workbook", filename=ref.filename,
                                sheet=region.sheet)]))
            return table
    if region.header_row > ws.max_row:
        result.issues.append(_issue(
            "WEIGHT_TABLE_INVALID",
            f"重量区域 {region.sheet} 表头行 {region.header_row} 超出范围",
            sources=[Source(type="workbook", filename=ref.filename,
                            sheet=region.sheet)]))
        return table

    header = region.subheader_row or region.header_row
    total_row, total_error = select_total_row(
        ws, header,
        [cols.get("qty"), cols.get("net"), cols.get("gross")],
        anchor_cols=(1, cols["description"]))
    if total_error == "multiple":
        result.issues.append(_issue(
            "MULTIPLE_TOTAL_ROWS",
            f"箱单 {region.sheet} 表头下方存在多张同属一个表块的合计行，"
            "无法确定 TOTAL 锚点，未猜测取哪张",
            sources=[Source(type="workbook", filename=ref.filename,
                            sheet=region.sheet)]))
        return table
    confirmed_total = region.total_row
    if confirmed_total is not None:
        if confirmed_total < 1 or confirmed_total > ws.max_row:
            result.issues.append(_issue(
                "WEIGHT_TABLE_INVALID",
                f"箱单 {region.sheet} 的 TOTAL 行 {confirmed_total} 超出范围",
                sources=[Source(type="workbook", filename=ref.filename,
                                sheet=region.sheet)]))
            return table
        if not _is_total_row(ws, confirmed_total,
                             (1, cols["description"])):
            result.issues.append(_issue(
                "WEIGHT_TABLE_INVALID",
                f"箱单 {region.sheet} 第 {confirmed_total} 行不是 TOTAL 行",
                sources=[Source(type="workbook", filename=ref.filename,
                                sheet=region.sheet,
                                cell=_cell_coord(ws, confirmed_total, 1))]))
            return table
        if total_row is not None and total_row != confirmed_total:
            result.issues.append(_issue(
                "WEIGHT_TABLE_INVALID",
                f"箱单 {region.sheet} 的已确认 TOTAL 行 {confirmed_total} 与"
                f"当前唯一锚点 {total_row} 不一致，未按任一顺序取舍",
                sources=[Source(type="workbook", filename=ref.filename,
                                sheet=region.sheet)]))
            return table
        total_row = confirmed_total
    if total_row is None:
        _check(layout, RULE_TOTAL_ROW, "skipped",
               f"箱单 {region.sheet} 未找到 TOTAL 行，合计核对未执行")
        result.issues.append(_issue(
            "WEIGHT_TOTAL_MISSING",
            f"箱单 {region.sheet} 未找到 TOTAL 行，无法核对数量/净重合计",
            sources=[Source(type="workbook", filename=ref.filename,
                            sheet=region.sheet)],
            action="请确认箱单 TOTAL 行后重试",
            blocking=False))

    start = header + 1
    end = (total_row - 1) if total_row is not None else _scan_block_end(ws, start)
    end = min(end, ws.max_row)
    if end < start:
        result.issues.append(_issue(
            "WEIGHT_TABLE_INVALID",
            f"箱单 {region.sheet} 没有重量明细行",
            sources=[Source(type="workbook", filename=ref.filename,
                            sheet=region.sheet)]))
        return table

    for row in range(start, end + 1):
        description = _cell_text(ws, row, cols["description"]) or ""
        qty_cell = _sheet_cell(ws, row, cols["qty"])
        net_cell = _sheet_cell(ws, row, cols["net"])
        gross_cell = (_sheet_cell(ws, row, cols["gross"])
                      if "gross" in cols else None)
        qty, qty_error = _parse_qty(
            qty_cell.value if qty_cell is not None else None)
        net, net_error = _parse_decimal(
            net_cell.value if net_cell is not None else None)
        gross = None
        gross_error = None
        if gross_cell is not None:
            gross, gross_error = _parse_decimal(gross_cell.value)
        if not description and qty is None and net is None and gross is None:
            continue
        if qty_error or net_error or gross_error:
            error_sources = []
            for field_name, error, cell in (
                    ("qty", qty_error, qty_cell),
                    ("net", net_error, net_cell),
                    ("gross", gross_error, gross_cell)):
                if not error or field_name not in cols:
                    continue
                error_sources.append(Source(
                    type="workbook", filename=ref.filename,
                    sheet=region.sheet,
                    cell=_cell_coord(ws, row, cols[field_name]), row=row))
            if not error_sources:
                error_sources.append(Source(
                    type="workbook", filename=ref.filename,
                    sheet=region.sheet,
                    cell=_cell_coord(ws, row, cols["description"]),
                    row=row))
            result.issues.append(_issue(
                "WEIGHT_GROUP_INCOMPLETE",
                f"箱单 {region.sheet} 第 {row} 行数值无法解析："
                + "；".join(error for error in
                            (qty_error, net_error, gross_error) if error),
                sources=error_sources,
                action="请提供完整、有限的数量/净重/毛重数值"))
            table.complete = False
            if qty_error:
                table.qty_invalid = True
            continue
        if qty is None and net is None:
            # 有毛重或明确的重量组标签却缺数量/净重：必须留痕，不能当说明行
            # 静默跳过；纯文字说明行（无 MSO、无数值）仍不猜测。
            if gross is not None or _extract_group_msos(description):
                table.complete = False
                missing_sources = [Source(
                    type="workbook", filename=ref.filename,
                    sheet=region.sheet,
                    cell=_cell_coord(ws, row, cols["description"]), row=row)]
                for field_name in ("qty", "net"):
                    if field_name in cols:
                        missing_sources.append(Source(
                            type="workbook", filename=ref.filename,
                            sheet=region.sheet,
                            cell=_cell_coord(ws, row, cols[field_name]),
                            row=row))
                result.issues.append(_issue(
                    "WEIGHT_GROUP_INCOMPLETE",
                    f"箱单 {region.sheet} 第 {row} 行缺少数量与净重："
                    f"{description.strip()!r}",
                    sources=missing_sources,
                    action="请补齐箱单行的数量与净重"))
            continue
        msos = _extract_group_msos(description) or []
        source = Source(type="workbook", filename=ref.filename,
                        sheet=region.sheet,
                        cell=_cell_coord(ws, row, cols["description"]),
                        row=row)
        table.rows.append(_WeightRow(
            msos=[normalize_identifier(m) for m in msos],
            label=description, qty=qty, net=net, gross=gross, source=source))
        if qty is None or net is None:
            table.complete = False
            missing_sources = [source]
            for field_name, parsed in (("qty", qty), ("net", net)):
                if parsed is None and field_name in cols:
                    missing_sources.append(Source(
                        type="workbook", filename=ref.filename,
                        sheet=region.sheet,
                        cell=_cell_coord(ws, row, cols[field_name]), row=row))
            result.issues.append(_issue(
                "WEIGHT_GROUP_INCOMPLETE",
                f"箱单 {region.sheet} 第 {row} 行缺少数量或净重："
                f"{description.strip()!r}",
                sources=missing_sources,
                action="请补齐箱单行的数量与净重"))
            continue
        table.sum_qty += qty
        table.sum_net += net
        if gross is None and "gross" in cols:
            table.gross_complete = False
        elif gross is not None:
            table.sum_gross += gross

    if not table.rows:
        result.issues.append(_issue(
            "WEIGHT_TABLE_INVALID",
            f"箱单 {region.sheet} 没有可用重量明细行",
            sources=[Source(type="workbook", filename=ref.filename,
                            sheet=region.sheet)]))
        return table
    table.valid = True
    table.total_row = total_row
    if total_row is not None:
        total_source = Source(
            type="workbook", filename=ref.filename, sheet=region.sheet,
            cell=_cell_coord(ws, total_row, cols["description"]),
            row=total_row)
        table.total_source = total_source
        qty_cell = _sheet_cell(ws, total_row, cols["qty"])
        net_cell = _sheet_cell(ws, total_row, cols["net"])
        gross_cell = (_sheet_cell(ws, total_row, cols["gross"])
                      if "gross" in cols else None)
        table.total_qty, qty_error = _parse_qty(
            qty_cell.value if qty_cell is not None else None)
        table.total_net, net_error = _parse_decimal(
            net_cell.value if net_cell is not None else None)
        if gross_cell is not None:
            table.total_gross, gross_error = _parse_decimal(gross_cell.value)
        else:
            gross_error = None
        if qty_error or net_error or gross_error:
            error_sources = []
            for field_name, error, cell in (
                    ("qty", qty_error, qty_cell),
                    ("net", net_error, net_cell),
                    ("gross", gross_error, gross_cell)):
                if not error or field_name not in cols:
                    continue
                error_sources.append(Source(
                    type="workbook", filename=ref.filename,
                    sheet=region.sheet,
                    cell=_cell_coord(ws, total_row, cols[field_name]),
                    row=total_row))
            if not error_sources:
                error_sources = [total_source]
            result.issues.append(_issue(
                "WEIGHT_TOTAL_MISMATCH",
                f"箱单 {region.sheet} TOTAL 行数值无法解析："
                + "；".join(error for error in
                            (qty_error, net_error, gross_error) if error),
                sources=error_sources,
                action="请提供完整、有限的数量/净重/毛重 TOTAL 数值"))
            if qty_error:
                table.qty_invalid = True
        elif (table.total_qty is None and table.total_net is None
              and table.total_gross is None):
            _check(layout, RULE_TOTAL_ROW, "skipped",
                   f"箱单 {region.sheet} TOTAL 行数量/净重/毛重为空，"
                   "合计核对未执行")
        elif not table.complete:
            _check(layout, RULE_TOTAL_ROW, "skipped",
                   f"箱单 {region.sheet} 存在缺数量/净重的明细行，"
                   "合计核对未执行")
        else:
            net_ok = (table.total_net is None
                      or decimals_close(table.sum_net, table.total_net,
                                        MAX_DP_WEIGHT))
            qty_ok = (table.total_qty is None
                      or table.sum_qty == table.total_qty)
            gross_ok = (table.total_gross is None
                        or not table.gross_complete
                        or decimals_close(table.sum_gross, table.total_gross,
                                          MAX_DP_WEIGHT))
            _check(layout, RULE_TOTAL_ROW,
                   "pass" if net_ok and qty_ok and gross_ok else "fail",
                   f"箱单 {region.sheet} 明细合计 数量{table.sum_qty}/"
                   f"净重{table.sum_net}/毛重{table.sum_gross} vs TOTAL行 "
                   f"数量{table.total_qty}/净重{table.total_net}/"
                   f"毛重{table.total_gross}")
            if not (net_ok and qty_ok and gross_ok):
                result.issues.append(_issue(
                    "WEIGHT_TOTAL_MISMATCH",
                    f"箱单 {region.sheet} 明细合计（数量{table.sum_qty} "
                    f"净重{table.sum_net} 毛重{table.sum_gross}）与 TOTAL 行"
                    f"（数量{table.total_qty} 净重{table.total_net} "
                    f"毛重{table.total_gross}）不一致",
                    sources=[total_source],
                    action="请核对箱单明细与 TOTAL 行"))
    layout["weight_table"].append({
        "sheet": region.sheet,
        "filename": ref.filename,
        "header_row": region.header_row,
        "subheader_row": region.subheader_row,
        "columns": dict(region.columns),
        "total_row": total_row,
    })
    return table


# ---------------------------------------------------------------------------
# 重量组分配
# ---------------------------------------------------------------------------


def _ticket_machine_pool(machines: list, candidate: dict) -> list:
    if machines:
        return [_TicketMachine(mso=d.mso, serial=d.serial or "",
                               model=d.model, source=d.source)
                for d in machines]
    msos = _identity_values(candidate, "mso")
    serials = _identity_values(candidate, "serial")
    if len(msos) == 1:
        return [_TicketMachine(mso=msos[0],
                               serial=serials[0] if len(serials) == 1 else "")]
    return [_TicketMachine(mso=value) for value in msos]


def _contains_token(text: str, value: str) -> bool:
    if not text or not value:
        return False
    pattern = (r"(?<![0-9A-Za-z])" + re.escape(value.casefold())
               + r"(?![0-9A-Za-z])")
    return re.search(pattern, text.casefold()) is not None


def _unique_weight_machine(row: _WeightRow, pool: list) -> tuple:
    """无组内 MSO 时，按来源机器清单做唯一映射；返回 (machine, reason)。"""
    if not pool:
        return None, "no_machine"
    if len(pool) == 1:
        return pool[0], "single_machine"
    text = row.label or ""
    hits: dict = {}
    for machine in pool:
        for value in (machine.serial, machine.model, machine.mso):
            if value and _contains_token(text, value):
                hits[id(machine)] = machine
    if len(hits) == 1:
        return next(iter(hits.values())), "token"
    if len(hits) > 1:
        return None, "ambiguous"
    return None, "unmatched"


def _build_weight_groups(tables: list, machines: list, candidate: dict,
                         result: MachineWorkbookResult,
                         layout: dict) -> list:
    pool = _ticket_machine_pool(machines, candidate)
    canonical_msos: dict = {}
    for machine in pool:
        if machine.mso:
            canonical_msos.setdefault(_fold(machine.mso), machine.mso)
    for value in _identity_values(candidate, "mso"):
        canonical_msos.setdefault(_fold(value), value)
    machine_msos = {_fold(m.mso) for m in pool if m.mso}
    identity_msos = {_fold(v) for v in _identity_values(candidate, "mso")}
    known_msos = machine_msos or identity_msos

    groups: list = []
    assigned: dict = {}
    for table in tables:
        if not table.valid or table.qty_invalid:
            # 任一行/TOTAL 数量非法：整表不参与重量组分配，避免把不完整的
            # 数量核对结果当作可用重量依据。
            continue
        for row in table.rows:
            if row.qty is None or row.net is None:
                continue
            if row.msos:
                unique_msos = []
                for value in row.msos:
                    if value not in unique_msos:
                        unique_msos.append(value)
                if len(unique_msos) != len(row.msos):
                    result.issues.append(_issue(
                        "WEIGHT_GROUP_INCOMPLETE",
                        f"重量组 {row.label!r} 的 MSO 清单重复，无法唯一分配",
                        sources=[row.source],
                        action="请核对箱单组的 MSO 清单"))
                    continue
                unknown = [value for value in unique_msos
                           if _fold(value) not in known_msos]
                if unknown:
                    result.issues.append(_issue(
                        "WEIGHT_GROUP_MSO_UNKNOWN",
                        f"重量组 {row.label!r} 的 MSO {unknown} 在本票机器"
                        "信息中不存在，未用于本票",
                        sources=[row.source],
                        action="请核对箱单组 MSO 与机器信息表一致性"))
                    continue
                unique_msos = [canonical_msos.get(_fold(value), value)
                               for value in unique_msos]
                if row.qty != len(unique_msos):
                    _check(layout, RULE_GROUP_QTY, "fail",
                           f"组 {row.label!r} 数量{row.qty} vs "
                           f"组内 MSO 数{len(unique_msos)}")
                    result.issues.append(_issue(
                        "WEIGHT_GROUP_QTY_MISMATCH",
                        f"箱单组 {row.label!r} 数量 {row.qty} 与组内 MSO 数 "
                        f"{len(unique_msos)} 不一致，未分配重量",
                        sources=[row.source],
                        action="请核对箱单组数量与 MSO 清单"))
                    continue
                _check(layout, RULE_GROUP_QTY, "pass",
                       f"组 {row.label!r} 数量{row.qty} = "
                       f"组内 MSO 数{len(unique_msos)}")
                group = WeightGroup(
                    msos=unique_msos, qty=row.qty, net_weight=row.net,
                    gross_weight=row.gross, source=row.source)
            else:
                machine, reason = _unique_weight_machine(row, pool)
                if machine is None:
                    reason_text = {
                        "no_machine": "本票机器清单为空",
                        "ambiguous": "描述命中多台机器",
                        "unmatched": "描述无法唯一指向本票任一机器",
                    }.get(reason, reason)
                    result.issues.append(_issue(
                        "WEIGHT_GROUP_AMBIGUOUS",
                        f"箱单重量行 {row.label.strip()!r} 无组内 MSO，"
                        f"且{reason_text}，无法唯一映射，未建立 WeightGroup，"
                        "不按行号或平均分配猜测",
                        sources=[row.source],
                        action="请补充箱单组的 MSO/序列号，或人工确认重量归属"))
                    continue
                if not machine.mso:
                    result.issues.append(_issue(
                        "MISSING_MSO",
                        f"箱单重量行 {row.label.strip()!r} 唯一指向的机器缺 "
                        "MSO，无法建立 WeightGroup",
                        sources=[row.source],
                        action="请补充该机器的 MSO"))
                    continue
                if row.qty != 1:
                    _check(layout, RULE_GROUP_QTY, "fail",
                           f"组 {row.label!r} 无 MSO 且数量{row.qty}≠1")
                    result.issues.append(_issue(
                        "WEIGHT_GROUP_QTY_MISMATCH",
                        f"箱单重量行 {row.label.strip()!r} 无组内 MSO，"
                        f"唯一映射到单台机器 {machine.mso}，但数量 "
                        f"{row.qty}≠1，未分配重量",
                        sources=[row.source],
                        action="请核对箱单组数量"))
                    continue
                _check(layout, RULE_WEIGHT_UNIQUE_MAP, "pass",
                       f"重量行 {row.label.strip()!r} 无组内 MSO，"
                       f"按来源机器清单唯一映射到 MSO {machine.mso}"
                       f"（{reason}）")
                _check(layout, RULE_GROUP_QTY, "pass",
                       f"组 {row.label!r} 唯一机器数量1")
                group = WeightGroup(
                    msos=[machine.mso], qty=1, net_weight=row.net,
                    gross_weight=row.gross, source=row.source)

            overlap = [value for value in group.msos
                       if _fold(value) in assigned]
            if overlap:
                previous = assigned[_fold(overlap[0])]
                same_table = (previous.source.filename == group.source.filename
                              and previous.source.sheet == group.source.sheet)
                identical = (
                    {_fold(v) for v in previous.msos}
                    == {_fold(v) for v in group.msos}
                    and previous.qty == group.qty
                    and previous.net_weight == group.net_weight
                    and previous.gross_weight == group.gross_weight)
                if same_table:
                    result.issues.append(_issue(
                        "WEIGHT_GROUP_DUPLICATE",
                        f"同一箱单表内 MSO {overlap} 出现在多个重量组中，"
                        "不能重复计量",
                        sources=[previous.source, group.source],
                        action="请核对箱单重量组"))
                elif identical:
                    _check(layout, RULE_WEIGHT_TOTAL, "skipped",
                           f"多个重量区给出相同重量组 MSO {overlap}，"
                           "保留确定性来源一次并显式提示")
                    result.issues.append(_issue(
                        "WEIGHT_GROUP_DUPLICATE",
                        f"多个重量区对 MSO {overlap} 给出相同重量，已保留"
                        "一个确定性来源，未叠加也未按文件顺序改写数值",
                        sources=[previous.source, group.source],
                        action="请确认是否存在重复箱单",
                        blocking=False))
                else:
                    result.issues.append(_issue(
                        "WEIGHT_GROUP_CONFLICT",
                        f"多个重量区对 MSO {overlap} 给出不同重量："
                        f"{previous.net_weight}/{previous.qty} vs "
                        f"{group.net_weight}/{group.qty}，未按文件顺序取舍",
                        sources=[previous.source, group.source],
                        action="请人工确认该机器所属的箱单重量区"))
                continue
            groups.append(group)
            for value in group.msos:
                assigned[_fold(value)] = group

    if groups:
        covered = {_fold(value) for group in groups for value in group.msos}
        for machine in pool:
            if machine.mso and _fold(machine.mso) not in covered:
                result.issues.append(_issue(
                    "WEIGHT_GROUP_MACHINE_MISSING",
                    f"机器 MSO {machine.mso} 在本票箱单中没有对应重量组",
                    sources=[machine.source] if machine.source else [],
                    record_key=machine.mso,
                    field_name="net_weight",
                    action="请补充该机器对应的箱单重量组"))
    return groups


def _ci_text_net_check(invoice_ws_list: list, tables: list,
                       result: MachineWorkbookResult, layout: dict) -> None:
    values = []
    for filename, sheet, ws in invoice_ws_list:
        net = _text_net_weight(ws)
        if net is None:
            continue
        if not net.is_finite() or (
                net != 0 and abs(net.adjusted()) > _VALUE_MAX_ADJUSTED):
            result.issues.append(_issue(
                "WEIGHT_MISMATCH",
                f"CI 文字 NET WEIGHT 不是受控有限数值：{filename}/{sheet}",
                sources=[Source(type="workbook", filename=filename,
                                sheet=sheet)],
                action="请核对 CI 与箱单净重"))
            return
        values.append((net, Source(
            type="workbook", filename=filename, sheet=sheet,
            detail=f"CI 文字 NET WEIGHT {net}")))
    if not values:
        return
    distinct = {value for value, _ in values}
    if len(distinct) > 1:
        result.issues.append(_issue(
            "WEIGHT_MISMATCH",
            f"多个发票页给出不同的文字 NET WEIGHT："
            f"{sorted(str(value) for value in distinct)}，未采用任何一侧",
            sources=[source for _, source in values],
            action="请核对 CI 与箱单净重"))
        return
    if not tables or any(
            (not table.valid) or (not table.complete)
            or table.qty_invalid for table in tables):
        _check(layout, "MACHINE_CI_TEXT_VS_PL_NET", "skipped",
               "存在不完整/数量非法的箱单明细，CI 文字净重未核对")
        return
    total = sum((table.sum_net for table in tables if table.valid), Decimal("0"))
    net = next(iter(distinct))
    ok = decimals_close(total, net, MAX_DP_WEIGHT)
    _check(layout, "MACHINE_CI_TEXT_VS_PL_NET",
           "pass" if ok else "fail",
           f"CI文字NET WEIGHT {net} vs 箱单净重合计 {total}")
    if not ok:
        result.issues.append(_issue(
            "WEIGHT_MISMATCH",
            f"CI 文字总净重 {net} 与箱单净重合计 {total} 不一致",
            sources=[source for _, source in values],
            action="请核对 CI 与箱单净重"))


# ---------------------------------------------------------------------------
# 发票号
# ---------------------------------------------------------------------------


def _scan_invoice_numbers(ws, filename: str) -> list:
    found = []
    rows = min(ws.max_row, _MAX_INVOICE_SCAN_ROWS)
    cols = min(ws.max_column, _MAX_INVOICE_SCAN_COLS)
    for row in range(1, rows + 1):
        for col in range(1, cols + 1):
            value = ws.cell(row, col).value
            if not isinstance(value, str) or "INVOICE" not in value.upper():
                continue
            match = INVOICE_NO_RE.search(value.replace("\n", " "))
            if match:
                found.append((match.group(1).strip(), Source(
                    type="workbook", filename=filename, sheet=ws.title,
                    cell=ws.cell(row, col).coordinate, row=row)))
    return found


def _resolve_invoice_no(entries: list, candidate: dict,
                        result: MachineWorkbookResult,
                        layout: dict) -> tuple:
    by_value: dict = {}
    for value, source in entries:
        text = normalize_identifier(value)
        if not text:
            continue
        by_value.setdefault(_fold(text), []).append((text, source))
    confirmed = _identity_values(candidate, "invoice_no")
    confirmed_unique = sorted({_fold(v): v for v in confirmed}.items())
    if len(by_value) > 1:
        result.issues.append(_issue(
            "INVOICE_NO_CONFLICT",
            f"本票多个发票页/文件中出现不同发票号："
            f"{sorted({value for values in by_value.values() for value, _ in values})}，"
            "未按文件顺序静默选取",
            sources=[source for values in by_value.values()
                     for _, source in values],
            action="请人工确认本票最终发票号"))
        return None, None
    if len(confirmed_unique) > 1:
        result.issues.append(_issue(
            "INVOICE_NO_CONFLICT",
            f"已确认候选包含多个不同的发票号："
            f"{sorted(confirmed)}，无法唯一确定",
            action="请人工确认本票最终发票号"))
        return None, None
    workbook_value = None
    workbook_source = None
    if by_value:
        values = next(iter(by_value.values()))
        workbook_value, workbook_source = values[0]
        if len(values) > 1:
            _check(layout, "MACHINE_CONFIRMED_INVOICE_NO", "pass",
                   f"多个区域发票号一致：{workbook_value}")
    if workbook_value and confirmed_unique:
        confirmed_value = confirmed_unique[0][1]
        if _fold(workbook_value) != _fold(confirmed_value):
            result.issues.append(_issue(
                "INVOICE_NO_CONFLICT",
                f"工作簿发票号 {workbook_value} 与已确认候选身份 "
                f"{confirmed_value} 不一致，未采用任何一侧",
                sources=[workbook_source] if workbook_source else [],
                action="请核对候选身份与发票号"))
            return None, None
        _check(layout, "MACHINE_CONFIRMED_INVOICE_NO", "pass",
               f"工作簿发票号 {workbook_value} 与已确认候选一致")
        return workbook_value, workbook_source
    if workbook_value:
        _check(layout, "MACHINE_CONFIRMED_INVOICE_NO", "pass",
               f"工作簿发票号 {workbook_value}（候选未提供发票号）")
        return workbook_value, workbook_source
    if confirmed_unique:
        value = confirmed_unique[0][1]
        _check(layout, "MACHINE_CONFIRMED_INVOICE_NO", "skipped",
               f"区域未定位到发票号，采用已确认候选身份 {value}，"
               "无直接单元格来源")
        return value, Source(type="user_input",
                             detail="已确认候选身份中的发票号")
    result.issues.append(_issue(
        "MISSING_INVOICE_NO",
        "本票工作簿与已确认候选均未提供发票号",
        action="请补充发票号"))
    return None, None


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def parse_confirmed_machine_sources(sources: list[dict], candidate: dict,
                                    mail_rows: list[dict],
                                    aliases=None) -> MachineWorkbookResult:
    """解析已确认的整机单票快照，返回 reconcile_machines 可直接消费的结果。

    ``aliases`` 仅为接口兼容保留：已确认区域由候选给出列定位，不再做
    全表别名猜测；不会用别名扩大解析范围。
    """
    result = MachineWorkbookResult()
    layout = result.layout
    if not isinstance(candidate, dict):
        result.issues.append(_issue(
            "CONFIRMED_CANDIDATE_INVALID",
            "已确认候选不是对象，拒绝解析"))
        return result
    kind = str(candidate.get("kind") or "")
    if kind and kind not in ("machine", "unknown"):
        result.issues.append(_issue(
            "CANDIDATE_KIND_MISMATCH",
            f"已确认候选业务类型为 {kind!r}，不能按整机发票/箱单解析",
            action="请按候选业务类型选择对应解析入口"))
        return result
    if not isinstance(sources, list):
        result.issues.append(_issue(
            "CONFIRMED_CANDIDATE_INVALID",
            "sources 不是列表，无法确定本票材料"))
        return result
    if mail_rows is None:
        mail_rows = []
    elif not isinstance(mail_rows, list):
        result.issues.append(_issue(
            "CONFIRMED_CANDIDATE_INVALID",
            "mail_rows 不是列表，无法核对正文行"))
        return result

    refs = _resolve_confirmed_sources(sources, candidate, result, layout)
    if not refs:
        return result

    loaded: dict = {}
    drafts: list = []
    tables: list = []
    invoice_entries: list = []
    invoice_ws_seen: set = set()
    machine_region_seen = False
    try:
        for ref in refs:
            if not ref.regions:
                continue
            try:
                loaded[ref.path] = load_workbook_pair(ref.path)
            except WorkbookDependencyError as exc:
                result.issues.append(_issue(
                    "WORKBOOK_DEPENDENCY",
                    f"附件 {ref.filename} 已收到，但运行环境缺少解析组件：{exc}",
                    sources=[Source(type="workbook", filename=ref.filename)],
                    action="附件无需重发；请联系维护人员安装缺失组件后重试"))
                continue
            except UnsupportedSourceError as exc:
                result.issues.append(_issue(
                    "UNSUPPORTED_FILE", str(exc),
                    sources=[Source(type="workbook", filename=ref.filename)],
                    action="请补充未加密、未损坏的 xlsx 或 xls 附件"))
                continue
            except OSError as exc:
                result.issues.append(_issue(
                    "CONFIRMED_SOURCE_UNREADABLE",
                    f"附件 {ref.filename} 无法读取：{exc}",
                    sources=[Source(type="workbook", filename=ref.filename)],
                    action="请重新发布本票快照后再执行"))
                continue

        serials = _identity_values(candidate, "serial")
        for ref in refs:
            pair = loaded.get(ref.path)
            if pair is None:
                continue
            wb = pair[0]
            for region in ref.regions:
                try:
                    ws = wb[region.sheet]
                except Exception:
                    result.issues.append(_issue(
                        "CONFIRMED_REGION_INVALID",
                        f"候选区域 {region.sheet} 在附件 {ref.filename} 中"
                        "不存在，未解析其他工作表",
                        sources=[Source(type="workbook",
                                        filename=ref.filename)]))
                    continue
                if region.role in MACHINE_REGION_ROLES:
                    machine_region_seen = True
                    drafts.extend(_parse_machine_region(
                        ws, region, ref, serials, result, layout))
                elif region.role == WEIGHT_REGION_ROLE:
                    tables.append(_parse_weight_region(
                        ws, region, ref, result, layout))
                key = (ref.path, region.sheet)
                if key in invoice_ws_seen:
                    continue
                if (region.role in MACHINE_REGION_ROLES
                        or normalize_header(region.sheet) in INVOICE_NAME_KEYS
                        or normalize_header(region.sheet) in PACKING_NAME_KEYS):
                    invoice_ws_seen.add(key)
                    invoice_entries.extend(
                        _scan_invoice_numbers(ws, ref.filename))

        if not machine_region_seen:
            result.issues.append(_issue(
                "MACHINE_TABLE_INVALID",
                "候选 source_refs 没有机器信息区域，无法逐机核对 PIN/"
                "engine/serial",
                action="请确认候选包含整机发票机器信息表区域"))
        elif not drafts:
            result.issues.append(_issue(
                "MACHINE_TABLE_INVALID",
                "机器信息区域没有解析到任何机器行",
                action="请核对发票机器信息表"))

        evidence = _collect_mail_evidence(
            candidate, mail_rows, drafts, result, layout)
        _fill_missing_msos(drafts, evidence, candidate, result, layout)
        machines = _merge_machine_drafts(drafts, result, layout)
        _canonicalise_identity(machines, candidate, layout)
        _validate_identity(machines, candidate, result, layout)
        _per_machine_checks(machines, result, layout)
        _check_mail_cross(evidence, machines, result, layout)

        weight_groups = _build_weight_groups(
            tables, machines, candidate, result, layout)
        result.weight_groups = weight_groups
        if not tables:
            result.issues.append(_issue(
                "WEIGHT_TABLE_INVALID",
                "候选 source_refs 没有箱单重量表区域，无法取得本票重量",
                action="请确认候选包含对应箱单重量表"))

        invoice_ws_list = []
        for ref in refs:
            pair = loaded.get(ref.path)
            if pair is None:
                continue
            for region in ref.regions:
                if region.role not in MACHINE_REGION_ROLES:
                    continue
                try:
                    ws = pair[0][region.sheet]
                except Exception:
                    continue
                invoice_ws_list.append((ref.filename, region.sheet, ws))
                break
        _ci_text_net_check(invoice_ws_list, tables, result, layout)
        result.invoice_no, result.invoice_source = _resolve_invoice_no(
            invoice_entries, candidate, result, layout)

        invoice_rows = []
        packing_rows = []
        for machine in machines:
            if not machine.mso:
                continue
            row = MachineRow(
                mso=machine.mso, model=machine.model,
                loader=machine.serial, pin=machine.pin,
                engine=machine.engine, source=machine.source)
            if normalize_header(machine.source.sheet) in PACKING_NAME_KEYS:
                packing_rows.append(row)
            else:
                invoice_rows.append(row)
        result.invoice_rows = invoice_rows
        result.packing_rows = packing_rows
        if not invoice_rows and not packing_rows:
            result.issues.append(_issue(
                "MACHINE_TABLE_INVALID",
                "没有任何机器可进入 reconcile（全部缺少 MSO）",
                action="请补充机器 MSO"))

        _check(layout, RULE_PRICE_SOURCE, "pass",
               "价格只来自本票确认正文行并经 reconcile 处理；未读取发票 "
               "U'PRICE/T'Price/合计金额作为逐机单价")
        _check(layout, RULE_DESTINATION, "pass",
               "未从发票收票地址、正文 region/ship_to_party 推断最终目的国；"
               "目的国由调用方确认参数提供")
    finally:
        closed = set()
        for pair in loaded.values():
            for wb in pair:
                if id(wb) in closed:
                    continue
                closed.add(id(wb))
                close = getattr(wb, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
    return result
