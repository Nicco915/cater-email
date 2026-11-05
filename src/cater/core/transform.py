"""标准记录 → 输出行（字段固定值、清洗、量化与 provenance）。

映射依据 docs/field-mapping.md（plan.md §5 已确认规则）。
价格字段语义不可混同：Machine Price 只取 Total Sales Price；
发票单价（Invoice U'PRICE / Net Value）不参与输出。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .models import FieldValue, Issue, Source
from .numbertext import (MAX_DP_PRICE, MAX_DP_QTY, MAX_DP_WEIGHT,
                         clean_spec, decimal_to_text, normalize_so, to_decimal)


def _dec_text(raw, max_dp: int) -> str:
    """字段值（Decimal 或经覆盖修正的文本）→ 量化文本。"""
    d = to_decimal(raw)
    if d is None:
        raise ValueError(f"数值字段无法解析：{raw!r}")
    return decimal_to_text(d, max_dp)


def _model_key(text) -> str:
    return " ".join(str(text).replace("\xa0", " ").split()).upper()

# 整机固定字段（plan.md §5）
MACHINE_FIXED = {
    "decl_unit": "台",
    "goods_type": "成品",
    "enterprise_unit": "台",
    "legal_ratio": "1",
    "currency": "USD",
    "coo": "CHN",
}
PARTS_CURRENCY = "USD"


def _prov(record_key, field, value, source) -> dict:
    return {"record_key": record_key, "field": field,
            "value": None if value is None else str(value),
            "source": source.to_dict()}


def build_parts_rows(records, country_index, destination: Optional[dict],
                     gross_weight, packages, invoice_no,
                     gross_source, packages_source, invoice_source,
                     issues: list) -> tuple:
    """返回 (rows, json_records, provenance, normalizations)。

    destination = {name, alpha3} 或 None。normalizations 记录自动窄规范化
    （如规格型号开头 ``1||`` → ``1|`` 删除多余空项）：含记录键、字段、
    前后完整值（保留内部竖线）、来源与可读摘要，供交付正文“实际修正”节
    与人工审计使用——自动修正必须与按回复执行的修正一并可见。
    """
    rows, json_records, provenance, normalizations = [], [], [], []
    if gross_weight is None:
        issues.append(Issue(
            code="MISSING_FIELD", field="gross_weight",
            message="缺少整票毛重（Packing List TOTAL 行 J 列）",
            sources=[gross_source] if gross_source else [],
            required_action="请补充箱单总毛重"))
    if packages is None:
        issues.append(Issue(
            code="MISSING_FIELD", field="packages",
            message="缺少整票件数（Packing List TOTAL 行 G 列）",
            sources=[packages_source] if packages_source else [],
            required_action="请补充箱单总件数"))
    if not invoice_no:
        issues.append(Issue(
            code="MISSING_FIELD", field="invoice_no",
            message="缺少发票号（最终发票表 INVOICE NO.）",
            sources=[invoice_source] if invoice_source else [],
            required_action="请确认最终发票表包含发票号"))
    dest_alpha3 = None
    if destination:
        dest_alpha3 = destination.get("alpha3")
        if not dest_alpha3 and destination.get("name"):
            dest_alpha3 = country_index.resolve(destination["name"])
        if not dest_alpha3:
            issues.append(Issue(
                code="UNKNOWN_COUNTRY",
                field="destination",
                message=f"目的国无法映射为三位码：{destination!r}",
                sources=[Source(type="user_input", detail=str(destination))],
                required_action="请回复国家映射，例如 国家映射：France=FRA",
            ))
    for idx, rec in enumerate(records):
        f = rec.fields
        row = {}
        prov_src = {}
        coo_raw = f["coo"].value if f.get("coo") else None
        coo_alpha3 = country_index.resolve(coo_raw) if coo_raw else None
        if coo_raw and not coo_alpha3:
            issues.append(Issue(
                code="UNKNOWN_COUNTRY",
                record_key=rec.part_code,
                field="coo",
                message=f"原产国代码无法映射为三位码：{coo_raw!r}",
                sources=[f["coo"].source],
                required_action="请回复国家映射，例如 国家映射：US=USA",
            ))
        missing = []
        for key in ("qty", "unit_price", "net_weight", "hs_code", "product_name", "spec"):
            if key not in f or f[key].value in (None, ""):
                missing.append(key)
        if missing:
            issues.append(Issue(
                code="MISSING_FIELD",
                record_key=rec.part_code,
                field=",".join(missing),
                message=f"料号 {rec.part_code} 缺少必需字段：{', '.join(missing)}",
                sources=[fv.source for fv in f.values()],
                required_action="请补充最终发票表对应列后重发",
            ))
            continue
        row["part_code"] = rec.part_code
        try:
            row["qty"] = _dec_text(f["qty"].value, MAX_DP_QTY)
            row["unit_price"] = _dec_text(f["unit_price"].value, MAX_DP_PRICE)
            row["net_weight"] = _dec_text(f["net_weight"].value, MAX_DP_WEIGHT)
        except ValueError as exc:
            issues.append(Issue(
                code="INVALID_NUMBER", record_key=rec.part_code,
                message=str(exc),
                sources=[fv.source for fv in f.values()],
                required_action="请提供有效的数字取值"))
            continue
        row["currency"] = PARTS_CURRENCY
        row["coo"] = coo_alpha3 or ""
        row["destination"] = dest_alpha3 or ""
        row["hs_code"] = str(f["hs_code"].value)
        row["product_name"] = str(f["product_name"].value)
        row["spec_raw"] = str(f["spec"].value)
        row["spec"] = clean_spec(row["spec_raw"])
        if row["spec"] != row["spec_raw"]:
            spec_src = f["spec"].source
            loc = ""
            if spec_src is not None and spec_src.sheet and spec_src.cell:
                loc = f"（{spec_src.sheet}!{spec_src.cell}）"
            normalizations.append({
                "record_key": rec.part_code, "field": "spec",
                "before": row["spec_raw"], "after": row["spec"],
                "summary": f"料号 {rec.part_code} 规格型号自动规范化"
                           f"（删除开头多余空项）：{row['spec_raw']} → "
                           f"{row['spec']}{loc}",
                "source": spec_src.to_dict() if spec_src is not None else None,
            })
        del row["spec_raw"]
        row["order_no"] = ""  # 已确认本期留空，禁止复制旧值
        if idx == 0:
            # 已确认三个输出值仅首条填写
            if gross_weight is not None:
                row["gross_weight"] = _dec_text(gross_weight, MAX_DP_WEIGHT)
            if packages is not None:
                row["packages"] = _dec_text(packages, MAX_DP_QTY)
            if invoice_no:
                row["invoice_no"] = invoice_no
        rows.append(row)
        for key, col_field in (("part_code", "part_code"), ("qty", "qty"),
                               ("unit_price", "unit_price"), ("net_weight", "net_weight"),
                               ("coo", "coo"), ("hs_code", "hs_code"),
                               ("product_name", "product_name"), ("spec", "spec")):
            fv = f.get(key)
            if fv is not None:
                provenance.append(_prov(rec.part_code, col_field, fv.value, fv.source))
        provenance.append(_prov(rec.part_code, "currency", PARTS_CURRENCY,
                                Source(type="fixed", detail="币制固定 USD")))
        if dest_alpha3:
            provenance.append(_prov(rec.part_code, "destination", dest_alpha3,
                                    Source(type="user_input", detail="目的国←本次用户输入")))
        if idx == 0:
            if gross_weight is not None and gross_source:
                provenance.append(_prov(rec.part_code, "gross_weight", gross_weight, gross_source))
            if packages is not None and packages_source:
                provenance.append(_prov(rec.part_code, "packages", packages, packages_source))
            if invoice_no and invoice_source:
                provenance.append(_prov(rec.part_code, "invoice_no", invoice_no, invoice_source))
        json_records.append({
            "record_key": rec.part_code,
            "fields": {k: (None if fv.value is None else str(fv.value)) for k, fv in f.items()},
            "row": row,
        })
    return rows, json_records, provenance, normalizations


def build_machine_rows(records, machine_index, destination: Optional[dict],
                       issues: list) -> tuple:
    """返回 (material_rows, comm_rows, json_records, provenance)。"""
    material_rows, comm_rows, json_records, provenance = [], [], [], []
    dest_name = destination.get("name") if destination else None
    dest_alpha3 = None
    if destination:
        dest_alpha3 = destination.get("alpha3")
        if not dest_alpha3 and dest_name:
            dest_alpha3 = country_index.resolve(dest_name)
    if not destination or not dest_name:
        issues.append(Issue(
            code="MISSING_DESTINATION",
            field="destination",
            message="缺少目的国，整机沟通表国家列无法填写",
            sources=[],
            required_action="请回复 目的国：<国家中文名>",
        ))
    for rec in records:
        f = rec.fields
        material = f["material"].value if f.get("material") else None
        # 按目的国 scope 选机型变体（如 915F：BRA→123KW，其余→110KW）
        ref = machine_index.match(material, dest_alpha3) if material else None
        std_model = machine_index.alias_target(material, dest_alpha3) if material else None
        if material and ref is None:
            if machine_index is not None and \
                    _model_key(material) in getattr(machine_index, "deferred_unconfirmed", {}):
                entry = machine_index.deferred_unconfirmed[_model_key(material)]
                issues.append(Issue(
                    code="UNCONFIRMED_SPECIAL_RULE",
                    record_key=rec.mso,
                    field="material",
                    message=f"机型 {material!r} 存在未经确认的特殊配置记录"
                            f"（Sheet1 第 {entry.get('source_row')} 行 A 列标记 "
                            f"{entry.get('special')!r}，scope={entry.get('destination_scope')}），"
                            "已暂停匹配，不推广猜测规则",
                    sources=[f["material"].source],
                    required_action="请业务确认该机型的特殊配置规则后重试",
                ))
                continue
            issues.append(Issue(
                code="UNKNOWN_MODEL",
                record_key=rec.mso,
                field="material",
                message=f"机型 {material!r} 在机型库中无匹配（不静默模糊匹配）",
                sources=[f["material"].source],
                required_action="请确认机型或通过管理页维护显式别名",
            ))
            continue
        # 合并后仍缺必需字段的机器跳过输出（MISSING_FIELD 问题已由核对阶段登记）
        required = ("dealer_code", "dealer_order", "order", "material",
                    "serial_number", "total_sales_price", "net_weight")
        if any(k not in f or f[k].value in (None, "") for k in required):
            continue
        net = f.get("net_weight")
        try:
            net_text = _dec_text(net.value, MAX_DP_WEIGHT) if net else None
            price_text = _dec_text(f["total_sales_price"].value, MAX_DP_PRICE)
        except ValueError as exc:
            issues.append(Issue(
                code="INVALID_NUMBER", record_key=rec.mso,
                message=str(exc),
                sources=[fv.source for fv in f.values()],
                required_action="请提供有效的数字取值"))
            continue
        mrow = {
            "part_code": rec.mso,
            "hs_code": str(ref["hs_code"]),
            "product_name": str(ref["product_name"]),
            "declaration": str(ref["declaration"]),  # 数据库原样提取
            "decl_unit": MACHINE_FIXED["decl_unit"],
            "english_name": str(ref.get("english_name") or ""),
            "goods_type": MACHINE_FIXED["goods_type"],
            "enterprise_unit": MACHINE_FIXED["enterprise_unit"],
            "legal_ratio": MACHINE_FIXED["legal_ratio"],
            "second_ratio": net_text,
            "unit_net_weight": net_text,
            "currency": MACHINE_FIXED["currency"],
            "coo": MACHINE_FIXED["coo"],
            "model": std_model or str(ref["model"]),
        }
        material_rows.append(mrow)
        crow = {
            "dealer_code": str(f["dealer_code"].value),
            "dealer_order": str(f["dealer_order"].value),
            "mso": rec.mso,
            "material_col": rec.mso,  # 沟通表 Material 列已确认取 MSO
            "so": normalize_so(str(f["order"].value)),
            "model": std_model or str(ref["model"]),
            "serial_number": str(f["serial_number"].value),
            "machine_price": price_text,
            "country": dest_name or "",
        }
        comm_rows.append(crow)
        for key in ("dealer_code", "dealer_order", "order", "material",
                    "serial_number", "total_sales_price", "net_weight", "pin", "engine"):
            fv = f.get(key)
            if fv is not None:
                provenance.append(_prov(rec.mso, key, fv.value, fv.source))
        provenance.append(_prov(rec.mso, "hs_code", ref["hs_code"],
                                Source(type="reference", detail="机型库 Sheet1")))
        provenance.append(_prov(rec.mso, "declaration", ref["declaration"],
                                Source(type="reference", detail="机型库 Sheet1 原样提取")))
        provenance.append(_prov(rec.mso, "country", dest_name,
                                Source(type="user_input", detail="目的国←本次用户输入")))
        json_records.append({
            "record_key": rec.mso,
            "fields": {k: (None if fv.value is None else str(fv.value)) for k, fv in f.items()},
            "model_ref": {k: ref.get(k) for k in
                          ("model", "english_name", "hs_code", "product_name", "declaration")},
        })
    return material_rows, comm_rows, json_records, provenance
