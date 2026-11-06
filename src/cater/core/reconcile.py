"""整机双来源核对与合并。

规则（plan.md §5）：
- 用 MSO 关联邮件行与箱单/发票机器；序列号辅助核对；禁止凭行号或仅凭机型拼接；
- 逐字段核对：仅一处有值取该值；两处一致合并保留双来源；不同则列出两处
  原值与位置并暂停，不静默覆盖；
- 金额相等性：不允许用精度容差掩盖真实差异——仅视为相等当且仅当
  规范化后完全相同，或差异小于相对 1e-9（Excel 二进制浮点尾差量级）；
- Order(SO) 比较前规范化（无前导零补一个）：620186623 与 0620186623 同义；
- Material 经机型库显式别名视为同义（如 SEM520高配 → SEM520）；
- 合并后仍缺必需字段同样列出追问；来源互补不算缺失；
- 发票单价与 Total Sales Price 是不同业务字段，不互相覆盖、不当同字段冲突；
- 同一 MSO 对应多个不同序列号视为冲突；比较前只做已确认的规范化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .models import FieldValue, Issue, MachineRecord, Source
from .numbertext import (MAX_DP_WEIGHT, decimals_close, normalize_identifier,
                         normalize_so, to_decimal)

# 邮件业务表字段 → 标准字段
MAIL_FIELDS = {
    "dealer_code": "dealer_code",
    "dealer_order": "dealer_order",
    "order": "order",
    "material": "material",
    "serial_number": "serial_number",
    "total_sales_price": "total_sales_price",
}

# 工作簿机器表字段 → 标准字段
WORKBOOK_FIELDS = {
    "model": "material",         # 与邮件 Material 同语义，需核对
    "loader": "serial_number",   # 与邮件 Serial Number 同语义，需核对
    "pin": "pin",
    "engine": "engine",
}

REQUIRED_FIELDS = [
    "dealer_code", "dealer_order", "order", "material",
    "serial_number", "total_sales_price", "net_weight",
]


@dataclass
class ReconcileResult:
    records: list = field(default_factory=list)  # list[MachineRecord]
    issues: list = field(default_factory=list)


def _price_equal(a: Decimal, b: Decimal) -> bool:
    """金额相等：规范化后完全相同，或差异在相对 1e-9 内（浮点尾差量级）。

    1e-9 相对容差最多吸收双精度表示尾差（~1e-16 相对），
    而真实价格差异（最小 0.01 元，~1e-7 相对）必然产生冲突。
    """
    if a == b:
        return True
    if a == 0 or b == 0:
        return False
    return abs(a - b) <= max(abs(a), abs(b)) * Decimal("1e-9")


def _plain_decimal(d: Decimal) -> Decimal:
    """去掉浮点来源的多余尾零（17300.0→17300），不引入舍入。"""
    quantized = d.quantize(Decimal("0.00001"))
    return Decimal(format(quantized.normalize(), "f"))


def _model_key(text) -> str:
    return " ".join(str(text).replace("\xa0", " ").split()).upper()


def _same(name: str, a, b, machine_index=None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        da, db = to_decimal(a), to_decimal(b)
        if da is None or db is None:
            return str(a).strip() == str(b).strip()
        if name == "total_sales_price":
            return _price_equal(da, db)
        return decimals_close(da, db, max_dp=MAX_DP_WEIGHT)
    if name == "order":
        return normalize_so(str(a)) == normalize_so(str(b))
    if name == "material" and machine_index is not None:
        ka, kb = _model_key(a), _model_key(b)
        if ka == kb:
            return True
        ta = machine_index.alias_target(a)
        tb = machine_index.alias_target(b)
        return bool(ta and tb and _model_key(ta) == _model_key(tb))
    return normalize_identifier(a) == normalize_identifier(b)


def _merge_equal(existing: FieldValue, raw, source: Source) -> None:
    """双来源判定一致：保留两个来源定位。"""
    if not isinstance(existing.raw, list):
        existing.raw = [existing.raw]
    existing.raw.append(raw)
    existing.source = Source(type="merged",
                             detail=f"{existing.source.to_dict()} + {source.to_dict()}")


def _add_field(rec: MachineRecord, name: str, raw, source: Source, issues: list,
               machine_index=None) -> None:
    if raw in (None, ""):
        return
    value = raw
    if name == "total_sales_price":
        value = to_decimal(raw)
        if value is None:
            issues.append(Issue(
                code="INVALID_NUMBER",
                record_key=rec.mso,
                field=name,
                message=f"Total Sales Price 无法解析为金额：{raw!r}",
                sources=[source],
                required_action="请回复该机器正确的 Total Sales Price",
            ))
            return
    existing = rec.fields.get(name)
    if existing is None:
        if name == "order":
            value = normalize_so(str(raw))
        rec.fields[name] = FieldValue(raw, value, source)
        return
    if _same(name, existing.value, value, machine_index):
        if name == "order":
            existing.value = normalize_so(str(existing.value))
        _merge_equal(existing, raw, source)
        return
    issues.append(Issue(
        code="FIELD_CONFLICT",
        record_key=rec.mso,
        field=name,
        message=f"字段 {name} 两来源不一致：{existing.raw!r} ≠ {raw!r}",
        sources=[existing.source, source],
        required_action=f"请回复 MSO={rec.mso} 的 {name} 正确值，例如 MSO={rec.mso}；{name}=<值>",
    ))


def reconcile_machines(mail_rows: list, invoice_rows: list, packing_rows: list,
                       weight_groups: list, machine_index=None,
                       supplement_rows: Optional[list] = None) -> ReconcileResult:
    """machine_index 用于 Material 显式别名的同义判定（与目的国 scope 无关）。

    ``supplement_rows`` 为补件邮件业务表（同一 MSO 的后续补充行）：与原始
    邮件行同样按 MSO 经 _add_field 逐字段合并——缺失填充、一致去重保留
    双来源、真实冲突列两处原值与来源并暂停，不静默覆盖；只影响其 MSO
    对应的记录。表内 MSO 重复仍报 DUPLICATE_RECORD。"""
    result = ReconcileResult()
    records: dict[str, MachineRecord] = {}
    order_counter = 0

    def get_rec(mso: str) -> MachineRecord:
        nonlocal order_counter
        mso = normalize_identifier(mso)
        if mso not in records:
            records[mso] = MachineRecord(mso=mso, order_index=order_counter)
            order_counter += 1
        return records[mso]

    seen_mail_msos = set()
    for row in mail_rows:
        mso = normalize_identifier(row.fields.get("mso"))
        if not mso:
            continue
        if mso in seen_mail_msos:
            result.issues.append(Issue(
                code="DUPLICATE_RECORD",
                record_key=mso,
                message=f"邮件业务表中 MSO {mso} 重复出现",
                sources=[row.source],
                required_action="请确认该机器在业务表中只出现一次",
            ))
            continue
        seen_mail_msos.add(mso)
        rec = get_rec(mso)
        for mail_field, std in MAIL_FIELDS.items():
            _add_field(rec, std, row.fields.get(mail_field), row.source,
                       result.issues, machine_index)

    # 补件业务表：逐字段合并到对应 MSO（来源为补件邮件，provenance 分开）。
    # 多封部分表格补件按 MSO 跨封累积：同一 MSO 允许出现在多封里（各提供
    # 部分字段，早先填充的保留，后到的只补缺失）；只有同一封内同一 MSO
    # 重复出现才算 DUPLICATE_RECORD。
    supplement_seen: dict[str, str] = {}
    for row in supplement_rows or []:
        mso = normalize_identifier(row.fields.get("mso"))
        if not mso:
            continue
        source_key = str(getattr(row.source, "message_id", None) or id(row.source))
        if supplement_seen.get(mso) == source_key:
            result.issues.append(Issue(
                code="DUPLICATE_RECORD",
                record_key=mso,
                message=f"同一封补件业务表中 MSO {mso} 重复出现",
                sources=[row.source],
                required_action="请确认该机器在业务表中只出现一次",
            ))
            continue
        supplement_seen[mso] = source_key
        rec = get_rec(mso)
        for mail_field, std in MAIL_FIELDS.items():
            _add_field(rec, std, row.fields.get(mail_field), row.source,
                       result.issues, machine_index)

    for rows in (invoice_rows, packing_rows):
        for mrow in rows:
            rec = get_rec(mrow.mso)
            for wb_field, std in WORKBOOK_FIELDS.items():
                _add_field(rec, std, getattr(mrow, wb_field, None), mrow.source,
                           result.issues, machine_index)

    # 同一序列号对应多个 MSO → 冲突
    serial_to_mso: dict[str, str] = {}
    for mso, rec in records.items():
        fv = rec.fields.get("serial_number")
        if fv is None or fv.value in (None, ""):
            continue
        serial = normalize_identifier(fv.value)
        if serial in serial_to_mso and serial_to_mso[serial] != mso:
            result.issues.append(Issue(
                code="DUPLICATE_SERIAL",
                record_key=mso,
                field="serial_number",
                message=f"序列号 {serial} 同时对应 MSO {serial_to_mso[serial]} 与 {mso}",
                sources=[fv.source],
                required_action="请核对序列号与 MSO 的对应关系",
            ))
        else:
            serial_to_mso[serial] = mso

    # 重量组分配：按组内 MSO 清单关联，禁止仅凭机型拼接
    group_by_mso: dict[str, object] = {}
    for group in weight_groups:
        for mso in group.msos:
            mso_n = normalize_identifier(mso)
            if mso_n in group_by_mso:
                result.issues.append(Issue(
                    code="WEIGHT_GROUP_OVERLAP",
                    record_key=mso_n,
                    message=f"MSO {mso_n} 出现在多个箱单重量组中，无法关联",
                    sources=[group.source, group_by_mso[mso_n].source],
                    required_action="请明确该机器所属的箱单重量组",
                ))
            else:
                group_by_mso[mso_n] = group
    for mso, rec in records.items():
        group = group_by_mso.get(mso)
        if group is None:
            continue
        per = group.net_weight / Decimal(group.qty)
        # 模板允许 5 位小数；超出精度不静默截断
        if per != per.quantize(Decimal("0.00001")):
            result.issues.append(Issue(
                code="WEIGHT_PRECISION",
                record_key=mso,
                field="net_weight",
                message=f"单台净重 {per} 超过模板允许的 5 位小数精度",
                sources=[group.source],
                required_action="请确认单台净重的取值精度",
            ))
            continue
        rec.fields["net_weight"] = FieldValue(
            f"{group.net_weight}/{group.qty}", _plain_decimal(per),
            Source(type="computed", filename=group.source.filename,
                   sheet=group.source.sheet, cell=group.source.cell,
                   detail=f"箱单组净重{group.net_weight}÷数量{group.qty}"))

    # 必需字段汇总检查（合并后仍缺才报；来源互补不算缺失）
    for mso, rec in records.items():
        missing = [f for f in REQUIRED_FIELDS if f not in rec.fields
                   or rec.fields[f].value in (None, "")]
        if missing:
            result.issues.append(Issue(
                code="MISSING_FIELD",
                record_key=mso,
                field=",".join(missing),
                message=f"MSO {mso} 合并后仍缺必需字段：{', '.join(missing)}",
                sources=[fv.source for k, fv in rec.fields.items()],
                required_action=(
                    f"请将包含表头与 MSO 列的完整业务表直接粘贴到回复中"
                    f"（HTML 表格或以 Tab/空格明确分列的文本均可；"
                    f"本行缺少：{', '.join(missing)}）"
                ),
            ))

    result.records = sorted(records.values(), key=lambda r: r.order_index)
    return result
