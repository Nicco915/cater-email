"""已确认任务快照 -> 现有 ``process_job`` 请求的只读适配。

可信边界（调用方前置条件，本模块无法替代）
==========================================
调用方必须**先**执行 ``BatchService.verify_batch_task_snapshot(task_id)``
并确认返回 ``ok is True``（即快照重新读取后的规范化哈希等于任务落库时
持久化的可信 ``snapshot_hash``），然后再把该快照 JSON 解析结果与快照
文件路径传入本模块。本模块不接收、也不信任任何客户端提供的
``verified``/``ok`` 布尔值：它只在已通过可信哈希核验的快照上做二次
结构/业务一致性校验（schema、候选、本票资产清单、正文行、逐文件哈希、
路径边界），并不会自行证明“快照未经篡改”。因此：

- 传入的 ``snapshot`` 必须与 ``snapshot_path`` 指向的 ``snapshot.json``
  文件内容完全一致（本模块会重新读取并逐字段比对），防止调用方拿着
  旧的内存副本或手工拼装的快照绕过文件；
- 本模块只读取 ``snapshot_path`` 所在的发布目录内的快照文件与本票
  ``assets``，绝不读取父邮件 EML、批次原件目录或任何其他票的材料；
- 正文只由快照中经人工确认归属到本票的 ``mail_rows`` 重建，绝不从父
  邮件补字段，也不把排除行/别票行补回；被排除行在快照里本来就不在
  ``mail_rows`` 中；
- 不生成、不覆盖任何文件，也不改变任务/批次状态；纯只读返回请求。

输出与 :func:`cater.core.api.process_job` 的现有请求契约兼容：
``task_id``/``kind``/``destination``/``sources``/``mail``/``references``/
``output_dir``/``header_aliases``，并新增 ``confirmed_input``：
``{schema_version: 1, candidate: <深拷贝>, mail_rows: <深拷贝>,
source_fingerprint: <str>}``。核心接入方应只在显式 ``confirmed_input``
校验通过后走已确认解析路径，不能借它跳过正式核对。

正文 HTML 说明：快照只冻结了解析后的语义字段，本模块用现有邮件别名
（``mso`` -> ``MSO``、``serial_number`` -> ``Serial Number`` 等）生成
一张规范业务表，供现有 ``process_job`` 入口兼容使用；字段值取自快照
字符串，HTML 转义、按原样保留（含金额/机器字段精度，绝不补算价格）。

别名 overlay：预览入口 ``split_preview`` 为核对发票身份会额外把
``invoice_no`` 列（``INVOICE NO.``/``INVOICE NO``）解析进正文行快照；
``process_job`` 默认邮件别名没有该字段。为保证“预览 -> 确认 -> 核心”
看到同一组字段且发票身份不被无痕丢弃，本模块内置与预览一致的受控
overlay :data:`MAIL_ALIAS_OVERLAY`，把它与调用方 ``header_aliases``
合并后随 ``request["header_aliases"]`` 传入；调用方别名保留，任何与
规范表头冲突的映射仍会抛 ``ValidationError``，不静默改名或丢列。

``task_id`` 由调用方从服务端已确认任务取得（本模块无法从快照反查），
绝不能采用客户端任意 JSON/路径；快照与任务的身份绑定由上游
``verify_batch_task_snapshot`` 和调用方保证。

调用示例（纯合成，供核心接入方参考）::

    import json
    from pathlib import Path
    from cater.app.confirmed_input import build_confirmed_request

    snapshot_path = Path("/tmp/synthetic-snapshot/snapshot.json")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    # 前置：BatchService.verify_batch_task_snapshot(task_id)["ok"] is True
    request = build_confirmed_request(
        snapshot,
        snapshot_path=snapshot_path,
        task_id="task-synthetic-1",
        destination={"name": "测试国", "alpha3": "TST"},
        references={"machines": [], "countries": [], "machine_aliases": {}},
        output_dir=Path("/tmp/synthetic-out"),
        header_aliases=None,
    )
    # request 可直接传给 cater.core.api.process_job；其中
    # request["confirmed_input"] 携带 candidate/mail_rows/source_fingerprint。
"""
from __future__ import annotations

import copy
import html
import json
import re
from pathlib import Path
from typing import Any

from ..core.parsing.headers import normalize_header
from ..core.parsing.mail_html import mail_aliases
from .errors import ValidationError
from .util import sha256_file

SNAPSHOT_SCHEMA = 1
CONFIRMED_INPUT_SCHEMA = 1

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# 与 split_preview._PREVIEW_ALIASES 中正文发票列保持一致的受控 overlay：
# 预览会把 INVOICE NO. 解析进 mail_row_refs.fields["invoice_no"]，核心默认
# 邮件别名不含该字段。只在已确认请求里追加，不修改全局默认别名。
MAIL_ALIAS_OVERLAY: dict[str, tuple[str, ...]] = {
    "invoice_no": ("INVOICE NO.", "INVOICE NO"),
}

# 快照语义字段 -> 规范 HTML 表头。每个表头经 normalize_header 后必须命中
# 该字段的邮件别名（含上面的受控 overlay；测试锁定，避免与解析器漂移）。
MAIL_FIELD_LABELS: dict[str, str] = {
    "region": "Regional",
    "order": "Order",
    "dealer_order": "Dealer Order",
    "mso": "MSO",
    "dealer_code": "Dealer Code",
    "ship_to_party": "Ship To Party",
    "dealer_name": "Dealer Name",
    "material": "Material",
    "serial_number": "Serial Number",
    "invoice_no": "INVOICE NO.",
    "net_value": "Net Value Ord.Item",
    "currency": "Doc.Currency",
    "order_status": "Order Status",
    "dealer_net_net": "Dealer Net Net",
    "inland_freight": "Inland Freight",
    "ocean_freight": "Ocean Freight",
    "insurance": "Insurance",
    "others": "Others",
    "total_sales_price": "Total Sales Price",
}

# 规范列顺序（同票多行稳定输出；不存在的字段不生成列）。
_FIELD_ORDER = tuple(MAIL_FIELD_LABELS)


def _fail(message: str) -> None:
    raise ValidationError(f"确认输入无效：{message}")


def _canonical(value: Any) -> str:
    """等值比较用的规范化 JSON（与快照落盘时的规范化口径一致）。"""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        _fail(f"快照包含无法序列化的内容：{type(exc).__name__}")


def _normalized_sha(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value.strip()):
        _fail(f"{what} 的 sha256 缺失或格式非法")
    return value.strip().lower()


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{what} 缺失或不是非空字符串")
    return value


def _row_key(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _fail("正文行的 MSO/序列号必须为字符串")
    return value.strip()


def _resolve_snapshot_file(snapshot_path: str | Path) -> tuple[Path, Path]:
    """返回 (快照目录, snapshot.json 真实路径)，拒绝符号链接路径。

    ``snapshot_path`` 既可以是 JSON 文件本身，也可以是任务表里持久化的
    快照目录（``verify_batch_task_snapshot`` 返回目录）；两种形态都要求
    不是符号链接，且解析后的文件确实是普通文件。
    """
    if not isinstance(snapshot_path, (str, Path)) or not str(snapshot_path).strip():
        _fail("snapshot_path 缺失")
    if "\x00" in str(snapshot_path):
        _fail("snapshot_path 含非法字符")
    raw = Path(snapshot_path)
    try:
        if raw.is_symlink():
            _fail(f"拒绝符号链接快照路径：{raw}")
        if not raw.exists():
            _fail(f"快照路径不存在：{raw}")
        snap_file = raw / "snapshot.json" if raw.is_dir() else raw
        if snap_file.is_symlink():
            _fail(f"拒绝符号链接快照文件：{snap_file}")
        resolved = snap_file.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(f"快照路径不可访问：{type(exc).__name__}")
    if not resolved.is_file():
        _fail(f"快照文件不存在或不是普通文件：{snap_file}")
    return resolved.parent, resolved


def _read_snapshot_file(snapshot_file: Path) -> dict:
    try:
        text = snapshot_file.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, UnicodeError, ValueError) as exc:
        _fail(f"快照文件不可读或已损坏：{type(exc).__name__}")
    if not isinstance(payload, dict):
        _fail("快照文件顶层必须是对象")
    return payload


def _validate_schema(snapshot: dict) -> None:
    schema = snapshot.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) \
            or schema != SNAPSHOT_SCHEMA:
        _fail(f"不支持的快照 schema={schema!r}（仅支持 {SNAPSHOT_SCHEMA}）")


def _validate_candidate(candidate: Any) -> tuple[dict, list[dict]]:
    if not isinstance(candidate, dict):
        _fail("快照缺少 candidate 对象")
    _require_text(candidate.get("candidate_id"), "candidate_id")
    kind = candidate.get("kind")
    if kind not in ("machine", "parts"):
        _fail(f"候选业务类型非法：{kind!r}（仅支持 machine/parts）")
    if candidate.get("needs_confirmation"):
        _fail("候选仍处于待人工确认状态，不能生成已确认请求")
    issues = candidate.get("issues")
    if issues is not None:
        if not isinstance(issues, list) or any(not isinstance(i, dict)
                                               for i in issues):
            _fail("candidate.issues 结构非法")
        blocking = [i for i in issues if i.get("blocking", True)]
        if blocking:
            codes = ", ".join(sorted({str(i.get("code")) for i in blocking}))
            _fail(f"候选仍存在阻断问题（{codes}），不能生成已确认请求")
    refs = candidate.get("source_refs")
    if not isinstance(refs, list):
        _fail("候选缺少 source_refs 列表")
    refs_by_sha: dict[str, dict] = {}
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            _fail(f"候选 source_refs[{index}] 不是对象")
        sha = _normalized_sha(ref.get("sha256"),
                              f"候选 source_refs[{index}]")
        if sha in refs_by_sha:
            _fail(f"候选 source_refs 重复资产：{sha[:12]}")
        filenames = ref.get("filenames")
        if filenames is None and isinstance(ref.get("filename"), str):
            filenames = [ref["filename"]]
        if not isinstance(filenames, list) or not filenames or any(
                not isinstance(name, str) or not name.strip()
                for name in filenames):
            _fail(f"候选 source_refs[{index}] 缺少有效 filenames")
        if "assigned" in ref and not isinstance(ref["assigned"], bool):
            _fail(f"候选 source_refs[{index}].assigned 必须为布尔值")
        regions = ref.get("regions", [])
        if not isinstance(regions, list) or any(
                not isinstance(region, dict) for region in regions):
            _fail(f"候选 source_refs[{index}].regions 结构非法")
        refs_by_sha[sha] = ref
    return candidate, refs


def _validate_mail_rows(snapshot: dict, candidate: dict) -> list[dict]:
    rows = snapshot.get("mail_rows")
    if not isinstance(rows, list):
        _fail("快照缺少 mail_rows 列表")
    candidate_rows = candidate.get("mail_row_refs")
    if not isinstance(candidate_rows, list):
        _fail("候选缺少 mail_row_refs 列表")
    if _canonical(rows) != _canonical(candidate_rows):
        _fail("快照 mail_rows 与候选 mail_row_refs 不一致（不能补回排除行/别票行）")

    seen: set[tuple[int | None, int]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            _fail(f"mail_rows[{index}] 不是对象")
        if "table_index" not in row:
            _fail(f"mail_rows[{index}].table_index 缺失")
        table_index = row.get("table_index")
        # 纯文本业务表的 Source.table_index 合法为 None（没有 HTML 表号）；
        # 只允许 None 或非负整数，绝不把 None 伪造成 HTML 表 0。
        if table_index is not None and (
                isinstance(table_index, bool)
                or not isinstance(table_index, int) or table_index < 0):
            _fail(f"mail_rows[{index}].table_index 非法")
        if "row" not in row:
            _fail(f"mail_rows[{index}].row 缺失")
        row_number = row.get("row")
        if isinstance(row_number, bool) or not isinstance(row_number, int) \
                or row_number < 1:
            _fail(f"mail_rows[{index}].row 非法")
        key = (table_index, row_number)
        if key in seen:
            _fail(f"mail_rows 重复正文行：表 {table_index} 第 {row_number} 行")
        seen.add(key)
        fields = row.get("fields")
        if not isinstance(fields, dict):
            _fail(f"mail_rows[{index}].fields 缺失或不是对象")
        for name, value in fields.items():
            if not isinstance(name, str) or not name:
                _fail(f"mail_rows[{index}].fields 含非法字段名")
            if not isinstance(value, str):
                _fail(f"mail_rows[{index}] 字段 {name!r} 的值必须是字符串"
                      "（金额/机器字段精度以快照原文为准）")
        mso = _row_key(fields.get("mso"))
        serial = _row_key(fields.get("serial_number"))
        if not mso and not serial:
            _fail(f"mail_rows[{index}] 缺少 MSO/序列号，正文行身份不完整")
        want_mso = _row_key(row.get("mso"))
        want_serial = _row_key(row.get("serial"))
        if want_mso != mso:
            _fail(f"mail_rows[{index}].mso 与 fields 不一致")
        if want_serial != serial:
            _fail(f"mail_rows[{index}].serial 与 fields 不一致")
    return rows


def _validate_assets(snapshot: dict, base_dir: Path,
                     snapshot_file: Path, refs: list[dict]) -> list[dict]:
    assets = snapshot.get("assets")
    if not isinstance(assets, list):
        _fail("快照缺少 assets 列表")
    refs_by_sha = {_normalized_sha(ref["sha256"], "候选资产"): ref
                   for ref in refs}
    assets_by_sha: dict[str, dict] = {}
    resolved_paths: set[Path] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            _fail(f"assets[{index}] 不是对象")
        sha = _normalized_sha(asset.get("sha256"), f"assets[{index}]")
        if sha in assets_by_sha:
            _fail(f"重复资产：{sha[:12]}")
        if sha not in refs_by_sha:
            _fail(f"资产 {sha[:12]} 不属于本票候选（拒绝跨票材料）")
        filename = _require_text(asset.get("filename"),
                                 f"assets[{index}].filename")
        raw_path = asset.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            _fail(f"assets[{index}].path 缺失")
        if "\x00" in raw_path:
            _fail(f"assets[{index}].path 含非法字符")
        path = Path(raw_path)
        if not path.is_absolute():
            path = base_dir / path
        try:
            if path.is_symlink():
                _fail(f"拒绝符号链接资产：{filename}")
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            _fail(f"资产路径不可访问（{filename}）：{type(exc).__name__}")
        if resolved.parent != base_dir:
            _fail(f"资产路径越界：{filename} 不在本票快照目录内")
        if resolved == snapshot_file:
            _fail(f"资产路径指向快照文件本身：{filename}")
        if resolved in resolved_paths:
            _fail(f"重复资产路径：{filename}")
        resolved_paths.add(resolved)
        if not resolved.is_file():
            _fail(f"资产文件缺失：{filename}")
        size = asset.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail(f"assets[{index}].size_bytes 缺失或非法")
        try:
            actual_size = resolved.stat().st_size
        except OSError as exc:
            _fail(f"资产文件不可读（{filename}）：{type(exc).__name__}")
        if actual_size != size:
            _fail(f"资产大小不符：{filename}（缺失或被篡改）")
        try:
            digest = sha256_file(resolved)
        except OSError as exc:
            _fail(f"资产文件不可读（{filename}）：{type(exc).__name__}")
        if digest != sha:
            _fail(f"资产哈希不符：{filename}（缺失或被篡改）")
        ref = refs_by_sha[sha]
        ref_filenames = ref.get("filenames") or [ref.get("filename")]
        if filename not in ref_filenames:
            _fail(f"资产 {filename} 与候选 filenames 不一致")
        if bool(asset.get("assigned")) != bool(ref.get("assigned")):
            _fail(f"资产 {filename} 的指派标记与候选不一致")
        if _canonical(asset.get("regions", [])) != \
                _canonical(ref.get("regions", [])):
            _fail(f"资产 {filename} 的区域信息与候选不一致")
        assets_by_sha[sha] = asset
    if set(assets_by_sha) != set(refs_by_sha):
        missing = sorted(set(refs_by_sha) - set(assets_by_sha))
        extra = sorted(set(assets_by_sha) - set(refs_by_sha))
        detail = []
        if missing:
            detail.append(f"缺少 {[s[:12] for s in missing]}")
        if extra:
            detail.append(f"多出 {[s[:12] for s in extra]}")
        _fail("本票资产清单与候选 source_refs 不一致：" + "；".join(detail))
    return assets


def _merge_mail_aliases(header_aliases: dict | None) -> dict[str, list[str]]:
    """受控 overlay + 调用方别名（后者只追加，不覆盖 overlay 字段）。

    与 ``split_preview.inspect_sources`` 的合并顺序一致：先预览 overlay，
    再逐字段追加调用方别名。返回值直接随请求传入，核心邮件解析因此能
    识别 ``invoice_no``，同时保留调用方自定义邮件/工作簿别名。
    """
    merged: dict[str, list[str]] = {
        name: list(keys) for name, keys in MAIL_ALIAS_OVERLAY.items()}
    for name, extra in (header_aliases or {}).items():
        if not isinstance(extra, (list, tuple)):
            # process_job/mail_aliases 同样忽略非列表配置；不在此发明新语义。
            continue
        bucket = merged.setdefault(str(name), [])
        for item in extra:
            text = str(item).strip()
            if text and text not in bucket:
                bucket.append(text)
    return merged


def _field_labels(rows: list[dict], header_aliases: dict | None) -> dict[str, str]:
    """字段 -> 规范表头，并核对表头在合并别名集中仍归到同一字段。"""
    present = {name for row in rows for name in row["fields"]}
    resolved = mail_aliases(header_aliases)
    key_to_field: dict[str, str] = {}
    for field, keys in resolved.items():
        for key in keys:
            # 与解析器 _resolve_header 一致：后出现的字段名覆盖同名键。
            key_to_field[key] = field

    labels: dict[str, str] = {}
    custom: list[str] = []
    for field in _FIELD_ORDER:
        if field in present:
            labels[field] = MAIL_FIELD_LABELS[field]
    for field in sorted(present - set(MAIL_FIELD_LABELS)):
        extra = (header_aliases or {}).get(field)
        if not isinstance(extra, (list, tuple)):
            _fail(f"正文行含未知语义字段 {field!r}，且未配置可映射的邮件别名")
        label = next((str(item).strip() for item in extra
                      if str(item).strip()), "")
        if not label:
            _fail(f"正文行字段 {field!r} 缺少可映射的邮件别名")
        labels[field] = label
        custom.append(field)

    for field, label in labels.items():
        key = normalize_header(label)
        owner = key_to_field.get(key)
        if owner != field:
            _fail(f"规范表头 {label!r} 在现有邮件别名中映射到 {owner!r}，"
                  f"与字段 {field!r} 冲突")
    # 输出列顺序：规范业务字段在前（保持既有顺序），自定义字段随后。
    ordered = [field for field in _FIELD_ORDER if field in labels]
    ordered.extend(sorted(custom))
    return {field: labels[field] for field in ordered}


def _render_mail_html(rows: list[dict], labels: dict[str, str]) -> str:
    columns = list(labels)
    header = "".join(f"<th>{html.escape(labels[name], quote=True)}</th>"
                     for name in columns)
    body: list[str] = []
    for row in rows:
        cells = "".join(
            f"<td>{html.escape(row['fields'].get(name, ''), quote=True)}</td>"
            for name in columns)
        body.append(f"<tr>{cells}</tr>")
    return (
        "<html><body><table><thead><tr>"
        f"{header}"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></body></html>"
    )


def build_confirmed_request(
    snapshot: dict,
    *,
    snapshot_path: str | Path,
    task_id: str,
    destination: dict | None,
    references: dict,
    output_dir: str | Path,
    header_aliases: dict | None = None,
) -> dict:
    """把已验真的本票快照转换为现有 ``process_job`` 请求（纯只读）。

    前置条件：调用方已执行
    ``BatchService.verify_batch_task_snapshot(task_id)`` 且 ``ok is True``。
    本模块只做二次结构与业务一致性校验；任何缺失/篡改/路径逃逸都会抛出
    :class:`cater.app.errors.ValidationError`，且不生成任何文件。
    """
    if not isinstance(snapshot, dict):
        _fail("snapshot 必须是对象")
    _validate_schema(snapshot)
    base_dir, snapshot_file = _resolve_snapshot_file(snapshot_path)
    payload = _read_snapshot_file(snapshot_file)
    if _canonical(payload) != _canonical(snapshot):
        _fail("传入快照与 snapshot_path 文件内容不一致（不能凭内存副本绕过文件）")

    candidate, refs = _validate_candidate(snapshot.get("candidate"))
    rows = _validate_mail_rows(snapshot, candidate)
    assets = _validate_assets(snapshot, base_dir, snapshot_file, refs)

    fingerprint = _require_text(snapshot.get("source_fingerprint"),
                                "source_fingerprint")
    if not isinstance(task_id, str) or not task_id.strip():
        _fail("task_id 缺失或不是非空字符串")
    if destination is not None and not isinstance(destination, dict):
        _fail("destination 必须是对象或 None")
    if not isinstance(references, dict):
        _fail("references 必须是对象")
    if header_aliases is not None and not isinstance(header_aliases, dict):
        _fail("header_aliases 必须是对象或 None")
    if not isinstance(output_dir, (str, Path)) or not str(output_dir).strip():
        _fail("output_dir 缺失或不是非空路径")
    if "\x00" in str(output_dir):
        _fail("output_dir 含非法字符")

    effective_aliases = _merge_mail_aliases(header_aliases)
    labels = _field_labels(rows, effective_aliases) if rows else {}
    mail = {"html": _render_mail_html(rows, labels)} if rows else {}

    sources = [{
        "path": str(Path(asset["path"]).resolve()
                    if Path(asset["path"]).is_absolute()
                    else (base_dir / asset["path"]).resolve()),
        "filename": asset["filename"],
        "sha256": _normalized_sha(asset["sha256"], "资产"),
    } for asset in assets]

    request = {
        "task_id": task_id,
        "kind": candidate["kind"],
        "destination": copy.deepcopy(destination),
        "sources": sources,
        "mail": mail,
        "references": copy.deepcopy(references),
        "output_dir": str(output_dir),
        "overrides": [],
        "confirmed_input": {
            "schema_version": CONFIRMED_INPUT_SCHEMA,
            "candidate": copy.deepcopy(candidate),
            "mail_rows": copy.deepcopy(rows),
            "source_fingerprint": fingerprint,
        },
    }
    if rows or header_aliases is not None:
        # 含受控 invoice_no overlay；调用方别名已合并保留。
        request["header_aliases"] = effective_aliases
    return request
