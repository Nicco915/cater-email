"""业务转换核心公共接口（plan/execution.md 公共接口 v1）。

- ``process_job(request: dict) -> dict``：配件/整机离线转换。
  永不发送邮件、不写业务数据库；输入输出均为 JSON 可序列化 dict。
- 附件支持 .xlsx 与真正 BIFF .xls（统一只读工作簿接口，见
  parsing/workbook.py）；``header_aliases`` 可按字段配置表头别名。
- 响应 ``layout`` 携带采用工作表、结构定位、已执行核对与规则 ID，
  供上层做布局确认与回复正文展示（不含正文/秘密）。
- ``import_machine_reference(path: str) -> list[dict]``：仅读机型库 Sheet1，
  供应用初始化本地机型表。
- ``inspect_sources(request: dict) -> dict``：多票分票预览（阶段 1，见
  split_preview.py）。只读识别工作簿业务区域与身份、关联正文业务行，
  返回候选与冲突；不写模板、不改 ``process_job`` 单票契约。
- 显式 ``confirmed_input``（阶段 3）：仅当请求携带经应用层可信哈希
  核验的已确认快照负载时才走已确认解析路径（machine_confirmed /
  kit_confirmed），sources 范围/完整性/schema 无法验证即拒绝
  （``ConfirmedInputError``），绝不落回全父邮件；旧单票契约不变。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import openpyxl

from .models import FieldValue, Issue, Source
from .parsing.headers import (alias_marker as headers_alias_marker,
                              merged_aliases, normalize_header)
from .parsing.mail_html import (MAIL_DEFAULT_ALIASES, MailRow,
                                check_alias_collisions,
                                mail_aliases, parse_mail_html, parse_mail_text)
from .parsing.machine_wb import (INVOICE_NAME_KEYS, PACKING_NAME_KEYS,
                                 parse_machine_workbook, sniff_machine_workbook)
from .parsing.parts import parse_parts_workbook
from .parsing.workbook import (SUPPORTED_SUFFIXES, UnsupportedSourceError,
                               WorkbookDependencyError, load_workbook_pair)
from .numbertext import normalize_identifier
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


class ConfirmedInputError(Exception):
    """已确认输入契约违反：schema/身份/完整性无法验证即拒绝处理。

    属于技术拒绝（疑似篡改或拼装负载），不是业务等待；抛出前不生成任何
    文件，调用方按失败如实记录，绝不落回全父邮件或旧单票路径继续。
    """


CONFIRMED_INPUT_SCHEMA = 1


def _confirmed_fail(message: str) -> None:
    raise ConfirmedInputError(f"已确认输入拒绝：{message}")


def _source_file_hash(source: dict, index: int) -> str:
    """重新计算源文件内容哈希（绝不信任调用方随附的 sha256 字段）。"""
    raw_path = source.get("path") if isinstance(source, dict) else None
    if not isinstance(raw_path, str) or not raw_path.strip():
        _confirmed_fail(f"sources[{index}] 缺少有效路径")
    path = Path(raw_path)
    try:
        if not path.is_file():
            _confirmed_fail(f"sources[{index}] 文件缺失：{path.name}")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        _confirmed_fail(f"sources[{index}] 文件不可读：{type(exc).__name__}")


def _validate_confirmed_input(confirmed, sources: list) -> tuple[dict, list]:
    """校验显式 confirmed_input 的 schema、候选身份与本票 sources 完整性。

    通过后返回 (candidate, mail_rows)。任何结构/范围/完整性不符即抛
    :class:`ConfirmedInputError`：确认冲突的候选不能靠传 confirmed_input
    跳过人工确认；sources 必须与候选 source_refs 精确一致（按内容哈希，
    不重不漏），防止借 confirmed_input 夹带或替换材料。
    """
    if not isinstance(confirmed, dict):
        _confirmed_fail("confirmed_input 必须是对象")
    schema = confirmed.get("schema_version")
    if isinstance(schema, bool) or schema != CONFIRMED_INPUT_SCHEMA:
        _confirmed_fail(f"不支持的 schema_version={schema!r}"
                        f"（仅支持 {CONFIRMED_INPUT_SCHEMA}）")
    candidate = confirmed.get("candidate")
    if not isinstance(candidate, dict):
        _confirmed_fail("candidate 缺失或不是对象")
    kind = candidate.get("kind")
    if kind not in ("machine", "parts"):
        _confirmed_fail(f"候选业务类型非法：{kind!r}（仅支持 machine/parts）")
    if candidate.get("needs_confirmation"):
        _confirmed_fail("候选仍处于待人工确认状态，不能按已确认路径执行")
    cand_issues = candidate.get("issues")
    if cand_issues is not None:
        if not isinstance(cand_issues, list) or any(
                not isinstance(i, dict) for i in cand_issues):
            _confirmed_fail("candidate.issues 结构非法")
        blocking = [i for i in cand_issues if i.get("blocking", True)]
        if blocking:
            codes = ", ".join(sorted({str(i.get("code")) for i in blocking}))
            _confirmed_fail(f"候选仍存在阻断问题（{codes}），不能按已确认路径执行")
    refs = candidate.get("source_refs")
    if not isinstance(refs, list) or not refs:
        _confirmed_fail("候选缺少 source_refs 资产清单")
    ref_hashes: list[str] = []
    for index, ref in enumerate(refs):
        sha = ref.get("sha256") if isinstance(ref, dict) else None
        if not isinstance(sha, str) or len(sha.strip()) != 64:
            _confirmed_fail(f"候选 source_refs[{index}] 的 sha256 缺失或非法")
        ref_hashes.append(sha.strip().lower())
    mail_rows = confirmed.get("mail_rows")
    if not isinstance(mail_rows, list) or any(
            not isinstance(r, dict) for r in mail_rows):
        _confirmed_fail("mail_rows 缺失或结构非法")
    for index, ref in enumerate(mail_rows):
        fields = ref.get("fields")
        if not isinstance(fields, dict) or any(
                not isinstance(name, str) or not name
                or not isinstance(value, str)
                for name, value in fields.items()):
            _confirmed_fail(f"mail_rows[{index}].fields 结构非法")
        if not normalize_identifier(fields.get("mso")) and \
                not normalize_identifier(fields.get("serial_number")):
            _confirmed_fail(
                f"mail_rows[{index}] 缺少 MSO/序列号，正文行身份不完整")
    fingerprint = confirmed.get("source_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        _confirmed_fail("source_fingerprint 缺失或不是非空字符串")
    got_hashes = sorted(_source_file_hash(src, i)
                        for i, src in enumerate(sources))
    if got_hashes != sorted(ref_hashes):
        _confirmed_fail("sources 与已确认候选的资产清单不一致"
                        "（缺失、夹带或内容被篡改），已拒绝继续")
    return candidate, mail_rows


def _confirmed_mail_rows(confirmed_rows: list, mail: dict) -> list:
    """由快照冻结的已确认正文行直接构建 reconcile 输入（含来源定位）。

    已确认路径不解析请求中的 mail 负载取内容：仅序列号票据的规范表可能
    根本没有 MSO 列，无法被正文业务表识别；而冻结行本身就是经应用层可
    信哈希核验的语义字段真值。直接使用冻结字段可在结构上杜绝 mail 负载
    拼装/注入生效（篡改它对输出零影响），同时完整保留原表/行号溯源。
    返回的是行副本（随后可做唯一配对 MSO 补认），可信快照绝不被修改。
    """
    mail = mail or {}
    source_type = "mail_html" if mail.get("html") else "mail_text"
    rows: list = []
    for ref in confirmed_rows:
        fields = {str(name): str(value)
                  for name, value in ref["fields"].items()
                  if value not in (None, "")}
        rows.append(MailRow(fields=fields, source=Source(
            type=source_type, message_id=mail.get("message_id"),
            table_index=ref.get("table_index"), row=ref.get("row"))))
    return rows


def _canonical_confirmed_row(fields) -> tuple:
    """正文行字段的规范化可比较形态：丢弃空值，键值全部字符串化。"""
    if not isinstance(fields, dict):
        _confirmed_fail("已确认正文行的 fields 结构非法")
    return tuple(sorted(
        (str(name), str(value)) for name, value in fields.items()
        if value not in (None, "")))


def _check_confirmed_mail(carrier_rows: list, confirmed_rows: list) -> None:
    """正文载体的防拼装核对：可解析出业务行时必须与冻结行逐字段一致。

    核对先于任何 MSO 补认，且始终针对快照冻结的原始字段。仅序列号票据
    的规范表可能没有 MSO 列、根本不被业务表识别（零行），此时不消费载体
    也不放行其内容——输出只由冻结行驱动（见 _confirmed_mail_rows），篡改
    载体对输出零影响；一旦载体可解析（含 MSO 列空单元格的正常票据），
    夹带/删改任何行或字段都会被此处拒绝。
    """
    if not carrier_rows:
        return
    parsed = sorted(_canonical_confirmed_row(r.fields) for r in carrier_rows)
    confirmed = sorted(_canonical_confirmed_row(r.get("fields"))
                       for r in confirmed_rows)
    if parsed != confirmed:
        _confirmed_fail("邮件正文与已确认正文行不一致（疑似篡改或拼装的 "
                        "mail 负载），已拒绝继续")


def _load_confirmed_machine_parser():
    try:
        from .parsing.machine_confirmed import parse_confirmed_machine_sources
    except ImportError as exc:
        raise ConfirmedInputError(
            f"已确认整机解析组件不可用：{exc}") from exc
    return parse_confirmed_machine_sources


def _load_confirmed_kit_parser():
    try:
        from .parsing.kit_confirmed import parse_confirmed_kit_sources
    except ImportError as exc:
        raise ConfirmedInputError(
            f"已确认配件解析组件不可用：{exc}") from exc
    return parse_confirmed_kit_sources


def _adopt_confirmed_msos(mail_rows: list, wb_result, issues: list) -> None:
    """已确认整机路径：仅序列号正文行按已确认工作簿的唯一机器配对补认 MSO。

    序列号 → MSO 的映射只来自已确认解析器（B）返回的本票机器清单；仅当
    序列号唯一对应一个 MSO 时才在解析得到的行副本上补认（绝不修改可信
    快照，行来源保持邮件来源不变）。无匹配/多候选/匹配机器无 MSO 一律
    显式阻断，绝不默认取第一台。
    """
    by_serial: dict[str, dict[str, str]] = {}
    for row in (list(wb_result.invoice_rows or [])
                + list(wb_result.packing_rows or [])):
        serial = normalize_identifier(getattr(row, "loader", None))
        mso = getattr(row, "mso", None)
        if not serial or not mso:
            continue
        by_serial.setdefault(serial, {})[normalize_identifier(mso)] = str(mso)
    for row in mail_rows:
        if normalize_identifier(row.fields.get("mso")):
            continue  # 已有 MSO：一致性由 reconcile 正式核对
        serial = normalize_identifier(row.fields.get("serial_number"))
        if not serial:
            continue
        matches = by_serial.get(serial, {})
        if len(matches) == 1:
            row.fields["mso"] = next(iter(matches.values()))
            continue
        if not matches:
            issues.append(Issue(
                code="MAIL_ROW_SERIAL_UNMATCHED",
                message=f"正文行序列号 {serial} 在已确认工作簿机器清单中"
                        "不存在，无法补认 MSO",
                sources=[row.source],
                required_action="请核对正文行序列号与本票机器清单"))
        else:
            issues.append(Issue(
                code="MAIL_ROW_SERIAL_AMBIGUOUS",
                message=f"正文行序列号 {serial} 在已确认工作簿中对应多个 MSO "
                        f"{sorted(matches)}，无法唯一补认，未采用任何一台",
                sources=[row.source],
                required_action="请核对序列号与 MSO 的对应关系"))


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


def _probe_workbook(path: str) -> tuple[Optional[list], Optional[Exception]]:
    """探测工作簿可读性与 sheet 键；区分“读不了”和“读得到但没有业务表”。

    返回 (规范化 sheet 键列表, 读取错误)。错误保留有限信息：
    WorkbookDependencyError = 运行环境缺依赖（部署问题，附件本身有效）；
    UnsupportedSourceError = 格式不支持/损坏/加密（输入问题）。
    """
    if not str(path).lower().endswith(SUPPORTED_SUFFIXES):
        return None, UnsupportedSourceError(
            f"不支持的附件格式：{Path(str(path)).name}（仅支持 .xlsx 与 .xls）")
    try:
        wb, _ = load_workbook_pair(path)
        names = [normalize_header(n) for n in wb.sheetnames]
        if hasattr(wb, "close"):
            wb.close()
        return names, None
    except Exception as exc:
        return None, exc


def _workbook_sheet_keys(path: str) -> Optional[list]:
    """统一只读接口取 sheet 名列表（规范化小写键）；无法读取返回 None。"""
    names, _error = _probe_workbook(path)
    return names


def _classify(sources: list, mail: dict, aliases=None) -> str:
    """kind=auto 时的业务识别：配件看 CI-For Dealer 表，整机看业务表别名、
    机器信息表/重量表结构（sheet 重命名也可识别）。

    至少一个附件因环境缺依赖（WorkbookDependencyError）读不了时，返回
    内部哨兵 "dependency_blocked"，由 process_job 暴露真实依赖错误，
    而不是笼统报“无法识别业务类型”把部署问题吞掉。"""
    dependency_blocked = False
    for src in sources:
        names, error = _probe_workbook(src.get("path", ""))
        if isinstance(error, WorkbookDependencyError):
            dependency_blocked = True
        if names and any(n.startswith("cifordealer") for n in names):
            return "parts"
    for src in sources:
        names, _error = _probe_workbook(src.get("path", ""))
        if names and (INVOICE_NAME_KEYS & set(names) or PACKING_NAME_KEYS & set(names)):
            return "machine"
    for src in sources:
        names, _error = _probe_workbook(src.get("path", ""))
        if names is not None:
            # 名称未命中但结构命中（已确认的布局灵活性：业务表可重命名）；
            # 结构探测使用调用方配置的别名（与解析阶段同一套）。
            if sniff_machine_workbook(src.get("path", ""), aliases):
                return "machine"
    if dependency_blocked:
        return "dependency_blocked"
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
    """别名集标记（实现已统一到 parsing.headers.alias_marker）。"""
    return headers_alias_marker(request_aliases)


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
    # 邮件别名与工作簿别名是两个命名空间（同一归一键可各归各的语义字
    # 段），冲突检测只在邮件别名内部做。
    alias_conflicts = check_alias_collisions(
        mail_aliases(), scope=set(MAIL_DEFAULT_ALIASES))
    if alias_conflicts:
        return _build_response(
            "waiting_input", "unsupported", [],
            [Issue(code="ALIAS_CONFIG_CONFLICT", message="；".join(alias_conflicts),
                   required_action="请维护人员修正 [core.header_aliases] 中映射"
                                   "到多个字段的同名别名后重试")],
            [], [], None, _content_hash("unsupported", destination, []))

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

    confirmed_input = request.get("confirmed_input")
    if confirmed_input is not None:
        # 阶段 3 显式已确认路径：先完整验证契约（schema/候选/本票资产完整
        # 性），再按候选类型走已确认解析器（B/C），随后仍走既有
        # reconcile/transform/模板输出与全部正式校验。任何验证失败直接
        # 抛出 ConfirmedInputError 拒绝，绝不落回全父邮件或旧单票路径。
        return _process_confirmed(
            confirmed_input, task_id=task_id, kind=kind,
            destination=destination, sources=sources, mail=mail,
            country_index=country_index, machine_index=machine_index,
            output_dir=output_dir, overrides=overrides, issues=issues,
            header_aliases=header_aliases,
            raw_aliases=request.get("header_aliases"))

    if kind == "auto":
        kind = _classify(sources, mail, header_aliases)
    if kind == "dependency_blocked":
        # 分类未完成是因为运行环境缺依赖（如 xlrd）：暴露真实依赖错误，
        # 而不是笼统报“无法识别业务类型”。附件本身有效，无需重发。
        for src in sources:
            _names, error = _probe_workbook(src.get("path", ""))
            if not isinstance(error, WorkbookDependencyError):
                continue
            filename = src.get("filename") or Path(str(src.get("path", ""))).name
            issues.append(Issue(
                code="WORKBOOK_DEPENDENCY",
                message=f"附件 {filename} 已收到，但运行环境缺少解析所需组件："
                        f"{error}",
                sources=[Source(type="workbook", filename=filename)],
                required_action="附件无需重发；请联系维护人员在运行环境"
                                "安装缺失组件后重试本任务",
            ))
        return _build_response("waiting_input", "unsupported", [], issues, [], [],
                               None, _content_hash("unsupported", destination, []))
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
                            header_aliases, request.get("header_aliases"),
                            supplement_mails=request.get("supplement_mails"),
                            supplement_mail=request.get("supplement_mail"))


def tempfile_get() -> str:
    import tempfile
    return tempfile.gettempdir()


def _pick_workbook(sources: list, want: str, issues: list,
                   aliases=None) -> tuple[Optional[str], Optional[str], str]:
    """从 sources 中挑选目标工作簿；返回 (path, filename, state)。

    state ∈ {"picked", "absent", "unreadable", "unrecognized", "ambiguous"}：
    - absent：未附任何工作簿（由调用方登记 MISSING_SOURCE）；
    - unreadable：附件在但读不了——依赖缺失（WORKBOOK_DEPENDENCY，指向
      维护人员）或格式损坏/不支持（UNSUPPORTED_FILE，指向重发修正件）；
    - unrecognized：可读但其中没有目标业务表（UNSUPPORTED_FILE，
      “未找到…工作表”，与“缺少附件”区分）；
    - ambiguous：多个附件都符合（AMBIGUOUS_INPUT，绝不同时报缺少附件）。

    整机挑选先按业务表名（Invoice/CI/Packing list/PL），再对每一个名称未
    命中但可读的附件做结构探测（已确认的布局灵活性，sheet 可重命名）：结构
    命中的与名称命中的同为候选，一起参与内容去重与歧义判定。读得到但不认识
    的保持 conservative：不猜测、不静默忽略，归入 unrecognized 暂停。
    """
    matches: list[dict] = []
    readable_unmatched: list[dict] = []
    unreadable: list[tuple[dict, Exception]] = []
    for src in sources:
        names, error = _probe_workbook(src.get("path", ""))
        if error is not None:
            unreadable.append((src, error))
            continue
        if names is None:
            continue
        if want == "parts" and any(n.startswith("cifordealer") for n in names):
            matches.append(src)
        elif want == "machine" and (INVOICE_NAME_KEYS & set(names)
                                    or PACKING_NAME_KEYS & set(names)):
            matches.append(src)
        else:
            readable_unmatched.append(src)
    if want == "machine":
        # 逐个结构探测名称未命中的可读附件（多附件同样探测，不能只挑一个
        # 或整体跳过）：重命名后的合格附件必须与名称命中的附件一起参与
        # 去重/歧义判定，不得静默忽略第二候选。
        for candidate in readable_unmatched:
            if sniff_machine_workbook(candidate.get("path", ""), aliases):
                matches.append(candidate)
    # 相同字节内容（同名再传或换名再传）只算一个候选：保留全部输入
    # 记录（审计/来源不变），但处理与歧义判定按内容去重。
    distinct: dict[str, dict] = {}
    for src in matches:
        try:
            digest = hashlib.sha256(Path(str(src["path"])).read_bytes()).hexdigest()
        except OSError:
            digest = f"unreadable:{src.get('path')}"
        distinct.setdefault(digest, src)
    matches = list(distinct.values())
    if len(matches) > 1:
        issues.append(Issue(
            code="AMBIGUOUS_INPUT",
            message=f"多个附件都包含目标工作表：{[m.get('filename') for m in matches]}",
            required_action="多份内容不同的附件都符合本票要求，无法自动选定；"
                            "本次未采用任何一份，已保存的附件不会被删除或改动。"
                            "请联系维护人员确认应使用的附件后再继续本任务",
        ))
        return None, None, "ambiguous"
    if matches:
        src = matches[0]
        return (src.get("path"),
                src.get("filename") or Path(src["path"]).name,
                "picked")
    if not sources:
        return None, None, "absent"
    for src, error in unreadable:
        filename = src.get("filename") or Path(str(src.get("path", ""))).name
        if isinstance(error, WorkbookDependencyError):
            issues.append(Issue(
                code="WORKBOOK_DEPENDENCY",
                message=f"附件 {filename} 已收到，但运行环境缺少解析所需组件："
                        f"{error}",
                sources=[Source(type="workbook", filename=filename)],
                required_action="附件无需重发；请联系维护人员在运行环境"
                                "安装缺失组件后重试本任务",
            ))
        elif isinstance(error, UnsupportedSourceError):
            issues.append(Issue(
                code="UNSUPPORTED_FILE",
                message=f"附件 {filename} 无法读取：{error}",
                sources=[Source(type="workbook", filename=filename)],
                required_action="请补充未加密、未损坏的 xlsx 或 xls 附件后重发",
            ))
        else:  # 未知读取错误：保留有限信息，不吞成“缺少附件”
            issues.append(Issue(
                code="UNSUPPORTED_FILE",
                message=f"附件 {filename} 读取失败：{type(error).__name__}",
                sources=[Source(type="workbook", filename=filename)],
                required_action="请补充未加密、未损坏的 xlsx 或 xls 附件后重发",
            ))
    if readable_unmatched:
        filenames = [m.get("filename") or Path(str(m.get("path", ""))).name
                     for m in readable_unmatched]
        hint = ("配件发票工作簿（CI-For Dealer）" if want == "parts"
                else "整机发票/箱单工作表（Invoice/CI/Packing list/PL，"
                     "或机器信息表/重量表结构）")
        issues.append(Issue(
            code="UNSUPPORTED_FILE",
            message=f"附件 {filenames} 已读取，但未找到{hint}",
            sources=[Source(type="workbook", filename=f) for f in filenames],
            required_action="请确认附件为正确的业务工作簿后重发",
        ))
        return None, None, "unrecognized"
    if unreadable:
        return None, None, "unreadable"
    return None, None, "absent"


def _process_parts(task_id, destination, sources, country_index,
                   output_dir, overrides, issues, header_aliases,
                   raw_aliases=None) -> dict:
    path, filename, pick_state = _pick_workbook(sources, "parts", issues,
                                                aliases=header_aliases)
    if path is None:
        # absent：未附任何工作簿；其余状态（unreadable/unrecognized/
        # ambiguous）已由挑选阶段登记具体问题，不再笼统报“缺少附件”。
        if pick_state == "absent":
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
    return _finish_parts(task_id, destination, parsed, country_index,
                         output_dir, overrides, issues, raw_aliases)


def _finish_parts(task_id, destination, parsed, country_index,
                  output_dir, overrides, issues, raw_aliases=None) -> dict:
    """配件处理公共尾段（旧单票与已确认路径共用）：正式核对、修正、
    行构建、状态判定、模板输出与布局/溯源响应。"""
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
                     header_aliases, raw_aliases=None,
                     supplement_mails=None, supplement_mail=None) -> dict:
    path, filename, pick_state = _pick_workbook(sources, "machine", issues,
                                                aliases=header_aliases)
    wb_result = None
    if path is not None:
        try:
            wb_result = parse_machine_workbook(path, filename,
                                               aliases=header_aliases)
        except WorkbookDependencyError as exc:
            issues.append(Issue(
                code="WORKBOOK_DEPENDENCY",
                message=f"附件 {filename} 已收到，但运行环境缺少解析所需组件："
                        f"{exc}",
                sources=[Source(type="workbook", filename=filename)],
                required_action="附件无需重发；请联系维护人员在运行环境"
                                "安装缺失组件后重试本任务",
            ))
            return _build_response("waiting_input", "machine", [], issues,
                                   [], [], None,
                                   _content_hash("machine", destination, []))
        except UnsupportedSourceError as exc:
            return _build_response("unsupported", "machine", [],
                                   [Issue(code="UNSUPPORTED_FILE", message=str(exc),
                                          required_action="请补充 xlsx 或 xls 格式附件")],
                                   [], [], None, _content_hash("machine", destination, []))
        issues.extend(wb_result.issues)
    elif pick_state == "absent":
        # 仅在确实未附工作簿时才报缺少附件；附件在但读不了/不匹配/多附件
        # 已由挑选阶段登记具体问题（WORKBOOK_DEPENDENCY/UNSUPPORTED_FILE/
        # AMBIGUOUS_INPUT），不得再误报“缺少附件”。
        issues.append(Issue(
            code="MISSING_SOURCE",
            message="缺少整机发票箱单附件，净重、序列号等无法取得",
            required_action="请补充 Invoice+Packing list（或 CI/PL）xlsx 或 xls 附件"))
    elif pick_state == "ambiguous":
        # 多份内容不同的附件都符合：整票暂停，只报告歧义本身（候选文件名
        # 与解决方式已由挑选阶段写入）。附件未定稿前不解析邮件、不对账，
        # 避免误导性的“缺字段/缺附件/请修正数值”提示。
        return _build_response("waiting_input", "machine", [], issues, [], [],
                               None, _content_hash("machine", destination, []))

    mail_result = None
    if mail.get("html"):
        mail_result = parse_mail_html(mail["html"], mail.get("message_id"),
                                      aliases=header_aliases)
    elif mail.get("text"):
        mail_result = parse_mail_text(mail["text"], mail.get("message_id"),
                                      aliases=header_aliases)
    mail_rows = mail_result.rows if mail_result else []

    # 补件邮件业务表（当前版本累积的补充来源邮件，非引用历史）：与原始
    # 邮件表在 reconcile 中按 MSO 显式合并（填充缺失/一致去重/冲突暂停带
    # 溯源）。多封按接收顺序依次合并；只影响其 MSO 对应的记录。
    supplement_rows: list = []
    supplement_payloads = []
    if supplement_mail:
        supplement_payloads.append(supplement_mail)
    supplement_payloads.extend(supplement_mails or [])
    for supplement_payload in supplement_payloads:
        supplement_result = None
        if supplement_payload.get("html"):
            supplement_result = parse_mail_html(
                supplement_payload["html"], supplement_payload.get("message_id"),
                aliases=header_aliases)
        elif supplement_payload.get("text"):
            supplement_result = parse_mail_text(
                supplement_payload["text"], supplement_payload.get("message_id"),
                aliases=header_aliases)
        if supplement_result is not None:
            issues.extend(supplement_result.issues)
            supplement_rows.extend(supplement_result.rows)

    # 原始业务表结构不可用（多列歧义/多版不一致/合并单元格越界）时，若
    # 存在干净的补件完整业务表，则显式按补件表继续，而不是永远卡在原始
    # 表的结构问题上：原始表的结构问题留痕（非阻断）后剔除其行，缺失字段
    # 仍由 reconcile 的 MISSING_FIELD 如实暂停。原始表与补件表都给出真实
    # 值时仍走 FIELD_CONFLICT，绝不静默覆盖。
    if mail_result is not None:
        structural = {"AMBIGUOUS_COLUMN", "MULTIPLE_BUSINESS_TABLES",
                      "TABLE_STRUCTURE_INVALID"}
        blocking_structural = [i for i in mail_result.issues
                               if i.code in structural and i.blocking]
        if blocking_structural and not mail_rows and supplement_rows:
            issues.append(Issue(
                code="TABLE_SUPERSEDED",
                message="原始邮件业务表结构无法使用，已按补件回复的完整业务表继续",
                sources=[i.sources[0] for i in blocking_structural if i.sources],
                required_action="",
                blocking=False,
            ))
            issues.extend(i for i in mail_result.issues
                          if i not in blocking_structural)
        else:
            issues.extend(mail_result.issues)

    return _finish_machine(task_id, destination, wb_result, mail_rows,
                           mail_result, supplement_rows, country_index,
                           machine_index, output_dir, overrides, issues,
                           raw_aliases)


def _finish_machine(task_id, destination, wb_result, mail_rows, mail_result,
                    supplement_rows, country_index, machine_index,
                    output_dir, overrides, issues, raw_aliases=None) -> dict:
    """整机处理公共尾段（旧单票与已确认路径共用）：缺表检查、reconcile、
    修正、行构建、状态判定、模板输出与布局/溯源响应。"""
    if not mail_rows and not supplement_rows and (
            mail_result is None or mail_result.tables_seen == 0):
        issues.append(Issue(
            code="MISSING_SOURCE",
            message="邮件正文未解析到业务表（含 Cust. Purch. Order/MSO 列），"
                    "Dealer/价格等无法取得",
            required_action="请回复包含完整业务表的邮件正文（HTML 表格或以 Tab/"
                            "空格明确分列的文本，保留表头与 MSO 列）"))

    invoice_rows = wb_result.invoice_rows if wb_result else []
    packing_rows = wb_result.packing_rows if wb_result else []
    weight_groups = wb_result.weight_groups if wb_result else []

    rec_result = reconcile_machines(mail_rows, invoice_rows, packing_rows,
                                    weight_groups, machine_index=machine_index,
                                    supplement_rows=supplement_rows)
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


def _process_confirmed(confirmed, *, task_id, kind, destination, sources,
                       mail, country_index, machine_index, output_dir,
                       overrides, issues, header_aliases,
                       raw_aliases=None) -> dict:
    """显式已确认路径：可信快照（应用层已验哈希）驱动的单票转换。

    - schema/候选/本票资产完整性由 :func:`_validate_confirmed_input` 核验，
      不符即抛 :class:`ConfirmedInputError`（技术拒绝，不生成任何文件）；
    - 工作簿解析走已确认解析器（machine_confirmed/kit_confirmed），按已
      确认身份与区域逐项核对，绝不把 preview candidate.facts 当明细真值；
    - 正文只取快照冻结的 mail_rows（经应用层可信哈希核验的语义字段），
      由 :func:`_confirmed_mail_rows` 直接构建 reconcile 输入并保留原表/
      行号溯源；随请求的正文载体只做防拼装核对（可解析出业务行时必须与
      冻结原始字段逐字段一致，见 :func:`_check_confirmed_mail`），绝不读
      父邮件补字段，也不走旧单票的附件挑选/歧义/补件邮件逻辑；
    - 仅序列号正文行先以冻结原始字段为基线，其 MSO 只能按已确认解析器
      返回的本票机器清单做唯一配对补认（无匹配/多候选显式阻断，绝不默认
      取第一台）；legacy 单票路径 require_mso 默认保持不变；
    - 之后的 reconcile、transform、模板 writer、status/layout/provenance
      及金额/HS 等正式校验与旧路径完全一致（共享公共尾段）。
    """
    candidate, confirmed_rows = _validate_confirmed_input(confirmed, sources)
    cand_kind = str(candidate.get("kind"))
    if kind == "auto":
        kind = cand_kind
    if kind not in ("parts", "machine") or cand_kind != kind:
        _confirmed_fail(f"请求业务类型 {kind!r} 与已确认候选 {cand_kind!r} 不一致")

    if kind == "parts":
        parse_confirmed = _load_confirmed_kit_parser()
        try:
            parsed = parse_confirmed(sources, candidate, confirmed_rows,
                                     aliases=header_aliases)
        except WorkbookDependencyError as exc:
            issues.append(Issue(
                code="WORKBOOK_DEPENDENCY",
                message=f"附件已确认，但运行环境缺少解析所需组件：{exc}",
                required_action="附件无需重发；请联系维护人员在运行环境"
                                "安装缺失组件后重试本任务",
            ))
            return _build_response("waiting_input", "parts", [], issues, [], [],
                                   None, _content_hash("parts", destination, []))
        except UnsupportedSourceError as exc:
            return _build_response("unsupported", "parts", [],
                                   [Issue(code="UNSUPPORTED_FILE", message=str(exc),
                                          required_action="请补充 xlsx 或 xls 格式附件")],
                                   [], [], None,
                                   _content_hash("parts", destination, []))
        return _finish_parts(task_id, destination, parsed, country_index,
                             output_dir, overrides, issues, raw_aliases)

    parse_confirmed = _load_confirmed_machine_parser()
    filename = next((str(s.get("filename")) for s in sources
                     if isinstance(s, dict) and s.get("filename")), "")
    try:
        wb_result = parse_confirmed(sources, candidate, confirmed_rows,
                                    aliases=header_aliases)
    except WorkbookDependencyError as exc:
        issues.append(Issue(
            code="WORKBOOK_DEPENDENCY",
            message=f"附件 {filename} 已确认，但运行环境缺少解析所需组件："
                    f"{exc}",
            sources=[Source(type="workbook", filename=filename)] if filename else [],
            required_action="附件无需重发；请联系维护人员在运行环境"
                            "安装缺失组件后重试本任务",
        ))
        return _build_response("waiting_input", "machine", [], issues,
                               [], [], None,
                               _content_hash("machine", destination, []))
    except UnsupportedSourceError as exc:
        return _build_response("unsupported", "machine", [],
                               [Issue(code="UNSUPPORTED_FILE", message=str(exc),
                                      required_action="请补充 xlsx 或 xls 格式附件")],
                               [], [], None,
                               _content_hash("machine", destination, []))
    issues.extend(wb_result.issues)

    mail_rows = _confirmed_mail_rows(confirmed_rows, mail)
    # 防拼装逐字段核对（先于任何补认，针对冻结原始字段）：正文载体可解析
    # 出业务行时必须与冻结行一致；仅序列号票的规范表无 MSO 列不可识别时
    # 不消费载体。legacy 路径 require_mso 默认保持不变。
    carrier_rows: list = []
    if mail.get("html"):
        carrier_rows = parse_mail_html(
            mail["html"], mail.get("message_id"),
            aliases=header_aliases, require_mso=False).rows
    elif mail.get("text"):
        carrier_rows = parse_mail_text(
            mail["text"], mail.get("message_id"),
            aliases=header_aliases, require_mso=False).rows
    _check_confirmed_mail(carrier_rows, confirmed_rows)
    # 仅序列号行：按 B 返回的唯一机器配对补认 MSO（多候选/无匹配阻断）。
    _adopt_confirmed_msos(mail_rows, wb_result, issues)
    return _finish_machine(task_id, destination, wb_result, mail_rows,
                           None, [], country_index, machine_index,
                           output_dir, overrides, issues, raw_aliases)


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


def inspect_sources(request: dict) -> dict:
    """多票分票预览入口（阶段 1）：识别附件业务区域/身份并关联正文业务行。

    委托 split_preview.inspect_sources；与 process_job 相同的只读工作簿
    加载与安全上限，不触碰模板、数据库、网络或任何写入路径。
    """
    from .split_preview import inspect_sources as _inspect

    return _inspect(request)
