"""业务转换核心公共接口（plan/execution.md 公共接口 v1）。

- ``process_job(request: dict) -> dict``：配件/整机离线转换。
  永不发送邮件、不写业务数据库；输入输出均为 JSON 可序列化 dict。
- 附件支持 .xlsx 与真正 BIFF .xls（统一只读工作簿接口，见
  parsing/workbook.py）；``header_aliases`` 可按字段配置表头别名。
- 响应 ``layout`` 携带采用工作表、结构定位、已执行核对与规则 ID，
  供上层做布局确认与回复正文展示（不含正文/秘密）。
- ``import_machine_reference(path: str) -> list[dict]``：仅读机型库 Sheet1，
  供应用初始化本地机型表。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import openpyxl

from .models import FieldValue, Issue, Source
from .parsing.headers import merged_aliases, normalize_header
from .parsing.mail_html import parse_mail_html, parse_mail_text
from .parsing.machine_wb import (INVOICE_NAME_KEYS, PACKING_NAME_KEYS,
                                 parse_machine_workbook)
from .parsing.parts import parse_parts_workbook
from .parsing.workbook import (SUPPORTED_SUFFIXES,
                             UnsupportedSourceError, load_workbook_pair)
from .reconcile import reconcile_machines
from .references import CountryIndex, MachineIndex
from .template_writer import (MACHINE_COMM_SPEC, MACHINE_MATERIALS_SPEC,
                              PARTS_LEDGER_SPEC, TemplateWriteError, write_output)
from .transform import build_machine_rows, build_parts_rows

_REPO_ROOT = Path(__file__).resolve().parents[3]

OUTPUT_NAMES = {
    "parts_ledger": "出口台账表体.xlsx",
    "machine_materials": "企业物料 上传.xlsx",
    "machine_comm": "SEM发运沟通表 上传.xlsx",
}


def _template_dir() -> Path:
    return Path(os.environ.get("CATER_TEMPLATE_DIR", _REPO_ROOT / "templates"))


def _content_hash(kind: str, destination: Optional[dict], records: list) -> str:
    canonical = {
        "kind": kind,
        "destination": (destination or {}).get("alpha3"),
        "records": sorted(
            json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) for r in records
        ),
    }
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _workbook_sheet_keys(path: str) -> Optional[list]:
    """统一只读接口取 sheet 名列表（规范化小写键）；无法读取返回 None。"""
    if not str(path).lower().endswith(SUPPORTED_SUFFIXES):
        return None
    try:
        wb, _ = load_workbook_pair(path)
        names = [normalize_header(n) for n in wb.sheetnames]
        if hasattr(wb, "close"):
            wb.close()
        return names
    except UnsupportedSourceError:
        return None
    except Exception:
        return None


def _classify(sources: list, mail: dict) -> str:
    """kind=auto 时的业务识别：配件看 CI-For Dealer 表，整机看业务表别名或机器信息表。"""
    for src in sources:
        names = _workbook_sheet_keys(src.get("path", ""))
        if names and any(n.startswith("cifordealer") for n in names):
            return "parts"
    for src in sources:
        names = _workbook_sheet_keys(src.get("path", ""))
        if names and (INVOICE_NAME_KEYS & set(names) or PACKING_NAME_KEYS & set(names)):
            return "machine"
    if (mail or {}).get("html") or (mail or {}).get("text"):
        return "machine"
    return "unsupported"


def _apply_overrides(records_fields: dict, overrides: list, issues: list) -> None:
    """应用原发起人确认的修正；record_key 无法对齐的记录不静默覆盖。"""
    for ov in overrides or []:
        key = str(ov.get("record_key", "")).strip()
        field = ov.get("field")
        value = ov.get("value")
        src = Source(type="override", message_id=ov.get("source_message_id"))
        target = records_fields.get(key)
        if target is None or not field:
            issues.append(Issue(
                code="OVERRIDE_UNMATCHED",
                record_key=key or None,
                field=field,
                message=f"修正项无法对齐到记录：record_key={key!r} field={field!r}",
                sources=[src],
                required_action="请确认修正对应的 MSO/料号与字段名",
            ))
            continue
        target[field] = FieldValue(value, value, src)


def _build_response(status: str, kind: str, records: list, issues: list,
                    artifacts: list, provenance: list,
                    business_identity: Optional[str], content_hash: str,
                    layout: Optional[dict] = None) -> dict:
    return {
        "status": status,
        "kind": kind,
        "records": records,
        "issues": [i.to_dict() if isinstance(i, Issue) else i for i in issues],
        "artifacts": artifacts,
        "provenance": provenance,
        "business_identity": business_identity,
        "content_hash": content_hash,
        "layout": layout or {},
    }


def _table_structure(t: dict) -> dict:
    """结构指纹用的表投影：只含定位信息，不含行数/数值/明细。"""
    return {"sheet": t.get("sheet"),
            "header_row": t.get("header_row"),
            "subheader_row": t.get("subheader_row"),
            "columns": t.get("columns") or {},
            "supplementary": t.get("supplementary") or {}}


def canonical_structure(kind: str, layout: dict, alias_marker: str) -> dict:
    """规范化 STRUCTURE-ONLY 投影：同一模板新一票（不同料号/数量/重量、
    不同记录条数）必须得到相同指纹；表按语义角色键归并（不按 sheet 名），
    因此 sheet 重命名会体现为结构变化，而记录数导致的 TOTAL/数据结束行
    移动不影响指纹。

    排除：checks 的证据明细（record_key/detail 含真实值）、total_row、
    data_first_row/data_last_row（随记录数变化）、任何数值/正文。
    """
    tables: dict = {}
    for key in ("parts_invoice", "packing_total"):
        t = layout.get(key)
        if isinstance(t, dict) and t.get("sheet"):
            tables[key] = _table_structure(t)
    for key in ("machine_table", "weight_table"):
        for index, t in enumerate(layout.get(key) or []):
            if isinstance(t, dict) and t.get("sheet"):
                tables[f"{key}#{index}"] = _table_structure(t)
    return {"kind": kind, "alias_sets": alias_marker, "tables": tables}


def _alias_marker(request_aliases) -> str:
    """别名集标记：默认集固定版本号；自定义别名以其规范化并集哈希标记，
    使别名配置变化体现为结构边界（且不含任何正文/秘密）。"""
    if not request_aliases:
        return "default-v1"
    canonical = json.dumps(
        {name: sorted(keys) for name, keys in sorted(request_aliases.items())},
        ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"custom-{digest}"


def _layout_block(kind: str, layout: dict, request_aliases=None) -> dict:
    """整理布局上报块并计算结构指纹（不含正文/秘密，仅结构与规则ID）。

    指纹只覆盖 canonical_structure 投影；checks 证据（含 record_key/
    detail 真实值）原样保留在块内供追溯，不参与指纹。
    request_aliases 为请求级原始配置（非合并结果），用于别名边界标记。
    """
    alias_marker = _alias_marker(request_aliases)
    structure = canonical_structure(kind, layout, alias_marker)
    canonical = json.dumps(structure, ensure_ascii=False, sort_keys=True,
                           default=str)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return {"kind": kind, "fingerprint": fingerprint,
            "alias_sets": alias_marker, "structure": structure, **layout}


def _finalize_status(issues: list) -> str:
    return "waiting_input" if any(
        (i.blocking if isinstance(i, Issue) else i.get("blocking", True)) for i in issues
    ) else "ready"


def process_job(request: dict) -> dict:
    task_id = request.get("task_id", "")
    kind = request.get("kind", "auto")
    destination = request.get("destination")
    sources = request.get("sources", []) or []
    mail = request.get("mail", {}) or {}
    references = request.get("references", {}) or {}
    output_dir = request.get("output_dir") or os.path.join(
        os.environ.get("CATER_OUTPUT_DIR", tempfile_get()), task_id or "job")
    overrides = request.get("overrides", []) or []
    header_aliases = merged_aliases(request.get("header_aliases"))

    country_index = CountryIndex(
        references.get("countries"),
        authoritative=bool(references.get("authoritative")),
    )
    machine_index = MachineIndex(references.get("machines"),
                                 references.get("machine_aliases"))

    issues: list = []
    for conflict in country_index.conflicts:
        issues.append(Issue(code="COUNTRY_ALIAS_CONFLICT", message=conflict,
                            required_action="请通过管理页解决国家别名冲突"))
    for msg in machine_index.issues:
        issues.append(Issue(code="MACHINE_REFERENCE_CONFLICT", message=msg,
                            required_action="请通过管理页解决机型资料冲突"))

    if kind == "auto":
        kind = _classify(sources, mail)
    if kind not in ("parts", "machine"):
        return _build_response(
            "unsupported", "unsupported", [],
            [Issue(code="UNSUPPORTED_BUSINESS",
                   message="无法识别业务类型：既非配件发票也非整机资料",
                   required_action="请提供配件发票或整机发票箱单（xlsx 或 xls）及业务表")],
            [], [], None, _content_hash("unsupported", destination, []))

    if kind == "parts":
        return _process_parts(task_id, destination, sources, country_index,
                              output_dir, overrides, issues, header_aliases,
                              request.get("header_aliases"))
    return _process_machine(task_id, destination, sources, mail, country_index,
                            machine_index, output_dir, overrides, issues,
                            header_aliases, request.get("header_aliases"))


def tempfile_get() -> str:
    import tempfile
    return tempfile.gettempdir()


def _pick_workbook(sources: list, want: str, issues: list):
    """从 sources 中挑选含目标工作表的 xlsx/xls；返回 (path, filename) 或 (None, None)。"""
    matches = []
    for src in sources:
        names = _workbook_sheet_keys(src.get("path", ""))
        if names is None:
            continue
        if want == "parts" and any(n.startswith("cifordealer") for n in names):
            matches.append(src)
        if want == "machine" and (INVOICE_NAME_KEYS & set(names)
                                  or PACKING_NAME_KEYS & set(names)):
            matches.append(src)
    if len(matches) > 1:
        issues.append(Issue(
            code="AMBIGUOUS_INPUT",
            message=f"多个附件都包含目标工作表：{[m.get('filename') for m in matches]}",
            required_action="请确认应使用的附件",
        ))
        return None, None
    if matches:
        return matches[0].get("path"), matches[0].get("filename") or Path(matches[0]["path"]).name
    return None, None


def _process_parts(task_id, destination, sources, country_index,
                   output_dir, overrides, issues, header_aliases,
                   raw_aliases=None) -> dict:
    path, filename = _pick_workbook(sources, "parts", issues)
    if path is None:
        if not any(i.code == "AMBIGUOUS_INPUT" for i in issues):
            # 检查是否存在不支持的附件
            if sources:
                issues.append(Issue(
                    code="UNSUPPORTED_FILE",
                    message="附件中未找到配件发票工作簿（CI-For Dealer）",
                    required_action="请补充含 CI-For Dealer (2) 的 xlsx 或 xls 附件"))
            else:
                issues.append(Issue(
                    code="MISSING_SOURCE",
                    message="缺少配件发票附件",
                    required_action="请补充配件发票 xlsx 或 xls 附件"))
        return _build_response("waiting_input", "parts", [], issues, [], [],
                               None, _content_hash("parts", destination, []))
    try:
        parsed = parse_parts_workbook(path, filename, aliases=header_aliases)
    except UnsupportedSourceError as exc:
        return _build_response("unsupported", "parts", [],
                               [Issue(code="UNSUPPORTED_FILE", message=str(exc),
                                      required_action="请补充 xlsx 或 xls 格式附件")],
                               [], [], None, _content_hash("parts", destination, []))
    issues.extend(parsed.issues)

    if destination is None:
        issues.append(Issue(
            code="MISSING_DESTINATION", field="destination",
            message="缺少目的国，台账最终目的国列无法填写",
            required_action="请回复 目的国：<国家名>"))

    record_map = {r.part_code: r.fields for r in parsed.records}
    _apply_overrides(record_map, overrides, issues)

    rows, json_records, provenance, normalizations = build_parts_rows(
        parsed.records, country_index, destination,
        parsed.gross_weight, parsed.packages, parsed.invoice_no,
        parsed.gross_source, parsed.packages_source, parsed.invoice_source,
        issues)

    status = _finalize_status(issues)
    artifacts = []
    if status == "ready":
        try:
            artifacts.append(write_output(
                str(_template_dir()), PARTS_LEDGER_SPEC, rows, output_dir,
                OUTPUT_NAMES["parts_ledger"]))
        except TemplateWriteError as exc:
            issues.append(Issue(code="TEMPLATE_ERROR", message=str(exc),
                                required_action="请联系管理员检查模板"))
            status = "waiting_input"
            artifacts = []
    identity = parsed.invoice_no
    layout = _layout_block("parts", {
        "sheets": [t.get("sheet") for t in (parsed.layout.get("parts_invoice", {}),)
                   if isinstance(t, dict) and t.get("sheet")]
                   + [t.get("sheet") for t in (parsed.layout.get("packing_total", {}),)
                      if isinstance(t, dict) and t.get("sheet")],
        "parts_invoice": parsed.layout.get("parts_invoice", {}),
        "packing_total": parsed.layout.get("packing_total", {}),
        "checks": parsed.layout.get("checks", []),
        # 自动窄规范化（如 1|| → 1|）与实际值证据随版本保存，供“实际修正”
        # 节展示；不参与结构指纹（canonical_structure 只读定位键）。
        "corrections": normalizations,
    }, request_aliases=raw_aliases)
    return _build_response(status, "parts", json_records, issues, artifacts,
                           provenance, identity,
                           _content_hash("parts", destination, json_records),
                           layout=layout)


def _process_machine(task_id, destination, sources, mail, country_index,
                     machine_index, output_dir, overrides, issues,
                     header_aliases, raw_aliases=None) -> dict:
    path, filename = _pick_workbook(sources, "machine", issues)
    wb_result = None
    if path is not None:
        try:
            wb_result = parse_machine_workbook(path, filename,
                                               aliases=header_aliases)
        except UnsupportedSourceError as exc:
            return _build_response("unsupported", "machine", [],
                                   [Issue(code="UNSUPPORTED_FILE", message=str(exc),
                                          required_action="请补充 xlsx 或 xls 格式附件")],
                                   [], [], None, _content_hash("machine", destination, []))
        issues.extend(wb_result.issues)
    else:
        issues.append(Issue(
            code="MISSING_SOURCE",
            message="缺少整机发票箱单附件，净重、序列号等无法取得",
            required_action="请补充 Invoice+Packing list（或 CI/PL）xlsx 或 xls 附件"))

    mail_result = None
    if mail.get("html"):
        mail_result = parse_mail_html(mail["html"], mail.get("message_id"))
    elif mail.get("text"):
        mail_result = parse_mail_text(mail["text"], mail.get("message_id"))
    if mail_result is not None:
        issues.extend(mail_result.issues)
    mail_rows = mail_result.rows if mail_result else []
    if not mail_rows and (mail_result is None or mail_result.tables_seen == 0):
        issues.append(Issue(
            code="MISSING_SOURCE",
            message="邮件正文未解析到业务表（Cust. Purch. Order (MSO#)），Dealer/价格等无法取得",
            required_action="请补充包含业务表的邮件正文"))

    invoice_rows = wb_result.invoice_rows if wb_result else []
    packing_rows = wb_result.packing_rows if wb_result else []
    weight_groups = wb_result.weight_groups if wb_result else []

    rec_result = reconcile_machines(mail_rows, invoice_rows, packing_rows,
                                    weight_groups, machine_index=machine_index)
    issues.extend(rec_result.issues)

    record_map = {r.mso: r.fields for r in rec_result.records}
    _apply_overrides(record_map, overrides, issues)

    material_rows, comm_rows, json_records, provenance = build_machine_rows(
        rec_result.records, machine_index, destination, issues)

    # 行数一致性：两个输出必须同时完整，否则暂停整票
    if len(material_rows) != len(comm_rows):
        material_rows, comm_rows = [], []

    status = _finalize_status(issues)
    if status == "ready" and (not material_rows or not comm_rows):
        issues.append(Issue(
            code="NO_RECORDS",
            message="整机未解析到任何完整机器记录，零条有效记录不交付",
            required_action="请补充业务资料"))
        status = "waiting_input"

    artifacts = []
    if status == "ready":
        try:
            artifacts.append(write_output(
                str(_template_dir()), MACHINE_MATERIALS_SPEC, material_rows,
                output_dir, OUTPUT_NAMES["machine_materials"]))
            artifacts.append(write_output(
                str(_template_dir()), MACHINE_COMM_SPEC, comm_rows,
                output_dir, OUTPUT_NAMES["machine_comm"]))
        except TemplateWriteError as exc:
            issues.append(Issue(code="TEMPLATE_ERROR", message=str(exc),
                                required_action="请联系管理员检查模板"))
            status = "waiting_input"
            artifacts = []

    msos = sorted(r.mso for r in rec_result.records)
    invoice_no = wb_result.invoice_no if wb_result else None
    identity = "|".join(msos) + (f"|{invoice_no}" if invoice_no else "") if msos else None
    layout = {}
    if wb_result is not None:
        layout = _layout_block("machine", {
            "sheets": sorted({t.get("sheet") for t in
                              wb_result.layout.get("machine_table", [])
                              + wb_result.layout.get("weight_table", [])
                              if isinstance(t, dict) and t.get("sheet")}),
            "machine_table": wb_result.layout.get("machine_table", []),
            "weight_table": wb_result.layout.get("weight_table", []),
            "checks": wb_result.layout.get("checks", []),
        }, request_aliases=raw_aliases)
    return _build_response(status, "machine", json_records, issues, artifacts,
                           provenance, identity,
                           _content_hash("machine", destination, json_records),
                           layout=layout)


def import_machine_reference(path: str) -> list:
    """从机型库工作簿 Sheet1 导入机型申报资料（不读 Sheet2）。

    字段：B→机型、C→英文品名、D→商品编码、E→商品名称、F→申报要素，
    A/G 保留为 special/verification 备注，另附 source_row（Sheet1 行号）溯源。
    每条增加 destination_scope：A 列为国家标记（如 巴西）时映射为该国 alpha3，
    否则为 "default"。同一机型可保留多条不同 scope 的冲突记录
    （用户已确认：915F 目的国 BRA 用 A=巴西 的 123KW 记录，其余国家用
    A 空的 110KW 记录）；同一 (机型, scope) 内申报资料仍不同才抛错，
    绝不最后一行覆盖。匹配时由 core 按 request.destination.alpha3 完成
    （references.MachineIndex），app 只需透传全部条目。
    注意：特殊规则仅 915F 获用户确认；其他带 A 列标记的机型（当前为
    SEM919BRZ）原样保留条目（special_rule_confirmed=False），匹配时
    暂停并登记 UNCONFIRMED_SPECIAL_RULE，不推广猜测规则。
    """
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    if "Sheet1" not in wb.sheetnames:
        wb.close()
        raise ValueError(f"机型库缺少 Sheet1：{path}")
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    countries = CountryIndex()
    CONFIRMED_SPECIAL_MODELS = {"915F"}   # 用户已确认特殊规则的机型，勿扩大
    entries: dict = {}
    for row_no, row in enumerate(rows[1:], start=2):
        model = row[1] if len(row) > 1 else None
        if model in (None, ""):
            continue
        model = str(model).strip()
        special = (str(row[0]).strip() if len(row) > 0 and row[0] else "")
        if special:
            scope = countries.resolve(special)
            if not scope:
                raise ValueError(
                    f"机型库第 {row_no} 行：机型 {model!r} 的特殊配置标记 "
                    f"{special!r} 无法映射为国家，请维护为国家名/别名后重新导入")
        else:
            scope = "default"
        hs = row[3] if len(row) > 3 else None
        if hs is not None and not isinstance(hs, str):
            hs = str(int(hs)) if float(hs).is_integer() else str(hs)
        entry = {
            "model": model,
            "english_name": (str(row[2]).strip() if len(row) > 2 and row[2] else ""),
            "hs_code": (str(hs).strip() if hs else ""),
            "product_name": (str(row[4]).strip() if len(row) > 4 and row[4] else ""),
            "declaration": (str(row[5]).strip() if len(row) > 5 and row[5] else ""),
            "special": special,
            "verification": (str(row[6]).strip() if len(row) > 6 and row[6] else ""),
            "destination_scope": scope,
            "source_row": row_no,
            "special_rule_confirmed": (not special) or (model in CONFIRMED_SPECIAL_MODELS),
        }
        key = (model, scope)
        if key in entries:
            prev = entries[key]
            differing = [k for k in ("english_name", "hs_code", "product_name", "declaration")
                         if prev[k] != entry[k]]
            if differing:
                raise ValueError(
                    f"机型库重复机型且申报资料不同：{model!r} scope={scope} "
                    f"（第 {prev['source_row']} 行 vs 第 {row_no} 行）差异字段 {differing}。"
                    "请维护为不同 scope 的特殊配置条目后重新导入")
            continue
        entries[key] = entry
    return list(entries.values())
