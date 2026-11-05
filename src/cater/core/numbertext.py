"""数字、标识符与特定文本清洗。

规则来源 plan.md §5/§11：
- 金额与净重使用 Decimal，不从显示字符串引入千分位歧义；
- Excel 二进制尾差在明确精度内处理（各字段按模板允许的最大小数位量化）；
- SO 无前导零补一个，已有则保留，按文本存储；
- 规格型号仅将开头的 ``1||`` 改为 ``1|``（已确认覆盖
  ``1||不适合进口报关`` 与 ``1||3|`` 两种实际样例），
  后续连续竖线（含空项）保持不变，不压缩申报要素分隔符。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

# 模板各字段允许的最大小数位（台账/企业物料填写规范）
MAX_DP_PRICE = 4      # 申报单价：整数最多14位、小数最多4位
MAX_DP_WEIGHT = 5     # 净重/毛重：小数最多5位
MAX_DP_QTY = 5        # 申报数量：小数最多5位

_NUM_CLEAN_RE = re.compile(r"[,，\s\xa0$¥￥]")

# 已确认的窄清洗规则：仅开头 1|| 改为 1|（多一个空项），不做通用 collapse
_SPEC_PREFIX_BAD = "1||"
_SPEC_PREFIX_GOOD = "1|"


def to_decimal(value) -> Optional[Decimal]:
    """把 Excel 单元格值或邮件文本转成 Decimal；无法解析返回 None。

    接受 int/float/Decimal/str；字符串先去掉千分位逗号、货币符号、空格和 nbsp。
    float 经 str() 中转，保留来源有效精度。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = _NUM_CLEAN_RE.sub("", value).strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def decimal_to_text(value: Decimal, max_dp: int) -> str:
    """按模板允许精度把 Decimal 写成文本。

    量化到 max_dp 位小数并去掉多余的尾零；整数不带小数点。
    Excel 二进制尾差（如 2.8000000000000003）在此精度内归一为 2.8。
    """
    quantum = Decimal(1) if max_dp == 0 else Decimal(1).scaleb(-max_dp)
    q = value.quantize(quantum)
    if q == q.to_integral_value():
        return str(q.to_integral_value())
    text = format(q.normalize(), "f")
    return text


def decimals_close(a: Decimal, b: Decimal, max_dp: int) -> bool:
    """在指定小数位精度内比较两个 Decimal（容差为半个最小分位）。

    仅用于重量等非金额字段；金额相等性见 reconcile._price_equal。
    """
    tol = Decimal(1).scaleb(-max_dp) / 2
    return abs(a - b) <= tol


def decimal_to_excel_number(value) -> object:
    """Decimal（或数值文本）→ Excel 安全数值：整数存 int，其余存 float。

    金额/数量/重量在输出模板中按数值存储；float 与 Excel 同为双精度，
    量化后的值（≤4 位小数金额、≤5 位小数重量）无语义损失。
    """
    d = value if isinstance(value, Decimal) else to_decimal(value)
    if d is None:
        raise ValueError(f"数值字段无法解析：{value!r}")
    if d == d.to_integral_value():
        return int(d)
    return float(d)


def normalize_so(text: str) -> str:
    """SO（Order）：去掉首尾空白；无前导零则补一个，已有则保留。按文本存储。"""
    t = (text or "").strip().replace("\xa0", "").strip()
    if not t:
        return t
    if t.startswith("0"):
        return t
    return "0" + t


def clean_spec(text: str) -> str:
    """规格型号清洗：仅替换开头的 ``1||`` 为 ``1|``（删除开头多出的一个空项）。

    其余内容（含其他连续竖线与空项）原样保留，不做通用 collapse。
    """
    if text is None:
        return text
    if text.startswith(_SPEC_PREFIX_BAD):
        return _SPEC_PREFIX_GOOD + text[len(_SPEC_PREFIX_BAD):]
    return text


def normalize_identifier(text) -> str:
    """标识符规范化：去空白/全角空格，不改大小写（大小写敏感场景各自处理）。"""
    if text is None:
        return ""
    return str(text).replace("\xa0", " ").strip()
