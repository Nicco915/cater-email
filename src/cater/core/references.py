"""配置匹配：国家与机型参考资料的精确匹配。

规则（plan.md §9）：
- 精确匹配和显式别名，不静默模糊匹配；
- 别名唯一，冲突拒绝（旧映射不得静默覆盖）；
- 机型匹配保留后缀/特殊配置，不任意删除 SEM 前缀或中文配置说明。
"""
from __future__ import annotations

from typing import Optional

from .countries import BUILTIN_COUNTRIES
from .models import Issue


def _norm_key(text: str) -> str:
    return " ".join(str(text).replace("\xa0", " ").split()).upper()


class CountryIndex:
    """alpha3/中文名/英文名/alpha2/显式别名 → 三位码。别名冲突即报错。"""

    def __init__(self, reference_countries: Optional[list] = None):
        self._alias_to_alpha3: dict = {}
        self._alpha3_to_name: dict = {}
        self.conflicts: list = []
        for alpha2, alpha3, zh, en in BUILTIN_COUNTRIES:
            self._alpha3_to_name.setdefault(alpha3, zh)
            for key in (alpha2, alpha3, zh, en):
                self._bind(key, alpha3, builtin=True)
        for entry in reference_countries or []:
            alpha3 = str(entry.get("alpha3", "")).strip().upper()
            if not alpha3:
                continue
            name = str(entry.get("name", "")).strip()
            if name:
                self._alpha3_to_name.setdefault(alpha3, name)
                self._bind(name, alpha3, builtin=False)
            self._bind(alpha3, alpha3, builtin=False)
            for alias in entry.get("aliases", []) or []:
                self._bind(str(alias), alpha3, builtin=False)

    def _bind(self, key: str, alpha3: str, builtin: bool) -> None:
        k = _norm_key(key)
        if not k:
            return
        existing = self._alias_to_alpha3.get(k)
        if existing is not None and existing != alpha3:
            self.conflicts.append(
                f"国家别名冲突：{key!r} 已映射 {existing}，拒绝再映射 {alpha3}"
            )
            return
        self._alias_to_alpha3[k] = alpha3

    def resolve(self, text) -> Optional[str]:
        if text is None:
            return None
        return self._alias_to_alpha3.get(_norm_key(str(text)))

    def name_of(self, alpha3: str) -> Optional[str]:
        return self._alpha3_to_name.get(str(alpha3).upper())

    def known(self, alpha3: str) -> bool:
        return str(alpha3).upper() in self._alpha3_to_name


class MachineIndex:
    """标准机型库：精确匹配 + 显式别名 + 目的国 scope 变体。

    references.machines 为 import_machine_reference 的快照；每条可选
    destination_scope（alpha3 或 "default"，缺省按 "default" 处理）。
    同一机型可有多条不同 scope 的条目（如 915F：default=110KW、BRA=123KW，
    用户已确认的规则：目的国 BRA 用 A=巴西 记录，其余国家用 A 空记录）。
    匹配时按 request.destination.alpha3 选 scope，未命中回退 default。
    """

    def __init__(self, machines: Optional[list] = None, aliases: Optional[dict] = None):
        self._models: dict = {}   # model_norm -> {scope -> entry}
        self.issues: list = []
        self.deferred_unconfirmed: dict = {}  # model_norm -> entry（特殊规则未确认，暂停匹配）
        for entry in machines or []:
            model = str(entry.get("model", "")).strip()
            if not model:
                continue
            scope = str(entry.get("destination_scope") or "default").strip().upper() \
                or "DEFAULT"
            scopes = self._models.setdefault(_norm_key(model), {})
            if scope in scopes and scopes[scope] != entry:
                self.issues.append(
                    f"机型资料重复：{model!r} scope={scope} 存在不一致条目")
                continue
            scopes[scope] = entry
        self._aliases = {}
        for alias, target in (aliases or {}).items():
            k = _norm_key(alias)
            if k in self._aliases and self._aliases[k] != target:
                self.issues.append(f"机型别名冲突：{alias!r}")
                continue
            self._aliases[k] = target

    def _resolve(self, model, destination_alpha3):
        key = _norm_key(model)
        if key not in self._models and key in self._aliases:
            key = _norm_key(self._aliases[key])
        scopes = self._models.get(key)
        if not scopes:
            return None
        scope = str(destination_alpha3 or "").strip().upper()
        entry = scopes.get(scope)
        if entry is None or scope == "":
            entry = scopes.get("DEFAULT", entry)
        if entry is not None and scope not in ("", "DEFAULT") \
                and not entry.get("special_rule_confirmed", True):
            # 特殊配置规则未获用户确认：暂停匹配，由转换层登记追问
            self.deferred_unconfirmed[key] = entry
            return None
        return entry

    def match(self, model, destination_alpha3: Optional[str] = None) -> Optional[dict]:
        """精确匹配标准机型（含显式别名）；按目的国 scope 选变体，未命中回退 default。"""
        if model is None:
            return None
        return self._resolve(model, destination_alpha3)

    def alias_target(self, model, destination_alpha3: Optional[str] = None) -> Optional[str]:
        """返回标准机型名：别名给出指向，直配给出条目机型名（用于输出统一命名）。"""
        if model is None:
            return None
        key = _norm_key(model)
        if key in self._models:
            entry = self._resolve(model, destination_alpha3)
            return entry.get("model") if entry else None
        target = self._aliases.get(key)
        return target
