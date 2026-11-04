"""SO、规格型号清洗、Decimal 量化与国家映射单元测试。"""
from decimal import Decimal

from cater.core.numbertext import (clean_spec, decimal_to_text, decimals_close,
                                   normalize_so, to_decimal)
from cater.core.references import CountryIndex, MachineIndex


def test_so_adds_single_leading_zero():
    assert normalize_so("620186623") == "0620186623"
    assert normalize_so("0620186624") == "0620186624"
    assert normalize_so(" 620186623 ") == "0620186623"
    assert normalize_so("0620186623") == "0620186623"


def test_spec_cleaning_only_confirmed_prefix():
    assert clean_spec("1||不适合进口报关|碳素钢|无") == "1|不适合进口报关|碳素钢|无"
    # 其他连续竖线保持不变
    assert clean_spec("1|3|a|||b") == "1|3|a|||b"
    assert clean_spec("境内自主品牌|不适合进口报关|;x") == "境内自主品牌|不适合进口报关|;x"
    # 非确认形态的开头 1|| 不动
    assert clean_spec("1||其他|x") == "1||其他|x"


def test_decimal_text_quantization():
    assert decimal_to_text(Decimal("2.8000000000000003"), 5) == "2.8"
    assert decimal_to_text(Decimal("28.24"), 4) == "28.24"
    assert decimal_to_text(Decimal("20000"), 5) == "20000"
    assert decimal_to_text(Decimal("0.635924"), 4) == "0.6359"
    assert decimals_close(Decimal("2.8000000000000003"), Decimal("2.8"), 5)


def test_to_decimal_strips_thousands():
    assert to_decimal("68,822.84") == Decimal("68822.84")
    assert to_decimal("$\xa0\xa0 66,757.84") == Decimal("66757.84")
    assert to_decimal(2.8000000000000003) == Decimal("2.8000000000000003")
    assert to_decimal("abc") is None


def test_country_index_builtin_and_alias_conflict():
    idx = CountryIndex()
    assert idx.resolve("US") == "USA"
    assert idx.resolve("CN") == "CHN"
    assert idx.resolve("法国") == "FRA"
    assert idx.resolve("多哥") == "TGO"
    assert idx.resolve("Atlantis") is None
    idx2 = CountryIndex([{"alpha3": "FRA", "name": "法国", "aliases": ["法兰西"]}])
    assert idx2.resolve("法兰西") == "FRA"
    # 别名冲突拒绝：把 US 映射到 FRA
    idx3 = CountryIndex([{"alpha3": "FRA", "name": "X", "aliases": ["US"]}])
    assert idx3.conflicts
    assert idx3.resolve("US") == "USA"  # 旧映射不被覆盖


def test_machine_index_alias():
    idx = MachineIndex(
        machines=[{"model": "SEM520", "hs_code": "8429401100"}],
        aliases={"SEM520高配": "SEM520"})
    assert idx.match("SEM520")["hs_code"] == "8429401100"
    assert idx.match("SEM520高配")["hs_code"] == "8429401100"
    assert idx.alias_target("SEM520高配") == "SEM520"
    assert idx.match("SEM52") is None  # 不静默模糊匹配
