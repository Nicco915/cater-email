"""第三轮审计应用层验收：首次使用正文必须列出字段列位（不依赖差异
对比/基线），自动窄规范化（1|| → 1|）必须出现在“实际修正”节且保留
内部竖线。"""
from __future__ import annotations

import json

from helpers import make_config  # noqa: F401  (conftest 提供 service/offline_source)

from cater.app import state

from test_flexible_input_app import PARTS_LAYOUT, _delivery_body, _submit


def test_first_use_body_lists_field_positions(service, offline_source) -> None:
    """首次成功（无基线、无差异）也必须给出数量/单价等关键字段的
    来源列位与表头行，使收件人能独立追溯取值位置。"""
    service.engine.layout = dict(PARTS_LAYOUT)
    task = _submit(service, offline_source)
    assert task["status"] == state.READY_TO_SEND
    body = _delivery_body(service, task["id"])
    assert "字段列位（本版采用）：" in body
    assert "最终发票表（工作表 CI-For Dealer (2)）：表头第17行" in body
    assert "料号→B" in body and "数量→F" in body and "单价→H" in body
    assert "第18行次表头" in body          # 双行表头取值说明
    assert "补充HS列→L" in body
    assert "箱单TOTAL（工作表 Packing List）" in body
    assert "人工重点核对建议" in body      # 首次使用提示保留


def test_confirmed_layout_still_lists_field_positions(
    service, offline_source
) -> None:
    """已确认布局同样列出列位（独立于差异对比）。"""
    service.engine.layout = dict(PARTS_LAYOUT)
    task = _submit(service, offline_source)
    service.confirm_layout(task["id"], actor="test-admin")
    service.reprocess_task(task["id"], actor="test-admin")
    body = _delivery_body(service, task["id"])
    assert "字段列位（本版采用）：" in body
    assert "料号→B" in body
    assert "结构变化" not in body  # 布局未变，无差异节


def test_auto_normalization_shown_in_corrections(service, offline_source) -> None:
    """自动 clean_spec 窄规范化必须与实际修正同列展示，且摘要保留
    规格型号内部的全部竖线。"""
    layout = json.loads(json.dumps(PARTS_LAYOUT))
    layout["corrections"] = [{
        "record_key": "SYNP100", "field": "spec",
        "before": "1||3|用于SEM品牌|无",
        "after": "1|3|用于SEM品牌|无",
        "summary": "料号 SYNP100 规格型号自动规范化（删除开头多余空项）："
                   "1||3|用于SEM品牌|无 → 1|3|用于SEM品牌|无"
                   "（CI-For Dealer (2)!N13）",
        "source": {"type": "workbook", "filename": "x.xlsx",
                   "sheet": "CI-For Dealer (2)", "cell": "N13"},
    }]
    service.engine.layout = layout
    task = _submit(service, offline_source)
    body = _delivery_body(service, task["id"])
    assert "实际修正（自动规范化与按您回复的修正项）：" in body
    assert "1||3|用于SEM品牌|无 → 1|3|用于SEM品牌|无" in body
    assert "（CI-For Dealer (2)!N13）" in body
