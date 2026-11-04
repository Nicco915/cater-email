"""核心内部数据模型。

边界（process_job 出入参）一律使用 JSON 可序列化 dict；
dataclass 仅供 core 内部使用，不被 app 导入（见 plan/execution.md 公共接口 v1）。
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from typing import Any, Optional


@dataclass
class Source:
    """单个值的来源定位。"""

    type: str  # workbook | mail_html | mail_text | reference | fixed | user_input | computed | override
    message_id: Optional[str] = None
    filename: Optional[str] = None
    sheet: Optional[str] = None
    cell: Optional[str] = None
    table_index: Optional[int] = None
    row: Optional[int] = None
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "type": self.type,
            "message_id": self.message_id,
            "filename": self.filename,
            "sheet": self.sheet,
            "cell": self.cell,
            "table_index": self.table_index,
            "row": self.row,
            "detail": self.detail,
        }.items() if v is not None}


@dataclass
class Issue:
    code: str
    message: str
    record_key: Optional[str] = None
    field: Optional[str] = None
    sources: list = dc_field(default_factory=list)  # list[Source]
    required_action: str = ""
    blocking: bool = True

    def to_dict(self) -> dict:
        d = {
            "code": self.code,
            "record_key": self.record_key,
            "field": self.field,
            "message": self.message,
            "sources": [s.to_dict() if isinstance(s, Source) else s for s in self.sources],
            "required_action": self.required_action,
            "blocking": self.blocking,
        }
        return d


@dataclass
class FieldValue:
    """某记录某字段的原值、规范值与来源。"""

    raw: Any
    value: Any
    source: Source


@dataclass
class MachineRecord:
    """整机：按 MSO 合并后的标准记录。"""

    mso: str
    fields: dict = dc_field(default_factory=dict)  # field_name -> FieldValue
    order_index: int = 0  # 邮件业务表中的顺序，用于输出行排序


@dataclass
class PartRecord:
    """配件：一行明细的标准记录。"""

    part_code: str
    fields: dict = dc_field(default_factory=dict)  # field_name -> FieldValue
    row_index: int = 0


@dataclass
class WeightGroup:
    """箱单重量组：一组 MSO 共享一组净重/数量。"""

    msos: list
    qty: int
    net_weight: Decimal
    gross_weight: Optional[Decimal]
    source: Source
