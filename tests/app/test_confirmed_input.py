"""``cater.app.confirmed_input`` 契约与边界测试（全合成数据）。

覆盖：两票输入不串、缺字段/未知 schema/哈希被改/路径越界/symlink/重复
资产/正文结构拒绝、HTML 恶意字段转义、正文来源只来自快照行、输入
dict 不被修改、同票多行与金额精度完整保留，以及输出请求可被现有
``process_job`` 直接消费。
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from batchkit import import_two_ticket, machine_workbook_bytes
from helpers import make_mail
from cater.app import confirmed_input as confirmed_mod
from cater.app.confirmed_input import (MAIL_ALIAS_OVERLAY, MAIL_FIELD_LABELS,
                                       build_confirmed_request)
from cater.app.errors import ValidationError
from cater.core.api import ConfirmedInputError, process_job
from cater.core.parsing.headers import normalize_header
from cater.core.parsing.mail_html import mail_aliases, parse_mail_html

DESTINATION = {"name": "测试国", "alpha3": "TST"}
REFERENCES = {"machines": [], "countries": [], "machine_aliases": {}}

_CONFIRMED_PARSERS = (
    "cater.core.parsing.machine_confirmed",
    "cater.core.parsing.kit_confirmed",
)


@pytest.fixture(autouse=True)
def _restore_confirmed_parser_imports():
    """本模块的端到端测试会真正加载 B/C；测试结束后还原导入现场。

    core 的既有契约测试会断言旧路径不 eager 引入已确认解析器；导入是
    进程级状态，本模块必须不把自身加载行为泄漏给后续测试。
    """
    before = {name: sys.modules.get(name) for name in _CONFIRMED_PARSERS}
    yield
    for name, module in before.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _mail_row(table_index: int, row: int, *, mso="MSOSYN1", serial="SER0001",
              **fields) -> dict:
    values: dict[str, str] = {}
    if mso:
        values["mso"] = mso
    if serial:
        values["serial_number"] = serial
    for name, value in fields.items():
        values[name] = str(value)
    return {
        "table_index": table_index,
        "row": row,
        "mso": mso or None,
        "serial": serial or None,
        "fields": values,
    }


def _default_rows() -> list[dict]:
    return [
        _mail_row(0, 2, material="TESTMODEL", dealer_code="SYN-DEALER",
                  dealer_order="SYN-DO-1", order="12345",
                  total_sales_price="1000.0000"),
        _mail_row(0, 3, mso="MSOSYN2", serial="SER0002",
                  material="TESTMODEL", dealer_code="SYN-DEALER",
                  dealer_order="SYN-DO-2", order="12346",
                  total_sales_price="250.0050"),
    ]


def _write(path: Path, snapshot: dict) -> None:
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _make_snapshot(
    tmp_path: Path,
    *,
    name: str = "snap-a",
    candidate_id: str = "cand-syn-a",
    kind: str = "parts",
    mail_rows: list[dict] | None = None,
    assets: tuple[tuple[str, bytes], ...] = (("syn-a.xlsx", b"synthetic-a"),),
    schema: int = 1,
    source_fingerprint: str = "a" * 64,
) -> tuple[dict, Path]:
    base = tmp_path / name
    base.mkdir()
    rows = copy.deepcopy(mail_rows if mail_rows is not None else _default_rows())
    asset_entries: list[dict] = []
    refs: list[dict] = []
    for filename, data in assets:
        asset_path = base / filename
        asset_path.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        asset_entries.append({
            "sha256": sha,
            "filename": filename,
            "path": str(asset_path),
            "size_bytes": len(data),
            "assigned": False,
            "regions": [],
        })
        refs.append({"sha256": sha, "filenames": [filename], "regions": []})
    candidate = {
        "candidate_id": candidate_id,
        "kind": kind,
        "source_refs": refs,
        "mail_row_refs": copy.deepcopy(rows),
        "issues": [],
        "needs_confirmation": False,
        "identity": {"mso": [row.get("mso") for row in rows if row.get("mso")]},
    }
    snapshot = {
        "schema": schema,
        "batch_id": "batch-syn",
        "plan_id": "plan-syn",
        "plan_version": 1,
        "source_fingerprint": source_fingerprint,
        "candidate": candidate,
        "assets": asset_entries,
        "mail_rows": rows,
        "created_at": "2026-01-01T00:00:00Z",
    }
    path = base / "snapshot.json"
    _write(path, snapshot)
    return snapshot, path


def _build(snapshot: dict, path: Path, tmp_path: Path, **overrides) -> dict:
    kwargs = {
        "task_id": "task-synthetic-1",
        "destination": copy.deepcopy(DESTINATION),
        "references": copy.deepcopy(REFERENCES),
        "output_dir": tmp_path / "isolated-out",
    }
    kwargs.update(overrides)
    return build_confirmed_request(snapshot, snapshot_path=path, **kwargs)


def _replace_rows(snapshot: dict, rows: list[dict]) -> None:
    snapshot["mail_rows"] = copy.deepcopy(rows)
    snapshot["candidate"]["mail_row_refs"] = copy.deepcopy(rows)


# ---------------------------------------------------------------------------
# 输出契约与正文重建
# ---------------------------------------------------------------------------


def test_build_request_matches_process_job_contract(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    request = _build(snapshot, path, tmp_path)

    assert set(request) == {
        "task_id", "kind", "destination", "sources", "mail", "references",
        "output_dir", "overrides", "header_aliases", "confirmed_input",
    }
    assert request["task_id"] == "task-synthetic-1"
    assert request["kind"] == "parts"
    assert request["destination"] == DESTINATION
    assert request["output_dir"] == str(tmp_path / "isolated-out")
    assert request["overrides"] == []
    assert [source["filename"] for source in request["sources"]] == ["syn-a.xlsx"]
    assert set(request["mail"]) == {"html"}  # 不携带父邮件主题/正文
    assert request["header_aliases"] == {
        name: list(keys) for name, keys in MAIL_ALIAS_OVERLAY.items()}
    assert request["confirmed_input"]["schema_version"] == 1
    assert request["confirmed_input"]["source_fingerprint"] == "a" * 64
    assert request["confirmed_input"]["candidate"] == snapshot["candidate"]
    assert request["confirmed_input"]["mail_rows"] == snapshot["mail_rows"]
    # 纯只读：不创建输出目录或任何文件
    assert not (tmp_path / "isolated-out").exists()
    # JSON 可序列化（现有 process_job 请求约定）
    json.dumps(request, ensure_ascii=False)


def test_mail_labels_map_to_existing_default_aliases():
    # 预览 overlay（invoice_no）与默认邮件别名合并后必须覆盖全部规范表头。
    resolved = mail_aliases(MAIL_ALIAS_OVERLAY)
    key_to_field = {key: field for field, keys in resolved.items() for key in keys}
    assert set(MAIL_ALIAS_OVERLAY) <= set(MAIL_FIELD_LABELS)
    for field, label in MAIL_FIELD_LABELS.items():
        assert field in resolved
        assert key_to_field[normalize_header(label)] == field


def test_reconstructed_mail_table_roundtrips_exact_fields(tmp_path):
    rows = _default_rows()
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    request = _build(snapshot, path, tmp_path)

    parsed = parse_mail_html(request["mail"]["html"], None)
    assert not parsed.issues
    assert [row.fields for row in parsed.rows] == [row["fields"] for row in rows]
    assert [row.source.row for row in parsed.rows] == [2, 3]


def test_multiple_rows_and_amount_precision_preserved(tmp_path):
    rows = [
        _mail_row(0, 2, material="TESTMODEL", dealer_code="D1",
                  dealer_order="DO1", order="1", total_sales_price="1000.0000",
                  net_value="0.1000"),
        _mail_row(0, 3, mso="MSOSYN1", serial="SER0002", material="TESTMODEL",
                  dealer_code="D1", dealer_order="DO1", order="1",
                  total_sales_price="999999.9900", net_value="1234.5678"),
        _mail_row(0, 4, mso="MSOSYN2", serial="SER0003", material="TESTMODEL",
                  dealer_code="D1", dealer_order="DO1", order="1",
                  total_sales_price="0.0001", net_value="0.0002"),
    ]
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    request = _build(snapshot, path, tmp_path)

    assert request["confirmed_input"]["mail_rows"] == rows
    parsed = parse_mail_html(request["mail"]["html"], None)
    assert len(parsed.rows) == 3  # 同票多行完整保留，未去重/未聚合
    assert [row.fields["total_sales_price"] for row in parsed.rows] == [
        "1000.0000", "999999.9900", "0.0001"]
    assert [row.fields["net_value"] for row in parsed.rows] == [
        "0.1000", "1234.5678", "0.0002"]
    assert [row.fields["serial_number"] for row in parsed.rows] == [
        "SER0001", "SER0002", "SER0003"]


def test_two_ticket_inputs_do_not_cross(tmp_path):
    rows_a = [_mail_row(0, 2, mso="MSOA1", serial="SER-A1",
                        total_sales_price="10.00")]
    rows_b = [_mail_row(0, 2, mso="MSOB2", serial="SER-B2",
                        total_sales_price="20.00")]
    snap_a, path_a = _make_snapshot(
        tmp_path, name="ticket-a", candidate_id="cand-a",
        mail_rows=rows_a, assets=(("a.xlsx", b"ticket-a-bytes"),))
    snap_b, path_b = _make_snapshot(
        tmp_path, name="ticket-b", candidate_id="cand-b",
        mail_rows=rows_b, assets=(("b.xlsx", b"ticket-b-bytes"),))

    req_a = _build(snap_a, path_a, tmp_path)
    req_b = _build(snap_b, path_b, tmp_path)

    assert req_a["confirmed_input"]["candidate"]["candidate_id"] == "cand-a"
    assert req_b["confirmed_input"]["candidate"]["candidate_id"] == "cand-b"
    assert [s["filename"] for s in req_a["sources"]] == ["a.xlsx"]
    assert [s["filename"] for s in req_b["sources"]] == ["b.xlsx"]
    assert req_a["confirmed_input"]["mail_rows"] == rows_a
    assert req_b["confirmed_input"]["mail_rows"] == rows_b
    parsed_a = parse_mail_html(req_a["mail"]["html"], None)
    parsed_b = parse_mail_html(req_b["mail"]["html"], None)
    assert [row.fields["mso"] for row in parsed_a.rows] == ["MSOA1"]
    assert [row.fields["mso"] for row in parsed_b.rows] == ["MSOB2"]
    assert all("b.xlsx" not in s["path"] for s in req_a["sources"])
    assert all("a.xlsx" not in s["path"] for s in req_b["sources"])


def test_html_fields_are_escaped_but_data_is_preserved(tmp_path):
    rows = [_mail_row(0, 2, material='A<img src=x onerror="alert(1)">',
                      dealer_name="<script>alert('x')</script>",
                      dealer_code="D & D", total_sales_price="1000.00")]
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    request = _build(snapshot, path, tmp_path)
    document = request["mail"]["html"]

    assert "<img" not in document.lower()
    assert "<script" not in document.lower()
    assert "&lt;img" in document
    assert "&lt;script&gt;" in document
    assert "&amp;" in document
    # 转义只发生在 HTML 载体；快照业务数据保持原文（下游按文本处理）
    assert request["confirmed_input"]["mail_rows"] == rows
    parsed = parse_mail_html(document, None)
    assert parsed.rows[0].fields["dealer_name"] == "<script>alert('x')</script>"
    assert parsed.rows[0].fields["material"] == \
        'A<img src=x onerror="alert(1)">'
    assert parsed.rows[0].fields["dealer_code"] == "D & D"


def test_empty_mail_rows_render_no_mail_payload(tmp_path):
    snapshot, path = _make_snapshot(tmp_path, mail_rows=[])
    request = _build(snapshot, path, tmp_path)
    assert request["mail"] == {}
    assert request["confirmed_input"]["mail_rows"] == []


def test_reads_only_snapshot_and_its_listed_assets(tmp_path, monkeypatch):
    snapshot, path = _make_snapshot(tmp_path)
    allowed = {path.resolve()} | {
        Path(asset["path"]).resolve() for asset in snapshot["assets"]}
    seen: list[Path] = []
    real_sha = confirmed_mod.sha256_file
    real_read_text = Path.read_text

    def tracking_sha(asset_path):
        seen.append(Path(asset_path).resolve())
        return real_sha(asset_path)

    def tracking_read_text(self, *args, **kwargs):
        seen.append(self.resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(confirmed_mod, "sha256_file", tracking_sha)
    monkeypatch.setattr(Path, "read_text", tracking_read_text)
    _build(snapshot, path, tmp_path)
    assert set(seen) == allowed


# ---------------------------------------------------------------------------
# 快照/schema/候选/正文结构拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema", [2, 0, "1", True, None])
def test_unknown_or_missing_schema_rejected(tmp_path, schema):
    snapshot, path = _make_snapshot(tmp_path)
    if schema is None:
        snapshot.pop("schema")
    else:
        snapshot["schema"] = schema
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="schema"):
        _build(snapshot, path, tmp_path)


def test_snapshot_file_and_in_memory_copy_must_match(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    snapshot["candidate"]["facts"] = {"price": "999999"}
    with pytest.raises(ValidationError, match="内容不一致"):
        _build(snapshot, path, tmp_path)


@pytest.mark.parametrize("mutation, match", [
    (lambda snap: snap.pop("candidate"), "candidate"),
    (lambda snap: snap["candidate"].pop("candidate_id"), "candidate_id"),
    (lambda snap: snap["candidate"].__setitem__("kind", "auto"), "业务类型"),
    (lambda snap: snap["candidate"].__setitem__("needs_confirmation", True),
     "待人工确认"),
    (lambda snap: snap["candidate"].__setitem__(
        "issues", [{"code": "SYNTHETIC_BLOCK", "blocking": True}]), "阻断问题"),
    (lambda snap: snap["candidate"].pop("source_refs"), "source_refs"),
    (lambda snap: snap["candidate"].pop("mail_row_refs"), "mail_row_refs"),
    (lambda snap: snap["candidate"].__setitem__("mail_row_refs", []), "mail_rows"),
    (lambda snap: snap.pop("source_fingerprint"), "source_fingerprint"),
])
def test_missing_or_invalid_candidate_fields_rejected(tmp_path, mutation, match):
    snapshot, path = _make_snapshot(tmp_path)
    mutation(snapshot)
    _write(path, snapshot)
    with pytest.raises(ValidationError, match=match):
        _build(snapshot, path, tmp_path)


def test_duplicate_source_ref_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    snapshot["candidate"]["source_refs"].append(
        copy.deepcopy(snapshot["candidate"]["source_refs"][0]))
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="重复资产"):
        _build(snapshot, path, tmp_path)


def test_mail_rows_must_equal_candidate_refs(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    snapshot["mail_rows"].append(
        _mail_row(0, 4, mso="MSOOTHER", serial="SER-X"))
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="mail_rows"):
        _build(snapshot, path, tmp_path)


def test_other_ticket_mail_row_cannot_be_injected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    injected = [_mail_row(0, 2, mso="MSO-OTHER", serial="SER-OTHER",
                          total_sales_price="1.00")]
    snapshot["mail_rows"] = copy.deepcopy(injected)
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="mail_rows"):
        _build(snapshot, path, tmp_path)


@pytest.mark.parametrize("mutator, match", [
    (lambda rows: rows.append(copy.deepcopy(rows[0])), "重复正文行"),
    (lambda rows: rows[0].__setitem__("fields", []), "fields"),
    (lambda rows: rows[0].pop("table_index"), "table_index"),
    (lambda rows: rows[0].__setitem__("row", 0), "row 非法"),
    (lambda rows: rows[0]["fields"].__setitem__("total_sales_price", 1000.0),
     "必须是字符串"),
    (lambda rows: (rows[0]["fields"].clear(),
                   rows[0].update({"mso": None, "serial": None})),
     "身份不完整"),
    (lambda rows: rows[0]["fields"].__setitem__("mso", "MSOX"), "mso 与 fields"),
    (lambda rows: rows[0]["fields"].__setitem__("unknown_note", "x"),
     "未知语义字段"),
])
def test_invalid_mail_row_structure_rejected(tmp_path, mutator, match):
    snapshot, path = _make_snapshot(tmp_path)
    rows = copy.deepcopy(snapshot["mail_rows"])
    mutator(rows)
    _replace_rows(snapshot, rows)
    _write(path, snapshot)
    with pytest.raises(ValidationError, match=match):
        _build(snapshot, path, tmp_path)


def test_serial_only_row_is_preserved(tmp_path):
    rows = [_mail_row(0, 2, mso=None, serial="SER-ONLY",
                      total_sales_price="3.00")]
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    request = _build(snapshot, path, tmp_path)
    assert request["confirmed_input"]["mail_rows"] == rows
    assert "SER-ONLY" in request["mail"]["html"]


def test_custom_alias_field_roundtrips(tmp_path):
    rows = [dict(_mail_row(0, 2, total_sales_price="1.00"),
                 fields={**_mail_row(0, 2)["fields"],
                         "custom_note": "free text"})]
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    aliases = {"custom_note": ["Synthetic Note"]}
    request = _build(snapshot, path, tmp_path, header_aliases=aliases)
    # 调用方别名保留，受控 overlay 同时随请求下发。
    assert request["header_aliases"]["custom_note"] == ["Synthetic Note"]
    assert request["header_aliases"]["invoice_no"] == \
        list(MAIL_ALIAS_OVERLAY["invoice_no"])
    parsed = parse_mail_html(request["mail"]["html"], None,
                             aliases=request["header_aliases"])
    assert parsed.rows[0].fields["custom_note"] == "free text"


def test_unknown_custom_field_without_alias_rejected(tmp_path):
    row = _mail_row(0, 2, total_sales_price="1.00")
    row["fields"]["custom_note"] = "free text"
    snapshot, path = _make_snapshot(tmp_path, mail_rows=[row])
    with pytest.raises(ValidationError, match="未知语义字段"):
        _build(snapshot, path, tmp_path)


def test_custom_alias_collision_with_default_rejected(tmp_path):
    row = _mail_row(0, 2, total_sales_price="1.00")
    row["fields"]["custom_note"] = "free text"
    snapshot, path = _make_snapshot(tmp_path, mail_rows=[row])
    with pytest.raises(ValidationError, match="冲突"):
        _build(snapshot, path, tmp_path,
               header_aliases={"custom_note": ["MSO"]})


# ---------------------------------------------------------------------------
# 资产：哈希 / 缺失 / 路径边界 / symlink / 重复
# ---------------------------------------------------------------------------


def test_tampered_asset_bytes_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    asset_path = Path(snapshot["assets"][0]["path"])
    data = asset_path.read_bytes()
    asset_path.write_bytes(b"X" + data[1:])  # 同长度篡改，只可能靠哈希发现
    with pytest.raises(ValidationError, match="哈希不符"):
        _build(snapshot, path, tmp_path)


def test_asset_sha_rewritten_with_file_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    fake_sha = "0" * 64
    snapshot["assets"][0]["sha256"] = fake_sha
    snapshot["candidate"]["source_refs"][0]["sha256"] = fake_sha
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="哈希不符"):
        _build(snapshot, path, tmp_path)


def test_missing_asset_file_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    Path(snapshot["assets"][0]["path"]).unlink()
    with pytest.raises(ValidationError, match="资产文件缺失"):
        _build(snapshot, path, tmp_path)


def test_asset_size_tamper_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    snapshot["assets"][0]["size_bytes"] += 1
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="资产大小不符"):
        _build(snapshot, path, tmp_path)


def _asset_outside_snapshot(tmp_path: Path, snapshot: dict, path: Path,
                            asset_path: str, data: bytes) -> None:
    sha = hashlib.sha256(data).hexdigest()
    snapshot["assets"] = [{
        "sha256": sha,
        "filename": "escaped.xlsx",
        "path": asset_path,
        "size_bytes": len(data),
        "assigned": False,
        "regions": [],
    }]
    snapshot["candidate"]["source_refs"] = [{
        "sha256": sha,
        "filenames": ["escaped.xlsx"],
        "regions": [],
    }]
    _write(path, snapshot)


@pytest.mark.parametrize("path_kind", ["absolute", "relative"])
def test_asset_path_escape_rejected(tmp_path, path_kind):
    snapshot, path = _make_snapshot(tmp_path)
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"outside-snapshot-bytes")
    raw_path = str(outside) if path_kind == "absolute" else "../outside.xlsx"
    _asset_outside_snapshot(tmp_path, snapshot, path, raw_path,
                            b"outside-snapshot-bytes")
    with pytest.raises(ValidationError, match="越界"):
        _build(snapshot, path, tmp_path)


def test_asset_symlink_to_outside_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    outside = tmp_path / "outside-symlink-target.xlsx"
    outside.write_bytes(b"symlink-target-bytes")
    link = path.parent / "linked.xlsx"
    link.symlink_to(outside)
    _asset_outside_snapshot(tmp_path, snapshot, path, str(link),
                            b"symlink-target-bytes")
    with pytest.raises(ValidationError, match="符号链接"):
        _build(snapshot, path, tmp_path)


def test_snapshot_path_symlink_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    copy_path = tmp_path / "snapshot-copy.json"
    copy_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / "snapshot-link.json"
    link.symlink_to(copy_path)
    with pytest.raises(ValidationError, match="符号链接"):
        _build(snapshot, link, tmp_path)


def test_duplicate_asset_entry_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    snapshot["assets"].append(copy.deepcopy(snapshot["assets"][0]))
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="重复资产"):
        _build(snapshot, path, tmp_path)


def test_asset_not_referenced_by_candidate_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    extra = path.parent / "other-ticket.xlsx"
    extra.write_bytes(b"other-ticket-bytes")
    snapshot["assets"].append({
        "sha256": hashlib.sha256(b"other-ticket-bytes").hexdigest(),
        "filename": "other-ticket.xlsx",
        "path": str(extra),
        "size_bytes": len(b"other-ticket-bytes"),
        "assigned": False,
        "regions": [],
    })
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="不属于本票"):
        _build(snapshot, path, tmp_path)


def test_missing_referenced_asset_rejected(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    snapshot["assets"] = []
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="资产清单"):
        _build(snapshot, path, tmp_path)


def test_asset_filename_must_match_candidate(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    snapshot["assets"][0]["filename"] = "renamed.xlsx"
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="filenames"):
        _build(snapshot, path, tmp_path)


def test_verified_boolean_does_not_bypass_asset_hash(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    snapshot["verified"] = True
    snapshot["ok"] = True
    _write(path, snapshot)
    asset_path = Path(snapshot["assets"][0]["path"])
    data = asset_path.read_bytes()
    asset_path.write_bytes(b"Y" + data[1:])
    with pytest.raises(ValidationError, match="哈希不符"):
        _build(snapshot, path, tmp_path)


# ---------------------------------------------------------------------------
# 参数与快照路径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overrides, match", [
    ({"task_id": ""}, "task_id"),
    ({"task_id": 123}, "task_id"),
    ({"destination": "TST"}, "destination"),
    ({"references": []}, "references"),
    ({"header_aliases": []}, "header_aliases"),
    ({"output_dir": ""}, "output_dir"),
    ({"output_dir": None}, "output_dir"),
])
def test_invalid_request_arguments_rejected(tmp_path, overrides, match):
    snapshot, path = _make_snapshot(tmp_path)
    with pytest.raises(ValidationError, match=match):
        _build(snapshot, path, tmp_path, **overrides)


def test_missing_snapshot_path_rejected(tmp_path):
    snapshot, _path = _make_snapshot(tmp_path)
    with pytest.raises(ValidationError, match="不存在"):
        _build(snapshot, tmp_path / "missing.json", tmp_path)


def test_snapshot_directory_argument_supported(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    request = build_confirmed_request(
        snapshot, snapshot_path=path.parent, task_id="task-synthetic-1",
        destination=copy.deepcopy(DESTINATION),
        references=copy.deepcopy(REFERENCES),
        output_dir=tmp_path / "isolated-out")
    assert request["confirmed_input"]["candidate"] == snapshot["candidate"]


def test_inputs_are_not_mutated_and_output_is_deep_copied(tmp_path):
    snapshot, path = _make_snapshot(tmp_path)
    aliases = {"custom_note": ["Synthetic Note"]}
    destination = copy.deepcopy(DESTINATION)
    references = copy.deepcopy(REFERENCES)
    before = copy.deepcopy(snapshot)
    before_aliases = copy.deepcopy(aliases)
    before_destination = copy.deepcopy(destination)
    before_references = copy.deepcopy(references)

    request = _build(snapshot, path, tmp_path, header_aliases=aliases,
                     destination=destination, references=references)

    assert snapshot == before
    assert aliases == before_aliases
    assert destination == before_destination
    assert references == before_references
    assert request["confirmed_input"]["candidate"] is not snapshot["candidate"]
    assert request["confirmed_input"]["mail_rows"] is not snapshot["mail_rows"]

    request["confirmed_input"]["candidate"]["candidate_id"] = "mutated"
    request["confirmed_input"]["mail_rows"].clear()
    request["references"]["machines"].append({"model": "MUTATED"})
    request["destination"]["alpha3"] = "MUT"
    request["header_aliases"]["custom_note"].append("Mutated")
    assert snapshot == before
    assert references == before_references
    assert destination == before_destination
    assert aliases == before_aliases
    assert snapshot["candidate"]["candidate_id"] == "cand-syn-a"
    assert snapshot["mail_rows"]

    snapshot["candidate"]["kind"] = "machine"
    assert request["kind"] == "parts"


# ---------------------------------------------------------------------------
# 与现有 process_job 的真实兼容
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 真正的合成 preview -> 确认 -> build -> core 端到端
# ---------------------------------------------------------------------------


def _machine_references() -> dict:
    return {
        "machines": [{
            "model": "TESTMODEL",
            "english_name": "TEST MODEL",
            "hs_code": "8429401900",
            "product_name": "压路机",
            "declaration": "1|0|synthetic",
            "destination_scope": "default",
        }],
        "countries": [],
        "machine_aliases": {},
    }


def _synthetic_machine_bytes(invoice_no: str = "SYN-INV-01") -> bytes:
    return machine_workbook_bytes(invoice_no, "MSOSYN1", "SERSYN1", 1000)


def _synthetic_html(invoice_no: str = "SYN-INV-01",
                    price: str = "1000.0000") -> str:
    return (
        "<html><body><p>Synthetic preview table.</p><table>"
        "<tr><th>Region</th><th>Order</th><th>Dealer Code</th>"
        "<th>Dealer Order</th><th>Material</th><th>MSO</th>"
        "<th>Serial Number</th><th>Total Sales Price</th>"
        "<th>INVOICE NO.</th></tr>"
        "<tr><td>Test</td><td>12345</td><td>SYN-DEALER</td>"
        "<td>SYN-DO-1</td><td>TESTMODEL</td><td>MSOSYN1</td>"
        f"<td>SERSYN1</td><td>{price}</td><td>{invoice_no}</td></tr>"
        "</table></body></html>"
    )


def _synthetic_text(invoice_no: str = "SYN-INV-01",
                    price: str = "1000.0000") -> str:
    header = "\t".join([
        "Region", "Order", "Dealer Code", "Dealer Order", "Material", "MSO",
        "Serial Number", "Total Sales Price", "INVOICE NO."])
    row = "\t".join([
        "Test", "12345", "SYN-DEALER", "SYN-DO-1", "TESTMODEL", "MSOSYN1",
        "SERSYN1", price, invoice_no])
    return f"Synthetic text order\n\n{header}\n{row}\n"


def _synthetic_eml(*, html: str | None, text: str = "synthetic",
                   workbook_invoice: str = "SYN-INV-01") -> bytes:
    return make_mail(
        sender="tester@example.com",
        subject="Synthetic invoice",
        text=text,
        html=html,
        attachments=(("syn-invoice.xlsx",
                      _synthetic_machine_bytes(workbook_invoice)),),
    )


def _confirm_synthetic(service, raw: bytes, *, filename: str = "synthetic.eml"):
    imported = service.batches.import_eml_bytes(
        raw, filename=filename, actor="tester")
    preview = imported["preview"]
    assert len(preview["candidates"]) == 1, preview.get("issues")
    assert preview["batch_ready"] is True, preview.get("issues")
    candidate = preview["candidates"][0]
    assert candidate["needs_confirmation"] is False, candidate.get("issues")
    outcome = service.batches.confirm_and_create(
        imported["batch"]["id"], plan_id=imported["plan"]["id"],
        fingerprint=imported["plan"]["source_fingerprint"],
        selected=[candidate["candidate_id"]], exclusions={}, actor="tester")
    assert len(outcome["tasks"]) == 1
    return preview, candidate, outcome["tasks"][0]


def _request_from_task(service, item: dict, tmp_path: Path, *,
                       header_aliases: dict | None = None) -> tuple[dict, dict]:
    check = service.batches.verify_batch_task_snapshot(item["task_id"])
    assert check["ok"] is True
    snapshot_file = Path(item["snapshot_path"])
    payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    request = build_confirmed_request(
        payload, snapshot_path=check["snapshot_path"], task_id=item["task_id"],
        destination=copy.deepcopy(DESTINATION),
        references=_machine_references(),
        output_dir=tmp_path / "machine-out",
        header_aliases=header_aliases,
    )
    return request, payload


def test_synthetic_preview_confirm_build_keeps_invoice_and_core_conflict(service,
                                                                        tmp_path):
    """HTML 带 INVOICE NO 列：发票身份过 build 仍可见，传输冲突被 core 拒绝。"""
    raw = _synthetic_eml(html=_synthetic_html())
    preview, candidate, item = _confirm_synthetic(service, raw)
    assert candidate["identity"]["invoice_no"] == ["SYN-INV-01"]
    assert candidate["mail_row_refs"][0]["fields"]["invoice_no"] == "SYN-INV-01"

    request, payload = _request_from_task(service, item, tmp_path)

    # 预览 overlay 随请求下发；发票号既在 confirmed_input 原文里，也在 HTML 载体里。
    assert request["header_aliases"]["invoice_no"] == \
        list(MAIL_ALIAS_OVERLAY["invoice_no"])
    assert request["confirmed_input"]["mail_rows"] == payload["mail_rows"]
    assert request["confirmed_input"]["mail_rows"][0]["fields"]["invoice_no"] == \
        "SYN-INV-01"
    assert "INVOICE NO." in request["mail"]["html"]
    parsed = parse_mail_html(request["mail"]["html"], None,
                             aliases=request["header_aliases"])
    assert parsed.rows[0].fields["invoice_no"] == "SYN-INV-01"

    response = process_job(request)
    assert response["status"] == "ready", response["issues"]
    assert response["kind"] == "machine"
    assert [record["record_key"] for record in response["records"]] == \
        ["MSOSYN1"]
    assert response["records"][0]["fields"]["total_sales_price"] == "1000.0000"

    # 候选身份发票号与工作簿冲突时，A 不篡改/归一化身份；core 仍显式报冲突。
    conflicted = copy.deepcopy(request)
    conflicted["confirmed_input"]["candidate"]["identity"]["invoice_no"] = [
        "SYN-INV-99"]
    conflict_response = process_job(conflicted)
    assert conflict_response["status"] == "waiting_input"
    conflict_codes = {issue["code"] for issue in conflict_response["issues"]}
    assert "INVOICE_NO_CONFLICT" in conflict_codes
    assert any("SYN-INV-01" in issue["message"]
               and "SYN-INV-99" in issue["message"]
               for issue in conflict_response["issues"])

    # 载体发票号与已确认正文行冲突时，core 必须显式拒绝而不是静默丢字段。
    tampered = copy.deepcopy(request)
    tampered["mail"]["html"] = tampered["mail"]["html"].replace(
        "SYN-INV-01", "SYN-INV-99")
    with pytest.raises(ConfirmedInputError,
                       match="邮件正文与已确认正文行不一致"):
        process_job(tampered)


def test_synthetic_preview_confirm_build_text_keeps_null_table_index(service,
                                                                     tmp_path):
    """纯文本业务表 Source.table_index=None：确认与 build 全程保留。"""
    raw = _synthetic_eml(html=None, text=_synthetic_text())
    _preview, candidate, item = _confirm_synthetic(
        service, raw, filename="synthetic-text.eml")
    assert candidate["mail_row_refs"][0]["table_index"] is None

    request, payload = _request_from_task(service, item, tmp_path)

    row = request["confirmed_input"]["mail_rows"][0]
    assert row["table_index"] is None  # 不伪造成 HTML 表 0
    assert row["row"] == 3
    assert row["fields"]["invoice_no"] == "SYN-INV-01"
    assert payload["mail_rows"][0]["table_index"] is None
    parsed = parse_mail_html(request["mail"]["html"], None,
                             aliases=request["header_aliases"])
    assert parsed.rows[0].fields["invoice_no"] == "SYN-INV-01"
    assert parsed.rows[0].fields["total_sales_price"] == "1000.0000"

    response = process_job(request)
    assert response["status"] == "ready", response["issues"]
    assert [record["record_key"] for record in response["records"]] == \
        ["MSOSYN1"]


def test_synthetic_invoice_mismatch_stays_visible_and_blocks_confirmation(
        service):
    """预览发现正文/附件发票冲突时，冲突可见且不能确认建票（无绕过）。"""
    raw = _synthetic_eml(html=_synthetic_html(invoice_no="SYN-INV-99"))
    imported = service.batches.import_eml_bytes(
        raw, filename="conflict.eml", actor="tester")
    preview = imported["preview"]
    assert preview["batch_ready"] is False
    candidate = preview["candidates"][0]
    assert candidate["needs_confirmation"] is True
    codes = {issue["code"] for issue in candidate["issues"]}
    assert "INVOICE_MISMATCH" in codes
    with pytest.raises(ValidationError, match="未解决问题"):
        service.batches.confirm_and_create(
            imported["batch"]["id"], plan_id=imported["plan"]["id"],
            fingerprint=imported["plan"]["source_fingerprint"],
            selected=[candidate["candidate_id"]], exclusions={},
            actor="tester")


# ---------------------------------------------------------------------------
# 纯文本坐标：合法 None / 非法类型 / 稳定去重
# ---------------------------------------------------------------------------


def test_null_table_index_row_accepted_with_invoice_overlay(tmp_path):
    rows = [_mail_row(None, 2, invoice_no="SYN-INV-01")]
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    request = _build(snapshot, path, tmp_path)

    row = request["confirmed_input"]["mail_rows"][0]
    assert row["table_index"] is None
    assert row["row"] == 2
    assert row["fields"]["invoice_no"] == "SYN-INV-01"
    assert request["header_aliases"]["invoice_no"] == \
        list(MAIL_ALIAS_OVERLAY["invoice_no"])
    parsed = parse_mail_html(request["mail"]["html"], None,
                             aliases=request["header_aliases"])
    assert parsed.rows[0].fields["invoice_no"] == "SYN-INV-01"


def test_null_and_zero_table_index_are_distinct_coordinates(tmp_path):
    rows = [
        _mail_row(None, 2, mso="MSOTEXT", serial="STEXT", invoice_no="SYN-INV-01"),
        _mail_row(0, 2, mso="MSOHTML", serial="SHTML", invoice_no="SYN-INV-01"),
    ]
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    request = _build(snapshot, path, tmp_path)
    coords = {(row["table_index"], row["row"])
              for row in request["confirmed_input"]["mail_rows"]}
    assert coords == {(None, 2), (0, 2)}

    duplicated = copy.deepcopy(rows)
    duplicated.append(copy.deepcopy(duplicated[0]))
    _replace_rows(snapshot, duplicated)
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="重复正文行"):
        _build(snapshot, path, tmp_path)


@pytest.mark.parametrize("bad_index", ["0", 1.5, True, -1, []])
def test_invalid_table_index_types_rejected(tmp_path, bad_index):
    rows = [_mail_row(0, 2)]
    rows[0]["table_index"] = bad_index
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    _replace_rows(snapshot, rows)
    _write(path, snapshot)
    with pytest.raises(ValidationError, match="table_index"):
        _build(snapshot, path, tmp_path)


def test_invoice_overlay_preserves_caller_aliases_and_blocks_ambiguity(tmp_path):
    rows = [_mail_row(0, 2, order="12345", invoice_no="SYN-INV-01")]
    snapshot, path = _make_snapshot(tmp_path, mail_rows=rows)
    request = _build(snapshot, path, tmp_path,
                     header_aliases={"custom_note": ["Synthetic Note"]})
    assert request["header_aliases"]["custom_note"] == ["Synthetic Note"]
    assert request["header_aliases"]["invoice_no"] == \
        list(MAIL_ALIAS_OVERLAY["invoice_no"])

    # 调用方把发票列别名映射到 Order：规范表头归属冲突，必须阻断而非猜列。
    with pytest.raises(ValidationError, match="冲突"):
        _build(snapshot, path, tmp_path,
               header_aliases={"invoice_no": ["Order"]})
