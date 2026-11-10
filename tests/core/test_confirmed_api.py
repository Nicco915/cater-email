"""阶段 3 显式 confirmed_input 路径的核心契约测试（全合成数据）。

覆盖：schema/候选/资产完整性拒绝、正文防拼装（输出只由快照冻结行驱动；
可解析载体必须与冻结原始字段逐字段一致，篡改/夹带/删行即拒绝）、仅
序列号行的唯一配对 MSO 补认与歧义/无匹配阻断、确认冲突候选不得跳过、
已确认解析器（B/C）真实接管（非转发 preview facts）、缺组件如实技术
失败、以及旧单票契约不受 confirmed 模块影响。
"""
from __future__ import annotations

import hashlib
import html
import importlib
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest

from cater.core import api as core_api
from cater.core.api import ConfirmedInputError, process_job
from cater.core.models import FieldValue, PartRecord, Source, WeightGroup
from cater.core.parsing.machine_wb import MachineRow, MachineWorkbookResult
from cater.core.parsing.parts import PartsParseResult


# ---------------------------------------------------------------------------
# 合成材料与请求构造
# ---------------------------------------------------------------------------

MACHINE_REFERENCES = [
    {
        "model": "SEMTEST",
        "english_name": "SEMTEST SOIL COMPACTOR",
        "hs_code": "8429401900",
        "product_name": "压路机",
        "declaration": "1|0|合成申报要素|是自推进|100KG|山工品牌|SEMTEST|无|无|无",
        "destination_scope": "default",
    },
]

# 规范业务表标签（与 app 侧 confirmed_input 的公开契约一致：标签必须
# 经默认邮件别名归一到对应语义字段；此处只用本测试需要的字段）。
_LABELS = {
    "order": "Order",
    "dealer_order": "Dealer Order",
    "mso": "MSO",
    "dealer_code": "Dealer Code",
    "material": "Material",
    "serial_number": "Serial Number",
    "total_sales_price": "Total Sales Price",
}
_FIELD_ORDER = (
    "order", "dealer_order", "mso", "dealer_code", "material",
    "serial_number", "total_sales_price",
)

MACHINE_FIELDS = {
    "mso": "MSOT1",
    "serial_number": "S000001",
    "dealer_code": "DC01",
    "dealer_order": "DO-SYN-1",
    "order": "0620186623",
    "material": "SEMTEST",
    "total_sales_price": "1000",
}


def _render_mail_html(rows: list[dict]) -> str:
    """按公共契约的规范业务表渲染（thead/th + tbody/td，HTML 转义）。"""
    present = {name for row in rows for name in row["fields"]}
    columns = [f for f in _FIELD_ORDER if f in present]
    header = f"<tr><th>{'</th><th>'.join(html.escape(_LABELS[c], quote=True) for c in columns)}</th></tr>"
    body = []
    for row in rows:
        cells = "".join(
            f"<td>{html.escape(row['fields'].get(c, ''), quote=True)}</td>"
            for c in columns)
        body.append(f"<tr>{cells}</tr>")
    return ("<html><body><table><thead>" + header + "</thead><tbody>"
            + "".join(body) + "</tbody></table></body></html>")


def _mail_rows(fields_list: list[dict]) -> list[dict]:
    return [
        {
            "table_index": 0,
            "row": index + 2,
            "mso": fields.get("mso", ""),
            "serial": fields.get("serial_number", ""),
            "fields": fields,
        }
        for index, fields in enumerate(fields_list)
    ]


def _write_source(tmp_path: Path, name: str, content: bytes) -> tuple[Path, str]:
    path = tmp_path / name
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _machine_candidate(sha: str, filename: str, mail_rows: list[dict],
                       **overrides) -> dict:
    candidate = {
        "candidate_id": "cand-synt001",
        "kind": "machine",
        "identity": {
            "invoice_no": ["INV-SYN-001"],
            "mso": ["MSOT1"],
            "serial": ["S000001"],
        },
        "source_refs": [{"sha256": sha, "filenames": [filename], "regions": []}],
        "mail_row_refs": mail_rows,
        # preview 聚合事实：刻意放误导金额，证明输出绝不转发 preview facts
        "facts": {"amount": {"value": "99999.00", "sources": [], "conflict": False}},
        "issues": [],
        "needs_confirmation": False,
    }
    candidate.update(overrides)
    return candidate


def _request(tmp_path: Path, output_dir: Path, *, candidate: dict,
             mail_rows: list[dict], sources: list[dict], mail: dict | None,
             kind: str = "machine") -> dict:
    return {
        "task_id": "TEST-CONFIRMED-1",
        "kind": kind,
        "destination": {"name": "测试目的国", "alpha3": "TST"},
        "sources": sources,
        "mail": mail or {},
        "references": {
            "machines": MACHINE_REFERENCES,
            "countries": [],
            "machine_aliases": {},
            "authoritative": True,
        },
        "output_dir": str(output_dir),
        "overrides": [],
        "confirmed_input": {
            "schema_version": 1,
            "candidate": candidate,
            "mail_rows": mail_rows,
            "source_fingerprint": "f" * 64,
        },
    }


@pytest.fixture()
def machine_setup(tmp_path, output_dir):
    """一票已确认整机的完整合成请求 + 依赖注入点。"""
    rows = _mail_rows([dict(MACHINE_FIELDS)])
    src, sha = _write_source(tmp_path, "a-invoice.xlsx", b"synthetic-workbook-bytes")
    candidate = _machine_candidate(sha, "a-invoice.xlsx", rows)
    sources = [{"path": str(src), "filename": "a-invoice.xlsx", "sha256": sha}]
    request = _request(
        tmp_path, output_dir, candidate=candidate, mail_rows=rows,
        sources=sources, mail={"html": _render_mail_html(rows),
                               "message_id": "<syn@test>"})
    return {"request": request, "candidate": candidate, "rows": rows,
            "sources": sources, "src": src, "sha": sha,
            "output_dir": output_dir}


def _fake_machine_module(calls: list, wb_result: MachineWorkbookResult) -> types.ModuleType:
    module = types.ModuleType("cater.core.parsing.machine_confirmed")

    def parse_confirmed_machine_sources(sources, candidate, mail_rows, aliases=None):
        calls.append({"sources": sources, "candidate": candidate,
                      "mail_rows": mail_rows, "aliases": aliases})
        return wb_result

    module.parse_confirmed_machine_sources = parse_confirmed_machine_sources
    return module


def _machine_wb_result(mso: str = "MSOT1", serial: str = "S000001") -> MachineWorkbookResult:
    src = Source(type="workbook", filename="a-invoice.xlsx", sheet="CI", cell="A1")
    return MachineWorkbookResult(
        invoice_rows=[MachineRow(mso=mso, model="SEMTEST", loader=serial,
                                 pin="SYNPIN001", engine="SYNENG001", source=src)],
        packing_rows=[],
        weight_groups=[WeightGroup(msos=[mso], qty=1, net_weight=Decimal("100"),
                                   gross_weight=Decimal("110"), source=src)],
        invoice_no="INV-SYN-001",
        invoice_source=src,
        layout={"checks": [], "machine_table": [], "weight_table": []},
        issues=[],
    )


# ---------------------------------------------------------------------------
# 已确认整机路径：真正调用 B 解析器
# ---------------------------------------------------------------------------

def test_confirmed_machine_ready_uses_confirmed_parser(machine_setup, monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module(calls, _machine_wb_result()))

    resp = process_job(machine_setup["request"])

    assert resp["status"] == "ready", resp["issues"]
    assert resp["kind"] == "machine"
    assert [r["record_key"] for r in resp["records"]] == ["MSOT1"]
    fields = resp["records"][0]["fields"]
    # 价格来自已确认正文行（1000），净重来自 B 的重量组（100）——
    # 绝不转发 preview candidate.facts 里的误导金额 99999.00
    assert fields["total_sales_price"] == "1000"
    assert fields["net_weight"] == "100"
    assert "99999" not in str(resp["records"])
    assert len(resp["artifacts"]) == 2
    for artifact in resp["artifacts"]:
        assert Path(artifact["path"]).is_file()
    # B 确实以 (sources, candidate, mail_rows, aliases) 被调用，且 sources
    # 只含本票资产
    assert len(calls) == 1
    call = calls[0]
    assert [Path(s["path"]).name for s in call["sources"]] == ["a-invoice.xlsx"]
    assert call["candidate"]["candidate_id"] == "cand-synt001"
    assert call["mail_rows"] == machine_setup["rows"]
    assert call["aliases"] is not None


def test_confirmed_machine_without_confirmed_modules_fails_technically(
        machine_setup, monkeypatch):
    """缺 B 组件是技术失败（ConfirmedInputError），不得静默落回旧单票路径。"""
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed", None)
    with pytest.raises(ConfirmedInputError):
        process_job(machine_setup["request"])
    assert list(Path(machine_setup["output_dir"]).iterdir()) == []


# ---------------------------------------------------------------------------
# 仅序列号正文行：唯一机器配对补认 MSO，歧义/无匹配显式阻断
# ---------------------------------------------------------------------------

SERIAL_ONLY_FIELDS = {
    "serial_number": "S000001",
    "dealer_code": "DC01",
    "dealer_order": "DO-SYN-1",
    "order": "0620186623",
    "material": "SEMTEST",
    "total_sales_price": "1000",
}


def _serial_only_request(tmp_path, output_dir):
    rows = _mail_rows([dict(SERIAL_ONLY_FIELDS)])
    # 快照冻结行：无 MSO（mso 为空、fields 无 mso 键）
    rows[0]["mso"] = ""
    src, sha = _write_source(tmp_path, "a-invoice.xlsx", b"synthetic-workbook-bytes")
    candidate = _machine_candidate(sha, "a-invoice.xlsx", rows)
    sources = [{"path": str(src), "filename": "a-invoice.xlsx", "sha256": sha}]
    return _request(
        tmp_path, output_dir, candidate=candidate, mail_rows=rows,
        sources=sources,
        mail={"html": _render_mail_html(rows), "message_id": "<syn@test>"})


def test_confirmed_serial_only_row_adopts_unique_machine_mso(
        tmp_path, output_dir, monkeypatch):
    """仅序列号正文行：B 返回唯一机器配对时补认 MSO（保留邮件来源），
    正式 reconcile 照常完成；可信快照绝不被修改。"""
    request = _serial_only_request(tmp_path, output_dir)
    snapshot_fields = request["confirmed_input"]["mail_rows"][0]["fields"]
    calls: list = []
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module(calls, _machine_wb_result()))

    resp = process_job(request)

    assert resp["status"] == "ready", resp["issues"]
    assert [r["record_key"] for r in resp["records"]] == ["MSOT1"]
    fields = resp["records"][0]["fields"]
    assert fields["total_sales_price"] == "1000"
    assert fields["serial_number"] == "S000001"
    assert fields["net_weight"] == "100"
    # 可信快照未被补认改写（防拼装核对仍针对原始冻结字段）
    assert "mso" not in snapshot_fields


def test_confirmed_serial_only_unmatched_serial_blocks(
        tmp_path, output_dir, monkeypatch):
    """序列号在已确认工作簿机器清单中不存在：显式阻断，不猜 MSO。"""
    request = _serial_only_request(tmp_path, output_dir)
    monkeypatch.setitem(
        sys.modules, "cater.core.parsing.machine_confirmed",
        _fake_machine_module([], _machine_wb_result(serial="S999999")))

    resp = process_job(request)

    assert resp["status"] == "waiting_input"
    codes = {i["code"] for i in resp["issues"]}
    assert "MAIL_ROW_SERIAL_UNMATCHED" in codes
    assert resp["artifacts"] == []


def test_confirmed_serial_only_ambiguous_serial_blocks(
        tmp_path, output_dir, monkeypatch):
    """同一序列号对应多个 MSO：阻断且不默认取第一台。"""
    request = _serial_only_request(tmp_path, output_dir)
    src = Source(type="workbook", filename="a-invoice.xlsx")
    wb_result = MachineWorkbookResult(
        invoice_rows=[
            MachineRow(mso="MSOT1", model="SEMTEST", loader="S000001",
                       pin="P1", engine="E1", source=src),
            MachineRow(mso="MSOT2", model="SEMTEST", loader="S000001",
                       pin="P2", engine="E2", source=src),
        ],
        packing_rows=[],
        weight_groups=[],
        invoice_no="INV-SYN-001",
        invoice_source=src,
        layout={"checks": [], "machine_table": [], "weight_table": []},
        issues=[],
    )
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], wb_result))

    resp = process_job(request)

    assert resp["status"] == "waiting_input"
    codes = {i["code"] for i in resp["issues"]}
    assert "MAIL_ROW_SERIAL_AMBIGUOUS" in codes
    assert resp["artifacts"] == []


def test_confirmed_serial_only_rejects_row_without_identity(
        tmp_path, output_dir, monkeypatch):
    """仅序列号票据的冻结行若连序列号也没有：身份不完整即拒绝（先于任何
    补认），绝不会静默放行进入 reconcile。"""
    request = _serial_only_request(tmp_path, output_dir)
    row = request["confirmed_input"]["mail_rows"][0]
    row["fields"] = {k: v for k, v in row["fields"].items()
                     if k != "serial_number"}
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    with pytest.raises(ConfirmedInputError, match="身份不完整"):
        process_job(request)
    assert list(Path(output_dir).iterdir()) == []


# ---------------------------------------------------------------------------
# schema / 候选 / 资产完整性拒绝
# ---------------------------------------------------------------------------

def test_confirmed_rejects_bad_schema(machine_setup, monkeypatch):
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]
    request["confirmed_input"]["schema_version"] = 2
    with pytest.raises(ConfirmedInputError, match="schema"):
        process_job(request)
    request["confirmed_input"]["schema_version"] = 1
    del request["confirmed_input"]["candidate"]
    with pytest.raises(ConfirmedInputError, match="candidate"):
        process_job(request)


def test_confirmed_rejects_missing_fingerprint(machine_setup, monkeypatch):
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]
    request["confirmed_input"]["source_fingerprint"] = ""
    with pytest.raises(ConfirmedInputError, match="fingerprint"):
        process_job(request)


def test_confirmed_rejects_unconfirmed_candidate(machine_setup, monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module(calls, _machine_wb_result()))
    request = machine_setup["request"]
    candidate = request["confirmed_input"]["candidate"]

    candidate["needs_confirmation"] = True
    with pytest.raises(ConfirmedInputError, match="待人工确认"):
        process_job(request)

    candidate["needs_confirmation"] = False
    candidate["issues"] = [{"code": "AMOUNT_CONFLICT", "blocking": True}]
    with pytest.raises(ConfirmedInputError, match="阻断"):
        process_job(request)
    # 确认冲突候选绝不靠 confirmed_input 跳过：B 从未被调用
    assert calls == []
    assert list(Path(machine_setup["output_dir"]).iterdir()) == []


def test_confirmed_rejects_kind_mismatch(machine_setup, monkeypatch):
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]
    request["confirmed_input"]["candidate"]["kind"] = "parts"
    with pytest.raises(ConfirmedInputError):
        process_job(request)


def test_confirmed_rejects_tampered_source_bytes(machine_setup, monkeypatch):
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    machine_setup["src"].write_bytes(b"tampered-content")
    with pytest.raises(ConfirmedInputError, match="资产清单不一致"):
        process_job(machine_setup["request"])
    assert list(Path(machine_setup["output_dir"]).iterdir()) == []


def test_confirmed_rejects_extra_or_missing_sources(machine_setup, monkeypatch,
                                                    tmp_path):
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]

    # 夹带别票/额外文件：拒绝
    extra, _extra_sha = _write_source(tmp_path, "other-ticket.xlsx", b"other")
    boosted = dict(request)
    boosted["sources"] = request["sources"] + [
        {"path": str(extra), "filename": "other-ticket.xlsx"}]
    with pytest.raises(ConfirmedInputError, match="资产清单不一致"):
        process_job(boosted)

    # 少带资产：拒绝
    reduced = dict(request)
    reduced["sources"] = []
    with pytest.raises(ConfirmedInputError, match="资产清单不一致"):
        process_job(reduced)


# ---------------------------------------------------------------------------
# 正文防拼装：可解析的载体必须与冻结行逐字段一致；输出只由冻结行驱动
# ---------------------------------------------------------------------------

def test_confirmed_rejects_tampered_mail_price(machine_setup, monkeypatch):
    """载体价格被篡改（仍可解析为业务表）：与冻结行不一致即拒绝。"""
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]
    tampered_fields = dict(MACHINE_FIELDS, total_sales_price="9999")
    request["mail"] = {"html": _render_mail_html(_mail_rows([tampered_fields]))}
    with pytest.raises(ConfirmedInputError, match="正文"):
        process_job(request)
    assert list(Path(machine_setup["output_dir"]).iterdir()) == []


def test_confirmed_rejects_extra_mail_row(machine_setup, monkeypatch):
    """载体夹带额外行同样拒绝（不能靠拼装负载绕过金额核对）。"""
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]
    extra = dict(MACHINE_FIELDS, mso="MSOT2", serial_number="S000002")
    request["mail"] = {
        "html": _render_mail_html(_mail_rows([dict(MACHINE_FIELDS), extra]))}
    with pytest.raises(ConfirmedInputError, match="正文"):
        process_job(request)


def test_confirmed_rejects_dropped_mail_rows(machine_setup, monkeypatch):
    """载体删掉已确认行（行数变少但仍可解析）也是拒绝。"""
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]
    extra = dict(MACHINE_FIELDS, mso="MSOT2", serial_number="S000002",
                 total_sales_price="2000")
    rows = _mail_rows([dict(MACHINE_FIELDS), extra])
    request["confirmed_input"]["mail_rows"] = rows
    request["confirmed_input"]["candidate"]["mail_row_refs"] = rows
    request["mail"] = {"html": _render_mail_html(rows[:1])}
    with pytest.raises(ConfirmedInputError, match="正文"):
        process_job(request)


def test_confirmed_works_without_mail_payload(machine_setup, monkeypatch):
    """载体整体缺失（零业务行可解析）时不消费正文：冻结行自带身份与字段
    （含来源定位），输出只由冻结行驱动。"""
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]
    request["mail"] = {}

    resp = process_job(request)

    assert resp["status"] == "ready", resp["issues"]
    assert [r["record_key"] for r in resp["records"]] == ["MSOT1"]
    assert resp["records"][0]["fields"]["total_sales_price"] == "1000"


def test_confirmed_rejects_mail_row_without_identity(machine_setup,
                                                     monkeypatch):
    """冻结行既无 MSO 也无序列号：身份不完整属 schema 违规，先拒绝。"""
    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _fake_machine_module([], _machine_wb_result()))
    request = machine_setup["request"]
    row = request["confirmed_input"]["mail_rows"][0]
    row["fields"] = {k: v for k, v in row["fields"].items()
                     if k not in ("mso", "serial_number")}
    with pytest.raises(ConfirmedInputError, match="身份不完整"):
        process_job(request)
    assert list(Path(machine_setup["output_dir"]).iterdir()) == []


# ---------------------------------------------------------------------------
# 已确认配件路径：真正调用 C 解析器
# ---------------------------------------------------------------------------

def _parts_wb_result() -> PartsParseResult:
    src = Source(type="workbook", filename="kit-invoice.xlsx", sheet="CI", cell="A5")
    fields = {
        name: FieldValue(value, value, src)
        for name, value in {
            "qty": "2",
            "unit_price": "10.5",
            "amount": "21",
            "net_weight": "1.5",
            "hs_code": "8409999990",
            "product_name": "合成零件",
            "spec": "1|0|合成规格",
            "chinese_name": "合成零件",
            "coo": "CN",
        }.items()
    }
    record = PartRecord(part_code="SYN-PART-001", fields=fields, row_index=0)
    return PartsParseResult(
        sheet_name="CI",
        records=[record],
        gross_weight=Decimal("2"),
        packages=Decimal("1"),
        invoice_no="KIT-SYN-001",
        gross_source=src,
        packages_source=src,
        invoice_source=src,
        layout={"checks": [], "parts_invoice": {}, "packing_total": {}},
        issues=[],
    )


def test_confirmed_parts_ready_uses_confirmed_parser(tmp_path, output_dir,
                                                     monkeypatch):
    rows = _mail_rows([])  # 配件路径不使用正文
    src, sha = _write_source(tmp_path, "kit-invoice.xlsx", b"synthetic-kit-bytes")
    candidate = {
        "candidate_id": "cand-synkit1",
        "kind": "parts",
        "identity": {"invoice_no": ["KIT-SYN-001"]},
        "source_refs": [{"sha256": sha, "filenames": ["kit-invoice.xlsx"], "regions": []}],
        "mail_row_refs": [],
        "facts": {"amount": {"value": "123456.00", "sources": [], "conflict": False}},
        "issues": [],
        "needs_confirmation": False,
    }
    request = _request(
        tmp_path, output_dir, candidate=candidate, mail_rows=rows,
        sources=[{"path": str(src), "filename": "kit-invoice.xlsx", "sha256": sha}],
        mail={}, kind="parts")
    # 原产国 CN 需要可映射的国家参考（authoritative 快照，不用内置表兜底）
    request["references"]["countries"] = [{
        "alpha3": "CHN", "name": "中国", "name_zh": "中国",
        "name_en": "China", "aliases": ["CN"],
    }]

    calls: list = []
    module = types.ModuleType("cater.core.parsing.kit_confirmed")

    def parse_confirmed_kit_sources(sources, candidate, mail_rows, aliases=None):
        calls.append({"sources": sources, "candidate": candidate})
        return _parts_wb_result()

    module.parse_confirmed_kit_sources = parse_confirmed_kit_sources
    monkeypatch.setitem(sys.modules, "cater.core.parsing.kit_confirmed", module)

    resp = process_job(request)

    assert resp["status"] == "ready", resp["issues"]
    assert resp["kind"] == "parts"
    assert [r["record_key"] for r in resp["records"]] == ["SYN-PART-001"]
    # 输出记录来自 C 的解析结果，不是 preview facts
    assert "123456" not in str(resp["records"])
    assert len(resp["artifacts"]) == 1
    assert len(calls) == 1
    assert [Path(s["path"]).name for s in calls[0]["sources"]] == ["kit-invoice.xlsx"]


def test_confirmed_parts_component_missing_fails(tmp_path, output_dir,
                                                 monkeypatch):
    src, sha = _write_source(tmp_path, "kit-invoice.xlsx", b"synthetic-kit-bytes")
    candidate = {
        "candidate_id": "cand-synkit1",
        "kind": "parts",
        "identity": {"invoice_no": ["KIT-SYN-001"]},
        "source_refs": [{"sha256": sha, "filenames": ["kit-invoice.xlsx"], "regions": []}],
        "mail_row_refs": [],
        "facts": {},
        "issues": [],
        "needs_confirmation": False,
    }
    request = _request(
        tmp_path, output_dir, candidate=candidate, mail_rows=[],
        sources=[{"path": str(src), "filename": "kit-invoice.xlsx", "sha256": sha}],
        mail={}, kind="parts")
    monkeypatch.setitem(sys.modules, "cater.core.parsing.kit_confirmed", None)
    with pytest.raises(ConfirmedInputError):
        process_job(request)


# ---------------------------------------------------------------------------
# 旧单票契约回归：无 confirmed_input 时绝不触碰 confirmed 模块
# ---------------------------------------------------------------------------

def test_legacy_job_never_loads_confirmed_modules(machine_request, monkeypatch):
    class _Exploding(types.ModuleType):
        def __getattr__(self, name):  # pragma: no cover - 防御
            raise AssertionError("旧单票路径不得调用已确认解析器")

    monkeypatch.setitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        _Exploding("cater.core.parsing.machine_confirmed"))
    monkeypatch.setitem(sys.modules, "cater.core.parsing.kit_confirmed",
                        _Exploding("cater.core.parsing.kit_confirmed"))
    resp = process_job(machine_request)
    assert resp["status"] == "ready", resp["issues"]
    assert resp["kind"] == "machine"


def test_confirmed_modules_not_imported_by_legacy_import(monkeypatch):
    """core.api 模块自身不 eager 引入 B/C（懒加载只在显式路径）。"""
    monkeypatch.delitem(sys.modules, "cater.core.parsing.machine_confirmed",
                        raising=False)
    monkeypatch.delitem(sys.modules, "cater.core.parsing.kit_confirmed",
                        raising=False)
    monkeypatch.delitem(sys.modules, "cater.core.api", raising=False)
    importlib.import_module("cater.core.api")
    assert "cater.core.parsing.machine_confirmed" not in sys.modules
    assert "cater.core.parsing.kit_confirmed" not in sys.modules
