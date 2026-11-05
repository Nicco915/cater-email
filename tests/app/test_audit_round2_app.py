"""第二轮审计的应用层验收：语义角色差异对比、header_aliases 配置全链、
skipped 核对的正文渲染。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from cater.app.application import _diff_layout_structure
from cater.app.config import CoreConfig, load_config
from cater.app.delivery import DeliveryService

from helpers import make_config


def _machine_layout(sheet_invoice: str, with_weight: bool = True) -> dict:
    layout = {
        "sheets": [sheet_invoice, "PL"],
        "machine_table": [{"sheet": sheet_invoice, "header_row": 56,
                           "columns": {"model": "B", "serial": "E", "mso": "O"}}],
        "weight_table": [],
        "checks": [],
    }
    if with_weight:
        layout["weight_table"].append(
            {"sheet": "PL", "header_row": 28, "subheader_row": 29,
             "columns": {"description": "A", "qty": "M", "net": "S"}})
    return layout


# ---------------------------------------------------------------------------
# 审计(1)：差异对比按语义角色配对——sheet 重命名/表增删必须可见
# ---------------------------------------------------------------------------

def test_diff_reports_sheet_rename() -> None:
    baseline = _machine_layout("CI")
    current = _machine_layout("Commercial Invoice")
    changes = _diff_layout_structure(baseline, current)
    assert any("工作表 CI→Commercial Invoice" in c for c in changes)


def test_diff_reports_added_and_removed_tables() -> None:
    baseline = _machine_layout("CI", with_weight=False)
    current = _machine_layout("CI", with_weight=True)
    added = _diff_layout_structure(baseline, current)
    assert any("新增" in c and "箱单重量表" in c for c in added)
    removed = _diff_layout_structure(current, baseline)
    assert any("移除" in c and "箱单重量表" in c for c in removed)


def test_diff_matches_by_role_not_sheet_name() -> None:
    """machine_table#0 对 machine_table#0：即使两张布局 sheet 名完全不同，
    列位/表头行差异仍必须列出（按角色而非名字配对）。"""
    baseline = _machine_layout("CI")
    current = _machine_layout("Renamed")
    current["machine_table"][0]["columns"]["model"] = "D"
    current["machine_table"][0]["header_row"] = 60
    changes = _diff_layout_structure(baseline, current)
    text = "\n".join(changes)
    assert "机型" in text and "B→D" in text
    assert "表头行 56→60" in text


def test_diff_empty_when_structure_identical() -> None:
    a = _machine_layout("CI")
    b = _machine_layout("CI")
    b["checks"] = [{"rule": "X", "record_key": "R1", "result": "pass",
                    "detail": "含真实值的证据不影响结构差异"}]
    assert _diff_layout_structure(a, b) == []


# ---------------------------------------------------------------------------
# 审计(8)：header_aliases 本地配置 → 请求快照 → 核心（配件与整机共用）
# ---------------------------------------------------------------------------

def test_load_config_parses_core_header_aliases(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[core.header_aliases]\n"
        'unit_price = ["Final Price", "结算价"]\n'
        'model = ["Type"]\n',
        encoding="utf-8",
    )
    config = load_config(config_path, runtime_dir=tmp_path / "runtime")
    assert config.core.header_aliases == {
        "unit_price": ["Final Price", "结算价"],
        "model": ["Type"],
    }


def test_load_config_rejects_bad_header_aliases(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[core]\nheader_aliases = 42\n", encoding="utf-8")
    try:
        load_config(config_path, runtime_dir=tmp_path / "runtime")
    except Exception as exc:  # ConfigurationError
        assert "header_aliases" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("bad header_aliases must be rejected")


def test_service_threads_header_aliases_into_request(
    service_factory, tmp_path: Path
) -> None:
    from helpers import FakeEngine

    engine = FakeEngine()
    config = make_config(tmp_path)
    config = replace(config,
                     core=CoreConfig(header_aliases={"unit_price": ["Final Price"]}))
    service = service_factory(config, engine=engine)
    source = tmp_path / "business.xlsx"
    source.write_bytes(b"fake-xlsx-input")
    service.submit_offline_sources(
        sender="tester@example.com",
        destination="法国",
        source_paths=[source],
    )
    assert engine.calls, "engine must have processed the request"
    request = engine.calls[-1]
    assert request.get("header_aliases") == {"unit_price": ["Final Price"]}
    # 请求快照随版本持久化（重试不依赖当时配置）
    task = service.tasks.list()[0]
    snapshot = json.loads(
        (service.config.tasks_dir / task["id"] / "v1" / "request.json")
        .read_text(encoding="utf-8")
    )
    assert snapshot["header_aliases"] == {"unit_price": ["Final Price"]}


def test_default_config_sends_no_alias_key(service_factory, tmp_path: Path) -> None:
    from helpers import FakeEngine

    engine = FakeEngine()
    service = service_factory(make_config(tmp_path), engine=engine)
    source = tmp_path / "business.xlsx"
    source.write_bytes(b"fake-xlsx-input")
    service.submit_offline_sources(
        sender="tester@example.com",
        destination="法国",
        source_paths=[source],
    )
    assert "header_aliases" not in engine.calls[-1]


# ---------------------------------------------------------------------------
# 审计(4)应用层：skipped 核对在正文渲染为“未执行”，不得标通过
# ---------------------------------------------------------------------------

def test_layout_lines_render_skipped_checks() -> None:
    lines = DeliveryService._layout_lines({
        "sheets": ["CI-For Dealer (2)"],
        "checks": [
            {"rule": "A", "result": "pass", "detail": "一致"},
            {"rule": "B", "result": "skipped", "detail": "表头HS列为空，HS 比对未执行"},
            {"rule": "C", "result": "fail", "detail": "不符"},
        ],
    })
    text = "\n".join(lines)
    assert "- 通过：一致" in text
    assert "- 未执行（条件不足，跳过）：表头HS列为空" in text
    assert "- 未通过：不符" in text
