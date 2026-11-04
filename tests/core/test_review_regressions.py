"""审核回归：金额相等性（不容差掩盖差异）、Order 双来源补零、
Material 显式别名同义、国家/机型别名比较契约、未确认特殊规则暂停。
"""
from decimal import Decimal

from cater.core.models import MachineRecord, Source, WeightGroup
from cater.core.reconcile import _add_field, reconcile_machines
from cater.core.references import CountryIndex, MachineIndex


def _src(t="mail_html"):
    return Source(type=t, message_id="<m>")


def _rec(mso="BFWVU"):
    return MachineRecord(mso=mso)


# ---- 1. 金额相等性：仅浮点尾差视为相等，真实差异必须冲突 ----

def test_price_float_tail_equal_merges():
    """Excel 二进制尾差（相对 ~1e-16）视为同一价格，双来源合并不冲突。"""
    rec, issues = _rec(), []
    _add_field(rec, "total_sales_price", "2.8", _src(), issues)
    _add_field(rec, "total_sales_price", 2.8000000000000003, _src("workbook"), issues)
    assert issues == []
    assert rec.fields["total_sales_price"].value == Decimal("2.8")
    assert rec.fields["total_sales_price"].source.type == "merged"


def test_price_real_difference_conflicts():
    """真实价格差异（含分位级）必须 FIELD_CONFLICT，不用精度容差掩盖。"""
    for a, b in (("68822.84", "69847.84"),      # 样例新旧两版
                 ("100.00", "100.005"),          # 分位级差异
                 ("0.01", "0.02")):
        rec, issues = _rec(), []
        _add_field(rec, "total_sales_price", a, _src(), issues)
        _add_field(rec, "total_sales_price", b, _src("workbook"), issues)
        assert [i.code for i in issues] == ["FIELD_CONFLICT"], (a, b, issues)
        assert a in issues[0].message and b in issues[0].message


def test_price_zero_not_special_cased():
    """零值金额不参与相对容差：0 与任何非零值都是冲突。"""
    rec, issues = _rec(), []
    _add_field(rec, "total_sales_price", "0", _src(), issues)
    _add_field(rec, "total_sales_price", "0.0000001", _src("workbook"), issues)
    assert [i.code for i in issues] == ["FIELD_CONFLICT"]


# ---- 2. Order(SO) 双来源：比较前补零，620186623 == 0620186623 ----

def test_order_dual_source_leading_zero_synonym():
    rec, issues = _rec(), []
    _add_field(rec, "order", "620186623", _src(), issues)
    _add_field(rec, "order", "0620186623", _src("workbook"), issues)
    assert issues == []
    assert rec.fields["order"].value == "0620186623"   # 规范值补零
    assert rec.fields["order"].source.type == "merged"


def test_order_dual_source_real_difference_conflicts():
    rec, issues = _rec(), []
    _add_field(rec, "order", "620186623", _src(), issues)
    _add_field(rec, "order", "0620186624", _src("workbook"), issues)
    assert [i.code for i in issues] == ["FIELD_CONFLICT"]


def test_order_first_value_normalized_at_intake():
    rec, issues = _rec(), []
    _add_field(rec, "order", "620186623", _src(), issues)
    assert rec.fields["order"].value == "0620186623"


# ---- 3. Material 显式别名同义（双来源核对） ----

def _mail_fields(material="SEM520高配"):
    return {"mso": "BFWVU", "dealer_code": "JM28", "dealer_order": "TSUJKT1371",
            "order": "0620186623", "material": material,
            "serial_number": "S8Z00583", "total_sales_price": "68,822.84"}


class _MailRow:
    def __init__(self, fields):
        self.fields = fields
        self.source = _src()


class _WbRow:
    def __init__(self, mso, model, loader):
        self.mso = mso
        self.model = model
        self.loader = loader
        self.pin = None
        self.engine = None
        self.source = _src("workbook")


def _idx_with_alias():
    return MachineIndex(
        machines=[{"model": "SEM520", "hs_code": "8429401100",
                   "destination_scope": "default"}],
        aliases={"SEM520高配": "SEM520"})


def _weight_group():
    return [WeightGroup(msos=["BFWVU"], qty=1, net_weight=Decimal("10000"),
                        gross_weight=None, source=_src("workbook"))]


def test_material_alias_synonym_no_conflict():
    """邮件 Material=别名、箱单 MODEL=标准名 → 同义合并，不冲突。"""
    idx = _idx_with_alias()
    res = reconcile_machines(
        [_MailRow(_mail_fields())], [], [_WbRow("BFWVU", "SEM520", "S8Z00583")],
        _weight_group(), machine_index=idx)
    assert [i.code for i in res.issues] == [], res.issues
    fv = res.records[0].fields["material"]
    assert fv.source.type == "merged"
    assert len(res.records[0].fields["material"].raw) == 2


def test_material_real_difference_conflicts_even_with_alias():
    idx = _idx_with_alias()
    res = reconcile_machines(
        [_MailRow(_mail_fields("SEM512"))], [], [_WbRow("BFWVU", "SEM520", "S8Z00583")],
        _weight_group(), machine_index=idx)
    assert [i.code for i in res.issues] == ["FIELD_CONFLICT"]
    assert res.issues[0].field == "material"


def test_material_case_whitespace_synonym():
    """机型比较大小写/多余空白不敏感（规范化比较），但不是模糊匹配。"""
    idx = _idx_with_alias()
    res = reconcile_machines(
        [_MailRow(_mail_fields(" sem520 "))], [], [_WbRow("BFWVU", "SEM520", "S8Z00583")],
        _weight_group(), machine_index=idx)
    assert not any(i.code == "FIELD_CONFLICT" for i in res.issues)


# ---- 4. 国家/机型别名比较契约回归 ----

def test_country_alias_contract():
    """大小写/空白规范化、内置名不被引用别名覆盖、冲突拒绝、未知返回 None。"""
    idx = CountryIndex()
    assert idx.resolve("us") == "USA"
    assert idx.resolve("  u s  ") is None          # 内部空白不消除，非模糊
    assert idx.resolve("United States") == "USA"
    assert idx.resolve("FRANCE") == "FRA"
    assert idx.resolve("Atlantis") is None
    # 引用别名补充新映射
    idx2 = CountryIndex([{"alpha3": "TGO", "name": "多哥", "aliases": ["Togolese"]}])
    assert idx2.resolve("togolese") == "TGO"
    assert idx2.conflicts == []
    # 别名冲突：拒绝覆盖既有映射
    idx3 = CountryIndex([{"alpha3": "FRA", "name": "X", "aliases": ["US"]}])
    assert idx3.conflicts
    assert idx3.resolve("US") == "USA"
    # 两个引用互相冲突
    idx4 = CountryIndex([{"alpha3": "FRA", "name": "K1", "aliases": ["KK"]},
                         {"alpha3": "TGO", "name": "K2", "aliases": ["KK"]}])
    assert len(idx4.conflicts) == 1
    assert idx4.resolve("KK") == "FRA"   # 先到先得，后者拒绝


def test_machine_alias_contract():
    """别名命中标准条目、alias_target 标准化、无模糊匹配、冲突登记。"""
    idx = MachineIndex(
        machines=[{"model": "SEM520", "hs_code": "8429401100",
                   "destination_scope": "default"}],
        aliases={"SEM520高配": "SEM520", "高配": "SEM520"})
    assert idx.match("SEM520")["hs_code"] == "8429401100"
    assert idx.match("sem520 ")["hs_code"] == "8429401100"    # 规范化
    assert idx.match("SEM520高配")["hs_code"] == "8429401100"
    assert idx.match("高配")["hs_code"] == "8429401100"
    assert idx.match("SEM52") is None                          # 不模糊
    assert idx.alias_target("SEM520高配") == "SEM520"
    assert idx.alias_target("SEM520") == "SEM520"
    # 同 scope 不一致条目与别名冲突都进 issues
    bad = MachineIndex(machines=[{"model": "SEM520", "hs_code": "1"},
                                 {"model": "SEM520", "hs_code": "2"}],
                       aliases={"A": "X", "A": "Y"})
    assert bad.issues


def test_machine_scope_alias_combination():
    """别名跨 scope 指向同一标准机型，按目的国选变体。"""
    idx = MachineIndex(
        machines=[{"model": "915F", "hs_code": "8429209000",
                   "destination_scope": "default", "declaration": "110KW"},
                  {"model": "915F", "hs_code": "8429209000",
                   "destination_scope": "BRA", "declaration": "123KW",
                   "special_rule_confirmed": True}],
        aliases={"915F高配": "915F"})
    assert "123KW" in idx.match("915F高配", "BRA")["declaration"]
    assert "110KW" in idx.match("915F高配", "IDN")["declaration"]
    assert idx.alias_target("915F高配") == "915F"


# ---- 5. 未确认特殊规则：暂停匹配并登记问题 ----

def test_unconfirmed_special_rule_paused():
    """带特殊标记但未获确认规则的机型：任何目的国都不静默匹配。"""
    idx = MachineIndex(
        machines=[{"model": "SEM919BRZ", "hs_code": "8429209000",
                   "destination_scope": "BRA", "special": "巴西",
                   "special_rule_confirmed": False, "source_row": 48}])
    assert idx.match("SEM919BRZ", "BRA") is None
    assert idx.match("SEM919BRZ", "IDN") is None
    assert "SEM919BRZ" in idx.deferred_unconfirmed
    # 确认过的 915F 不受影响的反向验证
    idx2 = MachineIndex(
        machines=[{"model": "915F", "hs_code": "8429209000",
                   "destination_scope": "BRA", "special": "巴西",
                   "special_rule_confirmed": True}])
    assert idx2.match("915F", "BRA") is not None
    assert idx2.deferred_unconfirmed == {}


def test_transform_issues_unconfirmed_special_rule():
    """build_machine_rows 对未确认机型登记 UNCONFIRMED_SPECIAL_RULE 并跳过。"""
    from cater.core.models import FieldValue
    from cater.core.transform import build_machine_rows
    idx = MachineIndex(
        machines=[{"model": "SEM919BRZ", "hs_code": "8429209000",
                   "destination_scope": "BRA", "special": "巴西",
                   "special_rule_confirmed": False, "source_row": 48}])
    rec = MachineRecord(mso="MSO1")
    for k, v in {"dealer_code": "JM28", "dealer_order": "DO1",
                 "order": "0620186623", "material": "SEM919BRZ",
                 "serial_number": "SN1", "total_sales_price": "1000",
                 "net_weight": Decimal("5000")}.items():
        rec.fields[k] = FieldValue(v, v, _src())
    issues = []
    mrows, crows, _, _ = build_machine_rows([rec], idx,
                                            {"name": "巴西", "alpha3": "BRA"}, issues)
    assert mrows == [] and crows == []
    assert [i.code for i in issues] == ["UNCONFIRMED_SPECIAL_RULE"]
    assert "48" in issues[0].message and "巴西" in issues[0].message
