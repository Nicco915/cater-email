"""多票分票预览（第一阶段）：``inspect_sources`` 只读识别入口。

定位（plan/tasks/claude-multi-ticket-preview.md）：
- 输入一封邮件的全部本地工作簿附件与正文（HTML/文本业务表），输出候选票
  清单：每票的附件/表/区域/正文行来源、识别到的标识（发票/MSO/Order
  Code/序列号）、数量与重量、问题与待确认原因。只读：不生成模板、不写
  数据库、不创建任务、不联网；
- 一个 Excel 不必等于一票：同票 CI/PL 分文件按身份合并；相同字节附件
  按内容去重；同号内容冲突保留全部版本并明确报告，不吞掉任何一个版本；
  证据不足保留候选与问题，不猜测；
- 归并的证据强度分层（弱字段不得桥接合票）：明确的当前发票号是强证据；
  MSO/序列号/Order Code 是弱证据，只能把无发票号附件挂到唯一匹配的票；
  不同明确发票号的簇绝不因弱字段相同而归并；无发票号附件命中多票时保留
  歧义、独立成候选，不充当桥接节点；
- candidate_id 只依赖内容哈希与识别身份：附件改名/调序、重复预览结果
  稳定；发票号不是全局唯一业务键；
- 与 process_job 的关系：共享只读工作簿接口、语义列定位、别名与来源
  模型，但本入口做“证据呈现式”识别（记录事实与来源、冲突并列），不
  复用单票的选表暂停语义；process_job 单票契约行为不变。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .models import Issue, Source
from .numbertext import (MAX_DP_PRICE, MAX_DP_WEIGHT, decimal_to_text,
                         decimals_close, normalize_identifier, to_decimal)
from .parsing.headers import (alias_marker, column_letter, merged_aliases,
                              normalize_header, resolve_columns,
                              select_total_row)
from .parsing.machine_wb import (INVOICE_NAME_KEYS, INVOICE_NO_RE,
                                 MACHINE_FIELDS, PACKING_NAME_KEYS,
                                 WEIGHT_FIELDS, WEIGHT_REQUIRE,
                                 _extract_group_msos, _parse_machine_table,
                                 sniff_machine_workbook)
from .parsing.mail_html import (extract_mail_notes, mail_aliases,
                                parse_mail_html, parse_mail_text)
from .parsing.workbook import (SUPPORTED_SUFFIXES, UnsupportedSourceError,
                               WorkbookDependencyError, load_workbook_pair)

PREVIEW_SCHEMA_VERSION = 1
INSPECTOR_VERSION = "split-preview-v1"

# 预览阶段补充别名（只作用于本入口，不改变 process_job 的默认别名行为）：
# - SEM655D 箱单的 NET/GROSS WEIGHT (KG) 单数写法；
# - 邮件业务表的 INVOICE NO. 列（仅预览核对用，process_job 不读取）；
# - CI 机器信息表的 PIN NUMBER 写法（PIN 内嵌序列号一致性核对）。
_PREVIEW_ALIASES: dict[str, list[str]] = {
    "net": ["NET WEIGHT (KG)"],
    "gross": ["GROSS WEIGHT (KG)"],
    "invoice_no": ["INVOICE NO.", "INVOICE NO"],
    "pin": ["PIN NUMBER"],
}

# 序列号形态（如 S5515230）：仅用于工作簿机器信息区无序列号表头时的
# 取值提示，必须全表数据区唯一且经 PIN/正文交叉核对，不据此单独定论。
_SERIAL_HINT_RE = re.compile(r"^[A-Z]\d{6,8}$")
_PIN_EMBEDDED_SERIAL_RE = re.compile(r"[A-Z]\d{7}")

# 配件 Health Care Kit 结构表头键（规范化后）
_KIT_INVOICE_KEY = "healthcarekitno"
_KIT_APP_KEYS = ("ordercode", "mso")
_KIT_PACK_KEYS = ("kit", "packages")

_IDENTITY_FIELDS = ("invoice_no", "order_code", "mso", "serial")
_FACT_FIELDS = ("amount", "qty", "net_weight", "gross_weight",
                "packages", "sets", "tare_weight")
# 各业务类型应有的关键事实；缺失即 FACT_MISSING（候选不可直接执行）
_EXPECTED_FACTS = {
    "machine": ("amount", "qty", "net_weight", "gross_weight"),
    "parts": ("amount", "net_weight", "gross_weight", "packages", "sets"),
}

_MAX_SCAN_ROWS = 200          # 身份/合计扫描的行上限（局部区域）
_MAX_NOTE_SHEET_ROWS = 60     # BL/文本扫描的行上限


# ---------------------------------------------------------------------------
# 基础数据结构
# ---------------------------------------------------------------------------


@dataclass
class FactEntry:
    """单个事实取值及其来源。"""

    value: object
    source: Source


@dataclass
class SheetRegion:
    sheet: str
    role: str          # machine_table | machine_info_partial | weight_table |
                       # kit_invoice | kit_application | kit_packing | bl |
                       # auxiliary | unrecognized
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"sheet": self.sheet, "role": self.role, **self.detail}


@dataclass
class WorkbookProfile:
    """一个唯一内容（按字节哈希去重）的工作簿附件的识别结果。"""

    sha256: str
    path: str
    filenames: list = field(default_factory=list)
    status: str = "ok"        # ok | not_workbook | unreadable | empty
    error: Optional[str] = None
    error_kind: Optional[str] = None   # dependency | format | os | suffix
    kind_hint: Optional[str] = None    # machine | parts
    identity: dict = field(default_factory=dict)   # field -> list[FactEntry]
    facts: dict = field(default_factory=dict)      # field -> list[FactEntry]
    regions: list = field(default_factory=list)    # list[SheetRegion]
    weight_rows: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    def add_identity(self, name: str, value, source: Source) -> None:
        self.identity.setdefault(name, []).append(FactEntry(value, source))

    def add_fact(self, name: str, value, source: Source) -> None:
        self.facts.setdefault(name, []).append(FactEntry(value, source))


# ---------------------------------------------------------------------------
# 数值与文本工具
# ---------------------------------------------------------------------------


def _price_equal(a: Decimal, b: Decimal) -> bool:
    """金额相等：完全相同，或差异小于相对 1e-9（Excel 二进制尾差量级）。

    与 reconcile._price_equal 同一规则：真实价格差异（≥0.01）必然不等，
    绝不用精度容差掩盖真实差额。
    """
    if a == b:
        return True
    if a == 0 or b == 0:
        return False
    return abs(a - b) <= max(abs(a), abs(b)) * Decimal("1e-9")


def _values_agree(field_name: str, a, b) -> bool:
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        if field_name == "amount":
            return _price_equal(a, b)
        return decimals_close(a, b, max_dp=MAX_DP_WEIGHT)
    return str(a).strip() == str(b).strip()


def _display_value(field_name: str, value):
    if isinstance(value, Decimal):
        dp = MAX_DP_PRICE if field_name == "amount" else MAX_DP_WEIGHT
        return decimal_to_text(value, dp)
    return str(value).strip()


def _norm_key(value) -> str:
    return normalize_identifier(value)


def _issue(code: str, message: str, *, sources=None, record_key=None,
           field_name=None, action="", blocking=True) -> Issue:
    return Issue(code=code, record_key=record_key, field=field_name,
                 message=message, sources=list(sources or []),
                 required_action=action, blocking=blocking)


# ---------------------------------------------------------------------------
# 人工调整（阶段 2）：把未归属正文行/附件指派到唯一候选，或有理由排除。
# 调整由服务端持久化后随请求传入；核心只接受结构化指令并在重识别时校验，
# 绝不接受客户端直接提交候选事实/路径/owner。
# ---------------------------------------------------------------------------


def _row_fields_snapshot(row) -> dict:
    """正文行的精确字段快照（有界）：用于人工归属判断与建票快照固化。"""
    snapshot = {}
    for name, value in (row.fields or {}).items():
        if value in (None, ""):
            continue
        snapshot[str(name)] = str(value)[:200]
    return snapshot


def _normalize_assignments(raw, issues: list) -> dict:
    """规范化请求中的 assignments 为内部映射。

    返回 {"mail_rows": {(table_index, row): op}, "sources": {sha256: op}}；
    op = {"action": "assign"|"exclude", "candidate_id": str|None,
          "reason": str}。形态非法的条目转为阻断问题并丢弃。
    """
    result: dict[str, dict] = {"mail_rows": {}, "sources": {}}
    if not isinstance(raw, dict):
        if raw:
            issues.append(_issue(
                "ADJUSTMENT_INVALID", "assignments 必须是对象，已忽略",
                action="请通过管理界面提交结构化调整"))
        return result

    def _op(entry, key_desc):
        if not isinstance(entry, dict):
            return None
        action = entry.get("action")
        if action not in ("assign", "exclude"):
            return None
        candidate_id = entry.get("candidate_id")
        reason = str(entry.get("reason") or "").strip()
        if action == "assign" and not (candidate_id
                                       and str(candidate_id).startswith("cand-")):
            return None
        if action == "exclude" and not reason:
            issues.append(_issue(
                "ADJUSTMENT_INVALID",
                f"排除 {key_desc} 必须填写理由",
                action="请为排除给出可审计的理由"))
            return None
        return {"action": action,
                "candidate_id": str(candidate_id) if candidate_id else None,
                "reason": reason}

    for entry in raw.get("mail_rows") or []:
        try:
            key = (int(entry.get("table_index")), int(entry.get("row")))
        except (TypeError, ValueError, AttributeError):
            issues.append(_issue(
                "ADJUSTMENT_INVALID", f"正文行调整缺少有效定位：{entry!r}",
                action="请通过管理界面提交结构化调整"))
            continue
        op = _op(entry, f"正文行（表 {key[0]} 第 {key[1]} 行）")
        if op is None:
            issues.append(_issue(
                "ADJUSTMENT_INVALID", f"正文行调整无效：{entry!r}",
                action="请通过管理界面提交结构化调整"))
            continue
        result["mail_rows"][key] = op
    for entry in raw.get("sources") or []:
        if not isinstance(entry, dict):
            issues.append(_issue(
                "ADJUSTMENT_INVALID", f"附件调整无效：{entry!r}",
                action="请通过管理界面提交结构化调整"))
            continue
        sha = str(entry.get("sha256") or "").strip().lower()
        op = _op(entry, f"附件 {sha[:12] or '?'}")
        if not sha or op is None:
            issues.append(_issue(
                "ADJUSTMENT_INVALID", f"附件调整无效：{entry!r}",
                action="请通过管理界面提交结构化调整"))
            continue
        result["sources"][sha] = op
    return result


# ---------------------------------------------------------------------------
# 工作簿区域识别：整机
# ---------------------------------------------------------------------------


def _scan_invoice_nos(ws, filename: str, profile: WorkbookProfile,
                      max_row: int = 16) -> None:
    """前若干行内的 INVOICE NO.: X 文本（每 sheet 取首个命中）。"""
    for r in range(1, min(ws.max_row, max_row) + 1):
        for c in range(1, min(ws.max_column, 16) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "INVOICE" in v.upper():
                m = INVOICE_NO_RE.search(v.replace("\n", " "))
                if m:
                    profile.add_identity(
                        "invoice_no", m.group(1).strip(),
                        Source(type="workbook", filename=filename,
                               sheet=ws.title, cell=ws.cell(r, c).coordinate))
                    return


def _scan_machine_amount(ws, filename: str, profile: WorkbookProfile) -> None:
    """TOTAL AMOUNT 行的最右数值单元格 → amount 事实（每 sheet 至多一条）。"""
    for r in range(1, min(ws.max_row, _MAX_SCAN_ROWS) + 1):
        anchor = False
        for c in range(1, min(ws.max_column, 4) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "TOTAL" in v.upper() and "AMOUNT" in v.upper():
                anchor = True
                break
        if not anchor:
            continue
        found = None
        for c in range(1, ws.max_column + 1):
            d = to_decimal(ws.cell(r, c).value)
            if d is not None:
                found = (d, c)
        if found is not None:
            value, col = found
            profile.add_fact("amount", value,
                             Source(type="workbook", filename=filename,
                                    sheet=ws.title,
                                    cell=f"{column_letter(col)}{r}", row=r))
        return


def _weight_resolution(ws, aliases):
    """重量表语义列定位（供探测与正式提取共用）。"""
    return resolve_columns(ws, WEIGHT_FIELDS, aliases, require=WEIGHT_REQUIRE)


def _machine_weight_table(ws, filename: str, aliases,
                          profile: WorkbookProfile, *,
                          strict: bool = True) -> bool:
    """整机箱单重量表：逐行数量/净重/毛重事实 + TOTAL 行交叉核对。

    strict=True（命名箱单表）：结构不完整登记 WEIGHT_TABLE_INVALID；
    strict=False（回退探测）：只有结构完整才采用，否则静默跳过（由调用方
    在多个候选间做唯一性判定）。返回是否采用了该表的重量结构。
    """
    resolution = _weight_resolution(ws, aliases)
    if resolution.header_row == 0:
        return False
    if resolution.multiple_regions or resolution.ambiguous or resolution.missing:
        if strict:
            profile.issues.append(_issue(
                "WEIGHT_TABLE_INVALID",
                f"工作表 {ws.title} 的重量表结构校验未通过"
                f"（缺失={resolution.missing} 多候选={resolution.ambiguous} "
                f"多表区={resolution.multiple_regions}），未提取重量事实",
                sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
                action="请确认重量表（QTY/NET WEIGHT 等表头）完整且唯一"))
        return False
    cols = resolution.columns
    first_row = (resolution.subheader_row or resolution.header_row) + 1
    total_row, total_error = select_total_row(
        ws, resolution.subheader_row or resolution.header_row,
        [cols.get("qty"), cols.get("net"), cols.get("gross")],
        anchor_cols=(1, cols["description"]))
    if total_error == "multiple":
        if strict:
            profile.issues.append(_issue(
                "MULTIPLE_TOTAL_ROWS",
                f"工作表 {ws.title} 表头下方存在多张同属一个表块的合计行，"
                "无法确定 TOTAL 锚点",
                sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
                action="请确认本票唯一 TOTAL 行后重发"))
        return False
    last_row = (total_row - 1) if total_row else ws.max_row
    profile.regions.append(SheetRegion(
        sheet=ws.title, role="weight_table",
        detail={"header_row": resolution.header_row,
                "subheader_row": resolution.subheader_row,
                "columns": {k: column_letter(v) for k, v in sorted(cols.items())},
                "total_row": total_row}))
    sum_qty = Decimal("0")
    sum_net = Decimal("0")
    n_rows = 0
    for r in range(first_row, last_row + 1):
        label = ws.cell(r, cols["description"]).value
        if label in (None, ""):
            continue
        qty = to_decimal(ws.cell(r, cols["qty"]).value)
        net = to_decimal(ws.cell(r, cols["net"]).value)
        gross = (to_decimal(ws.cell(r, cols["gross"]).value)
                 if "gross" in cols else None)
        msos = _extract_group_msos(str(label)) or []
        src = Source(type="workbook", filename=filename, sheet=ws.title,
                     cell=f"{column_letter(cols['description'])}{r}", row=r)
        profile.weight_rows.append({
            "description": " ".join(str(label).split())[:80],
            "msos": msos, "qty": str(qty) if qty is not None else None,
            "net": str(net) if net is not None else None,
            "gross": str(gross) if gross is not None else None,
            "source": src.to_dict(),
        })
        if qty is None or net is None:
            profile.issues.append(_issue(
                "WEIGHT_ROW_INCOMPLETE",
                f"重量行缺少数量或净重：{str(label).strip()!r}",
                sources=[src],
                action="请补齐箱单行的数量与净重"))
            continue
        sum_qty += qty
        sum_net += net
        n_rows += 1
    if total_row is not None:
        t_qty = to_decimal(ws.cell(total_row, cols["qty"]).value)
        t_net = to_decimal(ws.cell(total_row, cols["net"]).value)
        t_gross = (to_decimal(ws.cell(total_row, cols["gross"]).value)
                   if "gross" in cols else None)
        tsrc = Source(type="workbook", filename=filename, sheet=ws.title,
                      cell=f"{column_letter(cols['description'])}{total_row}",
                      row=total_row)
        if t_qty is not None:
            profile.add_fact("qty", t_qty, tsrc)
        if t_net is not None:
            profile.add_fact("net_weight", t_net, tsrc)
        if t_gross is not None:
            profile.add_fact("gross_weight", t_gross, tsrc)
        if n_rows and t_qty is not None and t_net is not None:
            ok = (t_qty == sum_qty
                  and decimals_close(sum_net, t_net, max_dp=MAX_DP_WEIGHT))
            profile.checks.append({
                "rule": "PREVIEW_WEIGHT_TOTAL", "result": "pass" if ok else "fail",
                "detail": f"明细合计 数量{sum_qty}/净重{sum_net} vs "
                          f"TOTAL行 数量{t_qty}/净重{t_net}"})
            if not ok:
                profile.issues.append(_issue(
                    "WEIGHT_TOTAL_MISMATCH",
                    f"箱单明细合计（数量{sum_qty} 净重{sum_net}）与 TOTAL 行"
                    f"（数量{t_qty} 净重{t_net}）不一致",
                    sources=[tsrc],
                    action="请核对箱单明细与 TOTAL 行"))
    elif n_rows:
        profile.add_fact("qty", sum_qty,
                         Source(type="workbook", filename=filename,
                                sheet=ws.title,
                                detail=f"{n_rows} 个重量行合计（无 TOTAL 行）"))
        profile.add_fact("net_weight", sum_net,
                         Source(type="workbook", filename=filename,
                                sheet=ws.title,
                                detail=f"{n_rows} 个重量行合计（无 TOTAL 行）"))
    return True


def _machine_info_partial(ws, filename: str, aliases,
                          profile: WorkbookProfile) -> None:
    """无 MSO 列的机器信息区（如旧 CI 布局）：只提取机型与序列号提示。

    序列号提示 = 数据区内全表唯一、形如 S5515230 的值；不唯一则报问题，
    绝不猜测。PIN 列可定位时抽取 PIN 内嵌序列号做一致性核对。
    """
    resolution = resolve_columns(ws, MACHINE_FIELDS, aliases, require=["model"])
    if (resolution.header_row == 0 or resolution.multiple_regions
            or resolution.ambiguous or "model" not in resolution.columns
            or "mso" in resolution.columns):
        return
    cols = resolution.columns
    first_row = (resolution.subheader_row or resolution.header_row) + 1
    rows = []
    for r in range(first_row, min(ws.max_row, first_row + 100) + 1):
        values = {c: ws.cell(r, c).value for c in range(1, ws.max_column + 1)}
        if all(v in (None, "") or (isinstance(v, str) and not v.strip())
               for v in values.values()):
            break
        model = ws.cell(r, cols["model"]).value
        if model in (None, ""):
            continue
        rows.append((r, values))
    if not rows:
        return
    serial_hints: dict[str, Source] = {}
    for r, values in rows:
        for c, v in values.items():
            if not isinstance(v, str):
                continue
            text = v.strip()
            if not _SERIAL_HINT_RE.match(text):
                continue
            if text not in serial_hints:
                serial_hints[text] = Source(
                    type="workbook", filename=filename, sheet=ws.title,
                    cell=f"{column_letter(c)}{r}", row=r)
    models = []
    for r, _values in rows:
        model = str(ws.cell(r, cols["model"]).value).strip()
        src = Source(type="workbook", filename=filename, sheet=ws.title,
                     cell=f"{column_letter(cols['model'])}{r}", row=r)
        models.append({"model": model, "source": src.to_dict()})
    if len(serial_hints) > 1:
        profile.issues.append(_issue(
            "SERIAL_HINT_AMBIGUOUS",
            f"工作表 {ws.title} 机器信息区存在多个序列号形态的值 "
            f"{sorted(serial_hints)}，无法唯一确定序列号",
            sources=list(serial_hints.values()),
            action="请确认机器的序列号取值"))
    elif serial_hints:
        serial, src = next(iter(serial_hints.items()))
        profile.add_identity("serial", serial, src)
        pin_col = cols.get("pin")
        if pin_col:
            for r, _values in rows:
                pin = ws.cell(r, pin_col).value
                if not pin:
                    continue
                m = _PIN_EMBEDDED_SERIAL_RE.search(str(pin))
                if m:
                    ok = m.group(0) == serial
                    profile.checks.append({
                        "rule": "PREVIEW_PIN_SERIAL",
                        "result": "pass" if ok else "fail",
                        "detail": f"PIN 内嵌序列号 {m.group(0)} vs 区域序列号 {serial}"})
                    if not ok:
                        profile.issues.append(_issue(
                            "SERIAL_CONFLICT",
                            f"PIN {pin} 内嵌序列号 {m.group(0)} 与区域序列号 "
                            f"{serial} 不一致",
                            sources=[src, Source(
                                type="workbook", filename=filename,
                                sheet=ws.title,
                                cell=f"{column_letter(pin_col)}{r}", row=r)],
                            action="请核对机器序列号与 PIN"))
                    break
    profile.regions.append(SheetRegion(
        sheet=ws.title, role="machine_info_partial",
        detail={"header_row": resolution.header_row,
                "columns": {k: column_letter(v) for k, v in sorted(cols.items())},
                "rows": len(models), "models": models,
                "note": "无 MSO 列；MSO 需由正文业务行按序列号关联补齐"}))


def _inspect_machine_sheet(ws, filename: str, aliases,
                           profile: WorkbookProfile) -> None:
    rows, layout, error = _parse_machine_table(ws, filename, aliases)
    if error is None:
        profile.regions.append(SheetRegion(
            sheet=ws.title, role="machine_table",
            detail={"header_row": layout["header_row"],
                    "columns": layout["columns"],
                    "data_first_row": layout["data_first_row"],
                    "data_last_row": layout["data_last_row"]}))
        for row in rows:
            profile.add_identity("mso", row.mso, row.source)
            if row.loader:
                profile.add_identity("serial", row.loader, row.source)
        return
    if error in ("ambiguous", "incomplete", "second_header"):
        profile.issues.append(_issue(
            "MACHINE_TABLE_INVALID",
            f"工作表 {ws.title} 的机器信息表结构校验未通过（{error}），不能猜测放行",
            sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
            action="请确认机器信息表（MODEL/MSO# 等表头）完整且唯一"))
        return
    # no_machine_table / empty：尝试无 MSO 的部分机器信息区
    _machine_info_partial(ws, filename, aliases, profile)


# ---------------------------------------------------------------------------
# 工作簿区域识别：配件 Health Care Kit
# ---------------------------------------------------------------------------


def _header_cells(ws, row: int) -> dict:
    """一行内 规范化表头文本 -> 列号（重复表头取首列）。"""
    found: dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        key = normalize_header(ws.cell(row, c).value)
        if key and key not in found:
            found[key] = c
    return found


def _find_kit_header(ws, required: tuple) -> Optional[tuple]:
    """定位覆盖全部 required 规范化键的表头行；返回 (row, cells) 或 None。"""
    for r in range(1, min(ws.max_row, _MAX_NOTE_SHEET_ROWS) + 1):
        cells = _header_cells(ws, r)
        if all(key in cells for key in required):
            return r, cells
    return None


def _first_total_row(ws, start_row: int) -> Optional[int]:
    """col1 文本规范化后恰为 total 的首行（配件套件表 TOTAL 锚点）。"""
    for r in range(start_row, min(ws.max_row, start_row + _MAX_SCAN_ROWS) + 1):
        if normalize_header(ws.cell(r, 1).value) == "total":
            return r
    return None


def _kit_invoice_sheet(ws, filename: str, profile: WorkbookProfile) -> bool:
    """Health Care Kit 发票页：T'Price 合计金额、总净重、辅助列差异。"""
    found = _find_kit_header(ws, (_KIT_INVOICE_KEY,))
    if found is None:
        return False
    header_row, cells = found
    sub = _header_cells(ws, header_row + 1) if header_row + 1 <= ws.max_row else {}
    price_col = cells.get("tprice")
    net_col = sub.get("总净重")
    excluded = {c for c in (price_col, net_col, cells.get("uprice"),
                            cells.get("totalqty"), cells.get("qtyset"),
                            cells.get("setqty")) if c}
    total_row = None
    for r in range(header_row + 1, min(ws.max_row, header_row + _MAX_SCAN_ROWS) + 1):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and "TOTAL" in v.upper() and "AMOUNT" in v.upper():
            total_row = r
            break
    region_detail = {"header_row": header_row, "total_row": total_row,
                     "columns": {"total_price": column_letter(price_col) if price_col else None,
                                 "net_weight": column_letter(net_col) if net_col else None}}
    profile.regions.append(SheetRegion(
        sheet=ws.title, role="kit_invoice", detail=region_detail))
    if total_row is None:
        profile.issues.append(_issue(
            "INVOICE_TOTAL_NOT_FOUND",
            f"发票页 {ws.title} 未找到 TOTAL AMOUNT 合计行",
            sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
            action="请确认发票页包含 TOTAL AMOUNT 合计行"))
        return True
    amount = to_decimal(ws.cell(total_row, price_col).value) if price_col else None
    if amount is not None:
        profile.add_fact("amount", amount,
                         Source(type="workbook", filename=filename,
                                sheet=ws.title,
                                cell=f"{column_letter(price_col)}{total_row}",
                                row=total_row))
    if net_col:
        net = to_decimal(ws.cell(total_row, net_col).value)
        if net is not None:
            profile.add_fact("net_weight", net,
                             Source(type="workbook", filename=filename,
                                    sheet=ws.title,
                                    cell=f"{column_letter(net_col)}{total_row}",
                                    row=total_row))
    # 合计行上已知列以外的数值单元格：辅助金额列，差异必须可见，绝不覆盖
    for c in range(1, ws.max_column + 1):
        if c in excluded:
            continue
        d = to_decimal(ws.cell(total_row, c).value)
        if d is None:
            continue
        aux_src = Source(type="workbook", filename=filename, sheet=ws.title,
                         cell=f"{column_letter(c)}{total_row}", row=total_row)
        if amount is None or not _price_equal(d, amount):
            profile.issues.append(_issue(
                "AMOUNT_VARIANTS",
                f"发票页 {ws.title} 合计行存在与 T'Price 合计 "
                f"{_display_value('amount', amount) if amount is not None else '（缺）'} "
                f"不一致的辅助金额列：{column_letter(c)}{total_row}="
                f"{_display_value('amount', d)}，未判定该列业务含义，未采用",
                sources=[aux_src],
                action="请人工核对辅助金额列含义与行级单价、精度、公式"))
    return True


def _kit_application_table(ws, filename: str, profile: WorkbookProfile) -> bool:
    """套件应用表（KIT/PO/Order Code/MSO/...）：Order Code、MSO、序列号身份。"""
    found = _find_kit_header(ws, _KIT_APP_KEYS)
    if found is None:
        return False
    header_row, cells = found
    oc_col = cells.get("ordercode")
    mso_col = cells.get("mso")
    serial_col = cells.get("machineserialno")
    n_rows = 0
    residue_rows: list[int] = []
    last_row = min(ws.max_row, header_row + _MAX_SCAN_ROWS)
    for r in range(header_row + 1, last_row + 1):
        src = Source(type="workbook", filename=filename, sheet=ws.title, row=r)
        oc = ws.cell(r, oc_col).value if oc_col else None
        mso = ws.cell(r, mso_col).value if mso_col else None
        serial = ws.cell(r, serial_col).value if serial_col else None
        filled = [v for v in (oc, mso, serial) if v not in (None, "")]
        if not filled:
            if n_rows:
                break
            continue
        if len(filled) < 2:
            # 业务区域边界：有效应用行至少含两项身份字段（真实行
            # Order Code/MSO/序列号齐全）。仅剩单项的是模板残留或页脚
            # （如复制模板后只清掉其他列、残留 Order Code），不构成身份；
            # 数据段开始后遇到即视为表结束，其后的行一概不采用。
            residue_rows.append(r)
            if n_rows:
                rr = r + 1
                while rr <= last_row:
                    tail = [ws.cell(rr, c).value
                            for c in (oc_col, mso_col, serial_col) if c]
                    if sum(v not in (None, "") for v in tail) == 1:
                        residue_rows.append(rr)
                        rr += 1
                    else:
                        break
                break
            continue
        n_rows += 1
        if oc not in (None, ""):
            profile.add_identity("order_code", str(oc).strip(), src)
        if mso not in (None, ""):
            profile.add_identity("mso", str(mso).strip(), src)
        if serial not in (None, ""):
            profile.add_identity("serial", str(serial).strip(), src)
    if residue_rows:
        vals = sorted({str(ws.cell(r, c).value).strip()
                       for r in residue_rows
                       for c in (oc_col, mso_col, serial_col) if c
                       and ws.cell(r, c).value not in (None, "")})
        profile.issues.append(_issue(
            "TEMPLATE_RESIDUE_ROWS",
            f"应用表 {ws.title} 第 {residue_rows[0]}"
            f"{'-' + str(residue_rows[-1]) if len(residue_rows) > 1 else ''} 行"
            f"仅有单项身份字段（{vals}），疑似模板残留/页脚，未作为本票身份",
            sources=[Source(type="workbook", filename=filename, sheet=ws.title,
                            row=residue_rows[0])],
            action="请确认这些行为模板残留而非有效业务行",
            blocking=False))
    profile.regions.append(SheetRegion(
        sheet=ws.title, role="kit_application",
        detail={"header_row": header_row, "rows": n_rows,
                "residue_rows": residue_rows,
                "columns": {"order_code": column_letter(oc_col) if oc_col else None,
                            "mso": column_letter(mso_col) if mso_col else None,
                            "serial": column_letter(serial_col) if serial_col else None}}))
    return True


def _kit_packing_table(ws, filename: str, profile: WorkbookProfile) -> bool:
    """套件箱单表：首段套件行 TOTAL 的套数/包装件数/毛重；皮重事实；
    后段零件明细 TOTAL 只用于净重交叉核对，绝不当作整票毛重。"""
    found = _find_kit_header(ws, _KIT_PACK_KEYS)
    if found is None:
        return False
    header_row, cells = found
    sets_col = cells.get("qty")
    pkg_col = cells.get("packages")
    gross_col = cells.get("weight")
    total_row = _first_total_row(ws, header_row + 1)
    profile.regions.append(SheetRegion(
        sheet=ws.title, role="kit_packing",
        detail={"header_row": header_row, "total_row": total_row,
                "columns": {"sets": column_letter(sets_col) if sets_col else None,
                            "packages": column_letter(pkg_col) if pkg_col else None,
                            "gross_weight": column_letter(gross_col) if gross_col else None}}))
    if total_row is None:
        profile.issues.append(_issue(
            "PACKING_TOTAL_NOT_FOUND",
            f"套件箱单 {ws.title} 未找到套件段 TOTAL 行",
            sources=[Source(type="workbook", filename=filename, sheet=ws.title)],
            action="请确认箱单套件段存在唯一 TOTAL 行"))
    else:
        tsrc = Source(type="workbook", filename=filename, sheet=ws.title,
                      cell=f"A{total_row}", row=total_row)
        for name, col in (("sets", sets_col), ("packages", pkg_col),
                          ("gross_weight", gross_col)):
            if not col:
                continue
            d = to_decimal(ws.cell(total_row, col).value)
            if d is not None:
                profile.add_fact(name, d, Source(
                    type="workbook", filename=filename, sheet=ws.title,
                    cell=f"{column_letter(col)}{total_row}", row=total_row))
        if gross_col and (sets_col is None or pkg_col is None):
            profile.issues.append(_issue(
                "PACKING_TOTALS_INCOMPLETE",
                f"套件箱单 {ws.title} 的 TOTAL 行列定位不完整"
                f"（套数列={column_letter(sets_col) if sets_col else None} "
                f"件数列={column_letter(pkg_col) if pkg_col else None}）",
                sources=[tsrc],
                action="请确认箱单表头包含 QTY/Packages/Weight 列"))
    # 皮重行：已含在箱单毛重合计内（2026-11-10 研究确认），仅作事实展示
    for r in range(header_row + 1, (total_row or ws.max_row) + 1):
        if normalize_header(ws.cell(r, 1).value) == "tareweight":
            if gross_col:
                tare = to_decimal(ws.cell(r, gross_col).value)
                if tare is not None:
                    profile.add_fact("tare_weight", tare, Source(
                        type="workbook", filename=filename, sheet=ws.title,
                        cell=f"{column_letter(gross_col)}{r}", row=r))
                    profile.checks.append({
                        "rule": "PREVIEW_TARE_INCLUDED", "result": "pass",
                        "detail": f"皮重 {tare} kg 已包含在箱单毛重合计内，不得再加"})
            break
    # 后段零件明细：独立表头 + 独立 TOTAL，仅用于净重交叉核对
    if total_row is not None:
        detail = None
        for r in range(total_row + 1, min(ws.max_row, total_row + 40) + 1):
            cells = _header_cells(ws, r)
            if "netweight" in cells:
                detail = (r, cells["netweight"])
                break
        if detail is not None:
            d_header, net_col = detail
            d_total = _first_total_row(ws, d_header + 1)
            if d_total is not None:
                d_net = to_decimal(ws.cell(d_total, net_col).value)
                invoice_nets = [e.value for e in profile.facts.get("net_weight", [])
                                if isinstance(e.value, Decimal)]
                if d_net is not None and invoice_nets:
                    ok = all(decimals_close(d_net, n, max_dp=MAX_DP_WEIGHT)
                             for n in invoice_nets)
                    profile.checks.append({
                        "rule": "PREVIEW_KIT_NET_CROSSCHECK",
                        "result": "pass" if ok else "fail",
                        "detail": f"零件明细净重合计 {d_net} vs 发票总净重 "
                                  f"{invoice_nets[0]}"})
                    if not ok:
                        profile.issues.append(_issue(
                            "WEIGHT_MISMATCH",
                            f"零件明细净重合计 {d_net} 与发票总净重 "
                            f"{invoice_nets[0]} 不一致",
                            sources=[Source(type="workbook", filename=filename,
                                            sheet=ws.title,
                                            cell=f"{column_letter(net_col)}{d_total}",
                                            row=d_total)],
                            action="请核对发票总净重与箱单零件明细合计"))
    return True


def _is_bl_sheet(ws) -> bool:
    if normalize_header(ws.title) == "bl":
        return True
    for r in range(1, min(ws.max_row, 12) + 1):
        for c in range(1, min(ws.max_column, 8) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "BILL" in v.upper() and "LADING" in v.upper():
                return True
    return False


def _scan_bl_sheet(ws, filename: str, profile: WorkbookProfile) -> None:
    """BL 页：残留的 INVOICE NO. 只用于旧票号核对，绝不作为当前发票号来源。"""
    profile.regions.append(SheetRegion(sheet=ws.title, role="bl", detail={}))
    seen: dict[str, Source] = {}
    for r in range(1, min(ws.max_row, _MAX_NOTE_SHEET_ROWS) + 1):
        for c in range(1, min(ws.max_column, 16) + 1):
            v = ws.cell(r, c).value
            if not isinstance(v, str) or "INVOICE" not in v.upper():
                continue
            m = INVOICE_NO_RE.search(v.replace("\n", " "))
            if m:
                seen.setdefault(m.group(1).strip(), Source(
                    type="workbook", filename=filename, sheet=ws.title,
                    cell=ws.cell(r, c).coordinate))
    current = {str(e.value).strip() for e in profile.identity.get("invoice_no", [])}
    for value, src in seen.items():
        if current and value not in current:
            profile.issues.append(_issue(
                "STALE_BL_INVOICE",
                f"BL 页 {ws.title} 出现与本次发票 {sorted(current)} 不同的票号 "
                f"{value}（疑似旧模板残留），未作为当前发票号来源",
                sources=[src],
                action="请人工确认 BL 页票号为旧模板残留"))


# ---------------------------------------------------------------------------
# 单工作簿识别
# ---------------------------------------------------------------------------


def _profile_workbook(path: str, filename: str, aliases) -> WorkbookProfile:
    p = Path(path)
    profile = WorkbookProfile(sha256="", path=str(p), filenames=[filename])
    try:
        data = p.read_bytes()
    except OSError as exc:
        profile.status = "unreadable"
        profile.error = f"文件无法读取：{type(exc).__name__}"
        profile.error_kind = "os"
        return profile
    profile.sha256 = hashlib.sha256(data).hexdigest()
    if p.suffix.lower() not in SUPPORTED_SUFFIXES:
        profile.status = "not_workbook"
        profile.error_kind = "suffix"
        return profile
    try:
        wb_values, _wb_formula = load_workbook_pair(str(p))
    except WorkbookDependencyError as exc:
        profile.status = "unreadable"
        profile.error = f"运行环境缺少解析组件：{exc}"
        profile.error_kind = "dependency"
        return profile
    except UnsupportedSourceError as exc:
        profile.status = "unreadable"
        profile.error = str(exc)
        profile.error_kind = "format"
        return profile
    except Exception as exc:  # 未知读取错误：保留有限信息
        profile.status = "unreadable"
        profile.error = f"读取失败：{type(exc).__name__}"
        profile.error_kind = "format"
        return profile

    names = list(wb_values.sheetnames)
    # 先识别配件 Health Care Kit 结构（其箱单后段零件表与整机重量表
    # 结构相似，必须先分流，避免误当整机重量表）
    kit_sheets = []
    for name in names:
        ws = wb_values[name]
        if (_find_kit_header(ws, (_KIT_INVOICE_KEY,))
                or _find_kit_header(ws, _KIT_APP_KEYS)
                or _find_kit_header(ws, _KIT_PACK_KEYS)):
            kit_sheets.append(name)
    machine_sheets = []
    if not kit_sheets:
        if sniff_machine_workbook(str(p), aliases):
            machine_sheets = names  # 具体区域在下方逐 sheet 定位
        else:
            # 名称辅助：Invoice/CI/Packing list 即使结构不完整也按整机路径
            # 检查（结构问题要可见，不能静默归入 unrecognized）
            if any(normalize_header(n) in INVOICE_NAME_KEYS | PACKING_NAME_KEYS
                   for n in names):
                machine_sheets = names

    if kit_sheets:
        profile.kind_hint = "parts"
        for name in names:
            ws = wb_values[name]
            if _is_bl_sheet(ws):
                _scan_bl_sheet(ws, filename, profile)
                continue
            handled = False
            if name in kit_sheets:
                if _kit_invoice_sheet(ws, filename, profile):
                    handled = True
                if _kit_packing_table(ws, filename, profile):
                    handled = True
                if _kit_application_table(ws, filename, profile):
                    handled = True
            if handled or name in kit_sheets:
                _scan_invoice_nos(ws, filename, profile)
            if not handled:
                role = ("auxiliary" if normalize_header(name)
                        in {"sheet1", "sheet2", "兼容性报表"} else "unrecognized")
                profile.regions.append(SheetRegion(sheet=name, role=role, detail={}))
    elif machine_sheets:
        profile.kind_hint = "machine"
        named_packing = [n for n in names
                         if normalize_header(n) in PACKING_NAME_KEYS]
        weight_done = False
        for name in names:
            ws = wb_values[name]
            if _is_bl_sheet(ws):
                _scan_bl_sheet(ws, filename, profile)
                continue
            before = len(profile.regions)
            _inspect_machine_sheet(ws, filename, aliases, profile)
            if name in named_packing:
                # 命名箱单表：严格提取，结构问题必须可见
                weight_done = _machine_weight_table(
                    ws, filename, aliases, profile, strict=True) or weight_done
            _scan_machine_amount(ws, filename, profile)
            _scan_invoice_nos(ws, filename, profile)
            if len(profile.regions) == before:
                role = ("auxiliary" if normalize_header(name)
                        in {"sheet1", "sheet2", "兼容性报表"} else "unrecognized")
                profile.regions.append(SheetRegion(sheet=name, role=role, detail={}))
        if not weight_done:
            # 无命名箱单表（如业务表被重命名）：其余 sheet 结构探测，
            # 唯一候选回退；多候选必须暂停，不猜测
            fallback = []
            for name in names:
                if name in named_packing:
                    continue
                ws = wb_values[name]
                if _is_bl_sheet(ws) or any(
                        r.sheet == name and r.role.startswith("machine")
                        for r in profile.regions):
                    continue
                resolution = _weight_resolution(ws, aliases)
                if (resolution.header_row and not resolution.missing
                        and not resolution.ambiguous
                        and not resolution.multiple_regions):
                    fallback.append(name)
            if len(fallback) > 1:
                profile.issues.append(_issue(
                    "AMBIGUOUS_WEIGHT_TABLE",
                    f"多个工作表存在箱单重量表：{fallback}，无法唯一选定",
                    sources=[Source(type="workbook", filename=filename)],
                    action="请确认应使用哪张工作表"))
            elif fallback:
                _machine_weight_table(wb_values[fallback[0]], filename,
                                      aliases, profile, strict=True)
    else:
        profile.status = "empty"
        profile.error = "未识别到整机或配件业务结构"

    if hasattr(wb_values, "close"):
        wb_values.close()
    return profile


# ---------------------------------------------------------------------------
# 候选票归并与交叉核对
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _merge_fact(name: str, entries: list, issues: list,
                candidate_label: str) -> Optional[dict]:
    """合并同一事实字段的全部取值：一致→单值多来源；冲突→并列 variants。

    返回 None 表示该字段无任何取值。冲突绝不吞掉任何版本。
    """
    if not entries:
        return None
    groups: list[list[FactEntry]] = []
    for entry in entries:
        for group in groups:
            if _values_agree(name, group[0].value, entry.value):
                group.append(entry)
                break
        else:
            groups.append([entry])
    if len(groups) == 1:
        raws = []
        for e in groups[0]:
            text = str(e.value)
            if text not in raws:
                raws.append(text)
        result = {"value": _display_value(name, groups[0][0].value),
                  "sources": [e.source.to_dict() for e in groups[0]],
                  "conflict": False}
        display = result["value"]
        if any(r != display for r in raws):
            result["raw"] = raws
        return result
    issues.append(_issue(
        "CANDIDATE_FACT_CONFLICT",
        f"候选 {candidate_label} 的 {name} 存在 "
        f"{len(groups)} 个不一致取值："
        + "；".join(f"{_display_value(name, g[0].value)}"
                    f"（{g[0].source.filename or g[0].source.type}"
                    f"{':' + g[0].source.sheet if g[0].source.sheet else ''}"
                    f"{'!' + g[0].source.cell if g[0].source.cell else ''}）"
                    for g in groups),
        field_name=name,
        sources=[e.source for g in groups for e in g],
        action="请人工确认应采用的内容版本；预览未采用任何一版"))
    return {"value": None, "conflict": True,
            "variants": [{"value": _display_value(name, g[0].value),
                          "raw": [str(e.value) for e in g],
                          "sources": [e.source.to_dict() for e in g]}
                         for g in groups]}


def _build_candidates(profiles: list, top_issues: list,
                      injections: dict | None = None) -> tuple[list, dict]:
    """按身份证据强度归并工作簿为候选票，弱字段不得桥接合票。

    证据分层：
    - 强证据：明确的当前发票号。同号（含同号不同内容的版本）归并到同一
      候选，内容冲突在事实层并列展示，绝不吞掉任何版本；
    - 弱证据：MSO/序列号/Order Code。仅允许把“无明确发票号的附件”挂到
      唯一匹配的票上；两个具有不同明确发票号的簇绝不因弱字段相同而归并，
      共享弱字段作为冲突问题保留可见；
    - 无发票号附件的弱字段同时命中多票 → 保留歧义：独立候选 + 阻断问题，
      不作为桥接节点归并任何一方。

    ``injections``：{sha256: {"candidate_id": str, "profile": profile}}，
    人工指派的“无身份附件 → 候选”映射（阶段 2）。返回
    (candidates, unresolved)，unresolved 为 {sha256: candidate_id}。
    """
    injected_sha = set(injections or ())
    indexed = [p for p in profiles
               if p.status == "ok" and p.sha256 not in injected_sha]
    uf = _UnionFind(len(indexed))
    for p in indexed:
        p._inv_values = sorted({str(e.value).strip()  # noqa: SLF001
                                for e in p.identity.get("invoice_no", [])
                                if str(e.value).strip()})
        weak = set()
        for kind, name in (("mso", "mso"), ("ser", "serial"),
                           ("oc", "order_code")):
            for e in p.identity.get(name, []):
                key = _norm_key(e.value)
                if key:
                    weak.add((kind, key))
        p._weak_keys = weak  # noqa: SLF001
        p._keys = [("inv", v) for v in p._inv_values] + sorted(weak)  # noqa: SLF001
    # 阶段 1：强证据归并（共享明确发票号；阶段 1 之后各簇发票号集合互不相交）
    inv_owner: dict[str, int] = {}
    for i, p in enumerate(indexed):
        for value in p._inv_values:  # noqa: SLF001
            if value in inv_owner:
                uf.union(i, inv_owner[value])
            else:
                inv_owner[value] = i
    stage1_root = [uf.find(i) for i in range(len(indexed))]
    stage1_inv: dict[int, set] = {}
    for i, p in enumerate(indexed):
        stage1_inv.setdefault(stage1_root[i], set()).update(p._inv_values)  # noqa: SLF001
    weak_owner: dict[tuple, list[int]] = {}
    for i, p in enumerate(indexed):
        for key in p._weak_keys:  # noqa: SLF001
            weak_owner.setdefault(key, []).append(i)

    def cluster_invoices(root: int) -> set:
        result: set = set()
        for j, q in enumerate(indexed):
            if uf.find(j) == root:
                result.update(q._inv_values)  # noqa: SLF001
        return result

    # 阶段 2：无明确发票号附件的归属判定。弱字段命中多票 → 歧义保留，
    # 不归并任何一方、不充当桥接节点；命中唯一票 → 挂到该票。
    ambiguous: set[int] = set()
    for i, p in enumerate(indexed):
        if p._inv_values or not p._weak_keys:  # noqa: SLF001
            continue
        matched = sorted({stage1_root[j]
                          for key in p._weak_keys  # noqa: SLF001
                          for j in weak_owner.get(key, [])
                          if stage1_inv.get(stage1_root[j])})
        if len(matched) > 1:
            ambiguous.add(i)
            invoices = sorted({v for root in matched for v in stage1_inv[root]})
            p.issues.append(_issue(
                "ATTACHMENT_MATCHES_MULTIPLE_TICKETS",
                f"附件 {sorted(p.filenames)} 无明确发票号，其身份字段同时命中 "
                f"{len(matched)} 票（发票号 {invoices}），归属不确定，"
                "未并入任何一票",
                sources=[e.source for name in ("mso", "serial", "order_code")
                         for e in p.identity.get(name, [])],
                action="请人工确认该附件属于哪一票后再分票"))
        elif len(matched) == 1:
            target = next(j for j in range(len(indexed))
                          if stage1_root[j] == matched[0])
            uf.union(i, target)
    # 阶段 3：其余弱证据归并。凡会把两个带不同明确发票号的簇并在一起的
    # 边一律拒绝并记为冲突问题（同一 MSO/序列号/Order Code 跨票出现）。
    weak_conflicts: list[tuple] = []
    for key in sorted(weak_owner):
        members = [j for j in weak_owner[key] if j not in ambiguous]
        for a, b in zip(members, members[1:]):
            ra, rb = uf.find(a), uf.find(b)
            if ra == rb:
                continue
            inv_a, inv_b = cluster_invoices(ra), cluster_invoices(rb)
            if inv_a and inv_b:
                weak_conflicts.append((key, a, b))
                continue
            uf.union(ra, rb)
    reported_conflicts: set[tuple] = set()
    for key, a, b in weak_conflicts:
        pa, pb = indexed[a], indexed[b]
        inv_a = sorted(cluster_invoices(uf.find(a)))
        inv_b = sorted(cluster_invoices(uf.find(b)))
        tag = (key, tuple(inv_a), tuple(inv_b))
        if tag in reported_conflicts:
            continue
        reported_conflicts.add(tag)
        kind_label = {"mso": "MSO", "ser": "序列号", "oc": "Order Code"}[key[0]]
        for p, mine, other in ((pa, inv_a, inv_b), (pb, inv_b, inv_a)):
            p.issues.append(_issue(
                "WEAK_IDENTITY_ACROSS_TICKETS",
                f"{kind_label} {key[1]} 同时出现在发票号 {mine} 与 {other} "
                "的附件中；弱身份字段不能跨票归并，已保持分票",
                sources=[e.source
                         for e in p.identity.get(
                             {"mso": "mso", "ser": "serial",
                              "oc": "order_code"}[key[0]], [])
                         if _norm_key(e.value) == key[1]],
                action="请人工确认共享该标识的附件归属与开票情况"))
    clusters: dict[int, list] = {}
    for i, p in enumerate(indexed):
        clusters.setdefault(uf.find(i), []).append(p)

    base: list = []
    for members in clusters.values():
        cand = _candidate_from_members(members)
        if cand is not None:
            base.append(cand)
    # 人工指派（阶段 2）：把“已读取但无业务身份”的附件并入唯一指定候选。
    # candidate_id 由基础簇冻结，人工并入不改变候选身份，只追加事实与来源；
    # 目标候选不存在时不静默丢弃：返回 unresolved 由调用方记阻断问题。
    unresolved: dict[str, str] = {}
    if injections:
        by_id = {c["candidate_id"]: c for c in base}
        for sha, inj in injections.items():
            target = by_id.get(inj["candidate_id"])
            if target is None:
                unresolved[sha] = inj["candidate_id"]
                continue
            profile = inj["profile"]
            merged = list(target["_members"]) + [profile]  # noqa: SLF001
            rebuilt = _candidate_from_members(
                merged, candidate_id=target["candidate_id"],
                assigned_ids={id(profile)})
            rebuilt["_mail_amount_rows"] = []  # 成员变化，金额核对需重跑
            base[base.index(target)] = rebuilt
    base.sort(key=lambda c: c["candidate_id"])
    return base, unresolved


def _candidate_from_members(members: list, *, candidate_id: str | None = None,
                            assigned_ids: frozenset = frozenset()) -> dict | None:
    """由一簇工作簿 profile 构建候选 dict（candidate_id 可冻结复用）。

    ``assigned_ids`` 中的 profile 是人工指派的成员：照常合并事实/区域并
    重新核对，但在 source_refs 中标注 assigned，便于审计与区分证据角色。
    """
    with_identity = [p for p in members if getattr(p, "_keys", None)]
    if not with_identity:
        return None  # 无身份附件由调用方归入 unmatched_sources
    issues: list = []
    checks: list = []
    for p in members:
        issues.extend(p.issues)
        checks.extend(p.checks)
    kinds = {p.kind_hint for p in members if p.kind_hint}
    if len(kinds) > 1:
        issues.append(_issue(
            "MIXED_KIND_CONFLICT",
            f"同一身份下混合了整机与配件附件：{sorted(kinds)}，不能猜测业务类型",
            action="请确认本票业务类型与附件归属"))
        kind = "unknown"
    else:
        kind = next(iter(kinds), "unknown")
    if len(members) > 1:
        hashes = sorted(p.sha256 for p in members)
        issues.append(_issue(
            "MULTIPLE_SOURCE_VERSIONS",
            f"本候选由 {len(members)} 份不同内容的附件合并："
            f"{[h[:12] for h in hashes]}，已按身份合并并逐字段核对",
            action="",
            blocking=False))
    label = (sorted({str(e.value).strip()
                     for p in members for e in p.identity.get("invoice_no", [])})
             or sorted({_norm_key(e.value)
                        for p in members for e in p.identity.get("mso", [])})
             or [members[0].sha256[:12]])
    identity: dict[str, list] = {}
    # 发票号：单值语义；同号内容冲突必须并列展示，不吞掉任何版本
    inv_entries = [e for p in members for e in p.identity.get("invoice_no", [])]
    merged_inv = _merge_fact("invoice_no", inv_entries, issues, label[0])
    if merged_inv is not None:
        if merged_inv.get("conflict"):
            identity["invoice_no"] = sorted(
                v["value"] for v in merged_inv["variants"])
        else:
            identity["invoice_no"] = [merged_inv["value"]]
    # MSO/序列号/Order Code：集合语义（一票多机/多套件正常）；同一候选
    # 内不同附件的同名字段集合不一致（同号不同机器集）必须可见，不静默并集
    for name in ("mso", "serial", "order_code"):
        per_profile = []
        for p in members:
            values = {_norm_key(e.value) for e in p.identity.get(name, [])}
            if values:
                per_profile.append((p, values))
        union = sorted(
            set().union(*(v for _, v in per_profile))) if per_profile else []
        if union:
            identity[name] = union
        if len(per_profile) > 1:
            distinct = {tuple(sorted(v)) for _, v in per_profile}
            if len(distinct) > 1:
                issues.append(_issue(
                    "IDENTITY_SET_MISMATCH",
                    f"同一候选内不同附件的 {name} 集合不一致："
                    + "；".join(f"{sorted(p.filenames)}={sorted(v)}"
                                for p, v in per_profile),
                    action="请人工确认这些附件是否属于同一票"))
    facts: dict[str, dict] = {}
    for name in _FACT_FIELDS:
        entries = [e for p in members for e in p.facts.get(name, [])]
        merged = _merge_fact(name, entries, issues, label[0])
        if merged is not None:
            facts[name] = merged
    missing = [n for n in _EXPECTED_FACTS.get(kind, ()) if n not in facts]
    if missing:
        issues.append(_issue(
            "FACT_MISSING",
            f"候选缺少关键事实字段：{', '.join(missing)}"
            "（提取不到的字段留空，不填造值）",
            field_name=",".join(missing),
            action="请确认附件是否包含完整的发票/箱单资料"))
    if not identity.get("invoice_no") and not identity.get("mso"):
        issues.append(_issue(
            "IDENTITY_INCOMPLETE",
            "候选既无发票号也无 MSO，业务身份不完整",
            action="请确认附件中的发票号与机器标识"))
    weight_rows = [row for p in members for row in p.weight_rows]
    source_refs = [{
        "sha256": p.sha256,
        "filenames": sorted(p.filenames),
        "regions": [r.to_dict() for r in p.regions],
        **({"assigned": True} if id(p) in assigned_ids else {}),
    } for p in sorted(members, key=lambda p: p.sha256)]
    if candidate_id is None:
        canonical = json.dumps({
            "invoice_nos": identity.get("invoice_no", []),
            "msos": sorted({_norm_key(v) for v in identity.get("mso", [])}),
            "order_codes": identity.get("order_code", []),
            "serials": sorted({_norm_key(v) for v in identity.get("serial", [])}),
            "members": sorted(p.sha256 for p in members),
        }, ensure_ascii=False, sort_keys=True)
        candidate_id = "cand-" + hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()[:16]
    return {
        "candidate_id": candidate_id,
        "kind": kind,
        "identity": identity,
        "source_refs": source_refs,
        "mail_row_refs": [],
        "facts": facts,
        "weight_rows": weight_rows,
        "checks": checks,
        "issues": [i.to_dict() for i in issues],
        "needs_confirmation": any(i.blocking for i in issues),
        "_members": members,          # 内部暂存，序列化前剔除
        "_identity_entries": {n: [e for p in members
                                  for e in p.identity.get(n, [])]
                              for n in _IDENTITY_FIELDS},
    }


# ---------------------------------------------------------------------------
# 正文业务行关联
# ---------------------------------------------------------------------------


def _machine_pairs(entries: dict) -> set:
    """从身份条目的来源坐标重建机器级 (mso, serial) 配对。

    同一台机器的 MSO/序列号取自同一表格行（来源 sheet+row 相同）；
    正文补认的条目（邮件来源，无 sheet）不参与配对。
    """
    by_row: dict[tuple, dict] = {}
    for entry in entries.get("mso", []):
        s = entry.source
        if s.sheet and s.row:
            by_row.setdefault((s.filename, s.sheet, s.row),
                              {})["mso"] = _norm_key(entry.value)
    for entry in entries.get("serial", []):
        s = entry.source
        if s.sheet and s.row:
            by_row.setdefault((s.filename, s.sheet, s.row),
                              {})["serial"] = _norm_key(entry.value)
    return {(v["mso"], v["serial"]) for v in by_row.values()
            if "mso" in v and "serial" in v}


def _canonical_machines(entries: dict) -> list:
    """附件冻结身份（仅工作簿来源）→ 规范机器清单，每台机器唯一规范 ID。

    同一表格行取出的 (mso, serial) 是一台机器；无配对信息的单字段身份
    各自成机。正文补认的条目（邮件来源）不参与——后续行判定基准以附件
    原始身份为准，不受补认影响。
    """
    def workbook_values(name: str) -> set:
        return {_norm_key(e.value) for e in entries.get(name, [])
                if e.source.type == "workbook"}

    pairs = _machine_pairs(entries)
    machines: dict[tuple, dict] = {}
    for mso, serial in sorted(pairs):
        machines[("pair", mso, serial)] = {
            "id": ("pair", mso, serial), "mso": mso, "serials": {serial}}
    for mso in sorted(workbook_values("mso") - {p[0] for p in pairs}):
        machines[("mso", mso)] = {
            "id": ("mso", mso), "mso": mso, "serials": set()}
    for serial in sorted(workbook_values("serial") - {p[1] for p in pairs}):
        machines[("ser", serial)] = {
            "id": ("ser", serial), "mso": None, "serials": {serial}}
    return list(machines.values())


def _map_row_to_machine(mso: str, serial: str, machines: list,
                        adopted: dict, row_source) -> tuple:
    """正文行标识 → 唯一规范机器；返回 (machine|None, Issue|None)。

    一个标识命中多台规范机器即歧义，显式阻断，不猜测。有效 MSO =
    附件原始 MSO 或此前行已补认的 MSO（补认只解决可寻址性，冲突判定
    基准仍是附件原始身份，见 _match_mail_rows）。
    """
    def effective_mso(m: dict):
        return m["mso"] or adopted.get(m["id"])

    exact, mso_hits, ser_hits = [], [], []
    for m in machines:
        if mso and effective_mso(m) == mso:
            mso_hits.append(m)
            if serial and serial in m["serials"]:
                exact.append(m)
        if serial and serial in m["serials"]:
            ser_hits.append(m)
    if exact:
        return exact[0], None

    def ambiguous(message: str):
        return None, _issue("MAIL_ROW_MACHINE_AMBIGUOUS", message,
                            sources=[row_source],
                            action="请核对附件机器清单与正文行标识")

    if mso and serial:
        if len(mso_hits) > 1 or len(ser_hits) > 1:
            return ambiguous(
                f"正文行（MSO={mso} 序列号={serial}）的标识命中多台机器，"
                "无法唯一映射")
        m_hit = mso_hits[0] if mso_hits else None
        s_hit = ser_hits[0] if ser_hits else None
        if m_hit and s_hit and m_hit is not s_hit:
            return None, _issue(
                "MSO_SERIAL_CROSS",
                f"正文行 MSO={mso} 与序列号 {serial} 分属同票不同机器，"
                "行内交叉错误，未采用",
                sources=[row_source],
                action="请核对正文行的 MSO 与序列号配对")
        if s_hit:
            return s_hit, None  # MSO 冲突/补认判定由调用方做
        if m_hit:
            return m_hit, None  # 序列号未知 → 调用方报 SERIAL_CONFLICT
        return ambiguous(
            f"正文行（MSO={mso} 序列号={serial}）无法映射到本票任何机器")
    pool = mso_hits if mso else ser_hits
    label = mso if mso else serial
    if len(pool) > 1:
        return ambiguous(
            f"正文行标识 {label} 命中 {len(pool)} 台机器，无法唯一映射")
    if pool:
        return pool[0], None
    return ambiguous(f"正文行标识 {label} 无法映射到本票任何机器")


def _check_mail_amount(cand: dict) -> None:
    """按票聚合正文金额并与整票金额核对（先关联、后聚合、再比较）。

    - 覆盖判定比较规范机器 ID 集合与附件 expected 集合本身（不是数量）：
      行→规范机器一一对应、无重复/交叉/缺价、映射集合 == expected 集合
      才允许把正文行合计当整票金额；否则 MAIL_AMOUNT_INCOMPLETE 显式问题，
      绝不把部分行总和当整票、不伪造 pass；
    - 附件无金额时：仅全量覆盖才采用正文合计（带来源），部分覆盖留空；
    - 附件已有金额冲突（同号多版本）时不重复比较。
    """
    rows = cand.pop("_mail_amount_rows", [])
    if not rows:
        return
    expected = {m["id"] for m in cand.get("_machines", [])}
    seen: dict = {}
    duplicates: list = []
    erroneous = False
    prices: list = []
    unpriced = 0
    for row, machine_id, err in rows:
        if err:
            erroneous = True
        if machine_id is not None:
            if machine_id in seen:
                duplicates.append(machine_id)
            else:
                seen[machine_id] = row
        price = to_decimal(row.fields.get("total_sales_price"))
        if price is None:
            unpriced += 1
        else:
            prices.append(price)
    full = (not erroneous and not duplicates and not unpriced
            and set(seen) == expected)
    if duplicates:
        cand["issues"].append(_issue(
            "MAIL_ROW_DUPLICATE",
            f"正文存在指向同一机器的重复业务行：{sorted(set(duplicates))}，"
            "无法判定金额是否重复计入",
            sources=[row.source for row, _, _ in rows],
            action="请核对正文业务表是否有重复行").to_dict())
        cand["needs_confirmation"] = True
    if not full:
        reasons = []
        if duplicates:
            reasons.append("存在重复行")
        if erroneous:
            reasons.append("存在交叉/冲突行")
        if unpriced:
            reasons.append(f"{unpriced} 行缺 Total Sales Price")
        if set(seen) != expected:
            reasons.append(f"仅覆盖 {len(seen)}/{len(expected)} 台机器")
        if not reasons:
            reasons.append("覆盖范围不可确认")
        cand["issues"].append(_issue(
            "MAIL_AMOUNT_INCOMPLETE",
            f"正文业务行不足以核对整票金额（{'；'.join(reasons)}），"
            "未将部分行合计当作整票金额",
            sources=[row.source for row, _, _ in rows],
            action="请补齐或核对正文业务行后人工核对整票金额").to_dict())
        cand["needs_confirmation"] = True
        return
    total = sum(prices)
    existing = cand["facts"].get("amount")
    row_sources = [row.source for row, _, _ in rows]
    if existing is not None and not existing.get("conflict"):
        current = to_decimal(existing["value"])
        ok = current is not None and _price_equal(current, total)
        cand["checks"].append({
            "rule": "PREVIEW_MAIL_AMOUNT",
            "result": "pass" if ok else "fail",
            "detail": f"正文 {len(prices)} 行金额合计 {total}"
                      f"（覆盖 {len(seen)}/{len(expected)} 台）vs 附件合计 "
                      f"{existing['value']}"})
        if not ok:
            cand["issues"].append(_issue(
                "AMOUNT_CONFLICT",
                f"正文业务行金额合计 {total} 与附件发票合计 "
                f"{existing['value']} 不一致，未采用任何一侧",
                sources=row_sources + [
                    Source(**s) if isinstance(s, dict) else s
                    for s in existing.get("sources", [])],
                action="请核对金额，不擅改价格").to_dict())
            cand["needs_confirmation"] = True
    elif existing is None:
        cand["facts"]["amount"] = {
            "value": _display_value("amount", total),
            "sources": [s.to_dict() for s in row_sources],
            "conflict": False}
        cand["checks"].append({
            "rule": "PREVIEW_MAIL_AMOUNT",
            "result": "pass",
            "detail": f"附件缺金额，采用正文 {len(prices)} 行全量合计 {total}"
                      f"（覆盖 {len(seen)}/{len(expected)} 台）"})


def _match_mail_rows(candidates: list, mail_rows: list,
                     top_issues: list, row_assignments: dict | None = None,
                     excluded_out: list | None = None) -> list:
    """正文业务行一对一关联候选：MSO 优先，序列号兜底；冲突即问题。

    关联阶段逐行定位候选并保持机器级 MSO×序列号配对语义（同票多机的
    集合身份不得掩盖行内交叉错误）；聚合阶段由 _check_mail_amount 按票
    核对整票金额。返回未匹配正文行（unmatched_mail_rows）。正文行绝不
    跨票重复使用。

    ``row_assignments``（阶段 2 人工调整）：{(table_index, row): op}。
    action=exclude 的行记入 excluded_out（带理由），不再 unmatched、不再
    阻断；action=assign 的行强制关联到指定候选——自动证据指向其他候选时
    记阻断冲突，目标不存在时记阻断问题，绝不静默改归属。
    """
    row_assignments = row_assignments or {}
    by_id = {c["candidate_id"]: c for c in candidates}
    unmatched = []
    for row in mail_rows:
        mso = _norm_key(row.fields.get("mso"))
        serial = _norm_key(row.fields.get("serial_number"))
        invoice = normalize_identifier(row.fields.get("invoice_no"))
        ref = {"table_index": row.source.table_index, "row": row.source.row,
               "mso": mso or None, "serial": serial or None,
               "fields": _row_fields_snapshot(row)}
        mso_hits, serial_hits = [], []
        for cand in candidates:
            c_msos = {_norm_key(v) for v in cand["identity"].get("mso", [])}
            c_serials = {_norm_key(v) for v in cand["identity"].get("serial", [])}
            if mso and mso in c_msos:
                mso_hits.append(cand)
            if serial and serial in c_serials:
                serial_hits.append(cand)
        hits = mso_hits or serial_hits
        assignment = row_assignments.get(
            (row.source.table_index, row.source.row))
        if assignment is not None:
            if assignment["action"] == "exclude":
                if excluded_out is not None:
                    excluded_out.append(
                        {**ref, "excluded": True,
                         "reason": assignment["reason"]})
                continue
            target = by_id.get(assignment["candidate_id"])
            if target is None:
                top_issues.append(_issue(
                    "ADJUSTMENT_TARGET_UNKNOWN",
                    f"正文行（表 {ref['table_index']} 第 {ref['row']} 行）被指"
                    f"派到不存在的候选 {assignment['candidate_id']}，未采用",
                    sources=[row.source],
                    action="请基于当前方案版本重新选择候选"))
                unmatched.append({**ref, "reason": "adjustment_target_unknown"})
                continue
            if len(hits) == 1 and hits[0] is not target:
                # 自动证据唯一指向其他候选：人工指派与之矛盾，拒绝并阻断。
                top_issues.append(_issue(
                    "ADJUSTMENT_CONFLICT",
                    f"正文行（MSO={mso} 序列号={serial}）人工指派与自动证据"
                    "（MSO/序列号唯一匹配）指向不同候选，未采用",
                    sources=[row.source],
                    action="请核对正文行身份与候选归属后再调整"))
                unmatched.append({**ref, "reason": "adjustment_conflict"})
                continue
            if len(hits) > 1 and target not in hits:
                top_issues.append(_issue(
                    "ADJUSTMENT_CONFLICT",
                    f"正文行（MSO={mso} 序列号={serial}）人工指派的目标不在"
                    "自动证据命中的候选集合内，未采用",
                    sources=[row.source],
                    action="请核对正文行身份与候选归属后再调整"))
                unmatched.append({**ref, "reason": "adjustment_conflict"})
                continue
            # hits 为空（自动无法定位）或命中多候选（人工精确归属歧义行）：
            # 采用人工指派，后续机器映射/金额核对照常重跑。
            hits = [target]
            ref["assigned"] = True
        if mso_hits and serial_hits and mso_hits != serial_hits:
            top_issues.append(_issue(
                "MAIL_ROW_AMBIGUOUS",
                f"正文行（MSO={mso} 序列号={serial}）按 MSO 与按序列号关联到"
                "不同候选，未采用",
                sources=[row.source],
                action="请核对正文行与附件的 MSO/序列号对应关系"))
            unmatched.append({**ref, "reason": "ambiguous_match"})
            continue
        if len(hits) > 1:
            top_issues.append(_issue(
                "MAIL_ROW_AMBIGUOUS",
                f"正文行（MSO={mso} 序列号={serial}）匹配到多个候选，未采用",
                sources=[row.source],
                action="请核对候选间的 MSO/序列号归属"))
            unmatched.append({**ref, "reason": "ambiguous_match"})
            continue
        if not hits:
            unmatched.append({**ref, "reason": "no_matching_candidate"})
            continue
        cand = hits[0]
        cand["mail_row_refs"].append(ref)
        entries = cand["_identity_entries"]
        cand_issues = []
        # 规范机器基准在首次关联时由附件冻结身份建立（不含任何正文补认），
        # 后续行的冲突判定与覆盖统计都以它为基准
        machines = cand.setdefault("_machines", _canonical_machines(entries))
        adopted = cand.setdefault("_adopted_msos", {})
        c_serials = {s for m in machines for s in m["serials"]}
        erroneous = False
        machine, map_issue = _map_row_to_machine(
            mso, serial, machines, adopted, row.source)
        if map_issue is not None:
            erroneous = True
            cand_issues.append(map_issue)
        elif mso and machine["mso"] is not None and machine["mso"] != mso:
            # 序列号兜底命中，但附件对该机器已有明确 MSO：冲突 MSO 不得
            # 作为补充身份并入，报告并阻断，不污染身份
            erroneous = True
            cand_issues.append(_issue(
                "MSO_CONFLICT",
                f"正文行序列号 {serial} 命中本票机器（附件 MSO "
                f"{machine['mso']}），但其 MSO {mso} 与附件既有 MSO 冲突，"
                "未补认、未采用",
                sources=[row.source],
                action="请核对正文行的 MSO 与机器序列号归属"))
        elif mso and machine["mso"] is None:
            # 附件对该机器确实无 MSO：唯一序列号完整匹配可补认（旧 CI
            # 布局正常兜底，标注邮件来源）；正文行间 MSO 不一致同样阻断
            prev = adopted.get(machine["id"])
            if prev is None:
                adopted[machine["id"]] = mso
                cand["identity"].setdefault("mso", [])
                if mso not in cand["identity"]["mso"]:
                    cand["identity"]["mso"] = sorted(
                        cand["identity"]["mso"] + [mso])
                entries.setdefault("mso", []).append(FactEntry(mso, row.source))
            elif prev != mso:
                erroneous = True
                cand_issues.append(_issue(
                    "MSO_CONFLICT",
                    f"正文行间对同一机器（序列号 {serial}）给出不同 MSO "
                    f"{prev} 与 {mso}，未补认后者",
                    sources=[row.source],
                    action="请核对正文行的 MSO 与机器归属"))
        # 序列号一致性：行给出序列号但不在附件机器序列号集合内
        if not erroneous and serial and c_serials and serial not in c_serials:
            erroneous = True
            cand_issues.append(_issue(
                "SERIAL_CONFLICT",
                f"正文行 MSO={mso} 的序列号 {serial} 与附件序列号 "
                f"{sorted(c_serials)} 不一致",
                sources=[row.source]
                + [e.source for e in entries.get("serial", [])],
                action="请核对机器序列号与 MSO 的对应关系"))
        machine_id = (machine["id"]
                      if machine is not None and not erroneous else None)
        # 发票号一致性
        if invoice:
            c_invs = {str(e.value).strip() for e in entries.get("invoice_no", [])}
            if c_invs and invoice not in c_invs:
                cand_issues.append(_issue(
                    "INVOICE_MISMATCH",
                    f"正文行 MSO={mso} 的发票号 {invoice} 与附件发票号 "
                    f"{sorted(c_invs)} 不一致",
                    sources=[row.source],
                    action="请核对正文业务行与附件发票的对应关系"))
        cand.setdefault("_mail_amount_rows", []).append(
            (row, machine_id, erroneous))
        if cand_issues:
            cand["issues"].extend(i.to_dict() for i in cand_issues)
            cand["needs_confirmation"] = True
    # 关联完成后按票聚合金额并核对（不能把部分行总和当整票）
    for cand in candidates:
        _check_mail_amount(cand)
    return unmatched


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def inspect_sources(request: dict) -> dict:
    """分票预览入口：本地工作簿 sources + 邮件正文 → 候选票 JSON。

    request（JSON 可序列化）：
    - sources: [{path, filename, mime_type?}] 本地工作簿路径（调用方负责
      把附件安全落盘；本入口只读，不写任何文件）；
    - mail: {html?, text?, message_id?, subject?}（可空）；
    - header_aliases: 可选别名扩展（与 process_job 同一配置语义）；
    - assignments: 可选人工调整（阶段 2），
      {"mail_rows": [{table_index, row, action, candidate_id?, reason?}],
       "sources": [{sha256, action, candidate_id?, reason?}]}，
      action 为 assign（指派到唯一候选，重跑核对）或 exclude（有理由排除）。

    永不写文件、永不联网、永不读写数据库与秘密。
    """
    sources = request.get("sources", []) or []
    mail = request.get("mail", {}) or {}
    raw_aliases = dict(_PREVIEW_ALIASES)
    for name, extra in (request.get("header_aliases") or {}).items():
        if isinstance(extra, (list, tuple)):
            raw_aliases.setdefault(name, [])
            raw_aliases[name] = list(raw_aliases[name]) + [
                str(x) for x in extra if str(x).strip()]
    aliases = merged_aliases(raw_aliases)
    marker = alias_marker(request.get("header_aliases") or None)

    top_issues: list = []
    assignments = _normalize_assignments(request.get("assignments"), top_issues)
    src_assignments = assignments["sources"]
    excluded_sources: list = []
    excluded_mail_rows: list = []

    # ---- 附件识别（按内容哈希去重） -------------------------------------
    profiles: list[WorkbookProfile] = []
    by_hash: dict[str, WorkbookProfile] = {}
    unmatched_sources: list = []
    injections: dict[str, dict] = {}
    excluded_shas: set[str] = set()
    for src in sources:
        path = str(src.get("path", ""))
        filename = src.get("filename") or Path(path).name
        p = Path(path)
        if not p.is_file():
            unmatched_sources.append({
                "filename": filename, "status": "unreadable",
                "reason": "文件不存在或不是普通文件"})
            top_issues.append(_issue(
                "UNREADABLE_SOURCE",
                f"附件 {filename} 不存在或不是普通文件",
                sources=[Source(type="workbook", filename=filename)],
                action="请确认附件文件可用"))
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest in by_hash:
            by_hash[digest].filenames.append(filename)
            continue
        profile = _profile_workbook(path, filename, aliases)
        by_hash[digest] = profile
        profiles.append(profile)
        adjustment = src_assignments.get(profile.sha256)
        if adjustment is not None and adjustment["action"] == "exclude":
            excluded_sources.append({
                "sha256": profile.sha256,
                "filenames": sorted(profile.filenames),
                "status": profile.status, "excluded": True,
                "reason": adjustment["reason"]})
            excluded_shas.add(profile.sha256)
            continue  # 有理由排除：留审计，不再 unmatched、不再阻断
        if adjustment is not None and adjustment["action"] == "assign":
            has_identity = any(profile.identity.get(name)
                               for name in _IDENTITY_FIELDS)
            if profile.status not in ("ok", "empty") or has_identity:
                top_issues.append(_issue(
                    "ADJUSTMENT_INVALID",
                    f"附件 {sorted(profile.filenames)} 指派无效：只有已读取"
                    "但未识别业务身份的工作簿可以指派到候选（状态 "
                    f"{profile.status}）",
                    sources=[Source(type="workbook", filename=f)
                             for f in sorted(profile.filenames)],
                    action="请改用排除并说明该附件的证据角色"))
                adjustment = None
        if adjustment is not None and adjustment["action"] == "assign":
            # “已读取但无身份”的工作簿：并入唯一指定候选（在候选构建时合并，
            # 合并后照常重跑字段核对；无身份附件不会成为桥接节点）。
            injections[profile.sha256] = {
                "candidate_id": adjustment["candidate_id"],
                "profile": profile}
            continue
        if profile.status == "unreadable":
            unmatched_sources.append({
                "sha256": profile.sha256, "filename": filename,
                "status": "unreadable", "reason": profile.error,
                "error_kind": profile.error_kind})
            code = ("WORKBOOK_DEPENDENCY" if profile.error_kind == "dependency"
                    else "UNSUPPORTED_FILE")
            top_issues.append(_issue(
                code, f"附件 {filename} 无法读取：{profile.error}",
                sources=[Source(type="workbook", filename=filename)],
                action=("附件无需重发；请联系维护人员安装缺失组件后重试"
                        if profile.error_kind == "dependency"
                        else "请补充未加密、未损坏的 xlsx 或 xls 附件")))
        elif profile.status == "not_workbook":
            unmatched_sources.append({
                "sha256": profile.sha256, "filename": filename,
                "status": "not_workbook", "reason": "非 xlsx/xls 附件，未解析"})
        elif profile.status == "empty":
            pass  # 无业务结构：稍后统一登记 identity_not_found / unmatched
    # 无身份附件：可见但不阻塞其他候选
    for profile in profiles:
        if profile.status not in ("ok", "empty"):
            continue
        if profile.sha256 in injections:
            continue  # 已人工指派，归入候选构建
        if profile.sha256 in excluded_shas:
            continue  # 已有理由排除：只留审计，不再登记未归属/阻断
        has_identity = any(profile.identity.get(name)
                           for name in _IDENTITY_FIELDS)
        if not has_identity:
            profile.status = "empty"
            if not profile.error:
                profile.error = "未识别到发票号/MSO/Order Code/序列号等业务身份"
            unmatched_sources.append({
                "sha256": profile.sha256, "filenames": sorted(profile.filenames),
                "status": "empty", "reason": profile.error})
            top_issues.append(_issue(
                "SOURCE_IDENTITY_NOT_FOUND",
                f"附件 {sorted(profile.filenames)} 已读取，但未识别到业务身份"
                "（发票号/MSO/Order Code/序列号），未归入任何候选",
                sources=[Source(type="workbook", filename=f)
                         for f in sorted(profile.filenames)],
                action="请确认附件为正确的业务工作簿，或显式指派/排除"))

    candidates, unresolved_injections = _build_candidates(
        profiles, top_issues, injections or None)
    for sha, target in sorted(unresolved_injections.items()):
        profile = by_hash.get(sha)
        filenames = sorted(profile.filenames) if profile else [sha[:12]]
        unmatched_sources.append({
            "sha256": sha, "filenames": filenames, "status": "empty",
            "reason": "人工指派的目标候选不存在，未并入"})
        top_issues.append(_issue(
            "ADJUSTMENT_TARGET_UNKNOWN",
            f"附件 {filenames} 被指派到不存在的候选 {target}，未并入任何候选",
            sources=[Source(type="workbook", filename=f) for f in filenames],
            action="请基于当前方案版本重新选择候选"))

    # ---- 正文解析与行关联 ------------------------------------------------
    mail_rows = []
    tables_seen = 0
    mail_result = None
    m_aliases = mail_aliases(raw_aliases)
    if mail.get("html"):
        mail_result = parse_mail_html(mail["html"], mail.get("message_id"),
                                      aliases=m_aliases, require_mso=False)
    elif mail.get("text"):
        mail_result = parse_mail_text(mail["text"], mail.get("message_id"),
                                      aliases=m_aliases, require_mso=False)
    if mail_result is not None:
        tables_seen = mail_result.tables_seen
        top_issues.extend(mail_result.issues)
        mail_rows = mail_result.rows
    unmatched_rows = _match_mail_rows(
        candidates, mail_rows, top_issues, assignments["mail_rows"],
        excluded_mail_rows)

    # ---- 汇总 ------------------------------------------------------------
    fingerprint = hashlib.sha256(json.dumps({
        "attachments": sorted(p.sha256 for p in profiles if p.sha256),
        "html": hashlib.sha256((mail.get("html") or "").encode("utf-8")).hexdigest(),
        "text": hashlib.sha256((mail.get("text") or "").encode("utf-8")).hexdigest(),
        "inspector": INSPECTOR_VERSION,
        "aliases": marker,
        "assignments": {
            "mail_rows": sorted(
                [list(k), v["action"], v["candidate_id"], v["reason"]]
                for k, v in assignments["mail_rows"].items()),
            "sources": sorted(
                [k, v["action"], v["candidate_id"], v["reason"]]
                for k, v in assignments["sources"].items()),
        },
    }, sort_keys=True).encode("utf-8")).hexdigest()

    serializable = []
    for cand in candidates:
        cand = {k: v for k, v in cand.items() if not k.startswith("_")}
        serializable.append(cand)
    blocking_top = [i for i in top_issues if i.blocking]
    batch_ready = (bool(serializable) and not blocking_top
                   and not any(c["needs_confirmation"] for c in serializable))
    return {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "inspector_version": INSPECTOR_VERSION,
        "alias_marker": marker,
        "source_fingerprint": fingerprint,
        "mail": {
            "subject": mail.get("subject"),
            "message_id": mail.get("message_id"),
            "tables_seen": tables_seen,
            "notes": extract_mail_notes(mail.get("html") or "",
                                        mail.get("text") or ""),
        },
        "candidate_count": len(serializable),
        "candidates": serializable,
        "unmatched_sources": unmatched_sources,
        "unmatched_mail_rows": unmatched_rows,
        "excluded_sources": excluded_sources,
        "excluded_mail_rows": excluded_mail_rows,
        "assignments": {
            "mail_rows": [
                {"table_index": k[0], "row": k[1], **v}
                for k, v in sorted(assignments["mail_rows"].items())],
            "sources": [
                {"sha256": k, **v}
                for k, v in sorted(assignments["sources"].items())],
        },
        "issues": [i.to_dict() for i in top_issues],
        "batch_ready": batch_ready,
    }
