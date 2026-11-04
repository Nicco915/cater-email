"""Versioned country and machine reference data.

Matching is exact (normalized whitespace/case only). Unknown values are
returned as missing; this module never guesses a country or machine model.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Iterable

from .db import Database
from .errors import ValidationError
from .logging import JsonlLogger
from .util import normalize_key, utc_now

# Common destinations. This is an initial exact-match table; additions are
# explicit configuration changes recorded in config_versions.
BUILTIN_COUNTRIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("FRA", "法国", "France", ("French Republic",)),
    ("DEU", "德国", "Germany", ("Deutschland",)),
    ("ITA", "意大利", "Italy", ()),
    ("ESP", "西班牙", "Spain", ()),
    ("NLD", "荷兰", "Netherlands", ("Holland",)),
    ("BEL", "比利时", "Belgium", ()),
    ("POL", "波兰", "Poland", ()),
    ("GBR", "英国", "United Kingdom", ("UK", "Great Britain", "England")),
    ("USA", "美国", "United States", ("US", "USA", "United States of America")),
    ("CAN", "加拿大", "Canada", ()),
    ("MEX", "墨西哥", "Mexico", ()),
    ("BRA", "巴西", "Brazil", ()),
    ("ARG", "阿根廷", "Argentina", ()),
    ("CHL", "智利", "Chile", ()),
    ("PER", "秘鲁", "Peru", ()),
    ("COL", "哥伦比亚", "Colombia", ()),
    ("AUS", "澳大利亚", "Australia", ()),
    ("NZL", "新西兰", "New Zealand", ()),
    ("JPN", "日本", "Japan", ()),
    ("KOR", "韩国", "South Korea", ("Korea", "Republic of Korea")),
    ("IDN", "印度尼西亚", "Indonesia", ("印尼",)),
    ("MYS", "马来西亚", "Malaysia", ()),
    ("THA", "泰国", "Thailand", ()),
    ("VNM", "越南", "Vietnam", ("Viet Nam",)),
    ("PHL", "菲律宾", "Philippines", ()),
    ("SGP", "新加坡", "Singapore", ()),
    ("IND", "印度", "India", ()),
    ("ARE", "阿联酋", "United Arab Emirates", ("UAE",)),
    ("SAU", "沙特阿拉伯", "Saudi Arabia", ("沙特",)),
    ("TUR", "土耳其", "Turkey", ("Türkiye",)),
    ("ZAF", "南非", "South Africa", ()),
    ("EGY", "埃及", "Egypt", ()),
    ("RUS", "俄罗斯", "Russia", ("Russian Federation",)),
    ("UKR", "乌克兰", "Ukraine", ()),
    ("SWE", "瑞典", "Sweden", ()),
    ("NOR", "挪威", "Norway", ()),
    ("DNK", "丹麦", "Denmark", ()),
    ("FIN", "芬兰", "Finland", ()),
    ("CHE", "瑞士", "Switzerland", ()),
    ("AUT", "奥地利", "Austria", ()),
    ("PRT", "葡萄牙", "Portugal", ()),
    ("GRC", "希腊", "Greece", ()),
    ("CZE", "捷克", "Czech Republic", ("Czechia",)),
    ("HUN", "匈牙利", "Hungary", ()),
    ("ROU", "罗马尼亚", "Romania", ()),
    ("MAR", "摩洛哥", "Morocco", ()),
    ("PAK", "巴基斯坦", "Pakistan", ()),
    ("BGD", "孟加拉国", "Bangladesh", ()),
    ("LKA", "斯里兰卡", "Sri Lanka", ()),
    ("MMR", "缅甸", "Myanmar", ("Burma",)),
    ("KHM", "柬埔寨", "Cambodia", ()),
    ("ISR", "以色列", "Israel", ()),
    ("QAT", "卡塔尔", "Qatar", ()),
    ("KWT", "科威特", "Kuwait", ()),
    ("OMN", "阿曼", "Oman", ()),
    ("NGA", "尼日利亚", "Nigeria", ()),
    ("KEN", "肯尼亚", "Kenya", ()),
    ("ETH", "埃塞俄比亚", "Ethiopia", ()),
    ("GHA", "加纳", "Ghana", ()),
    ("TZA", "坦桑尼亚", "Tanzania", ()),
)


class ReferenceService:
    def __init__(self, db: Database, logger: JsonlLogger) -> None:
        self.db = db
        self.logger = logger

    # ------------------------------------------------------------------
    # countries
    # ------------------------------------------------------------------
    def ensure_seeded(self) -> None:
        now = utc_now()
        with self.db.transaction() as conn:
            for alpha3, name_zh, name_en, aliases in BUILTIN_COUNTRIES:
                exists = conn.execute(
                    "SELECT 1 FROM countries WHERE alpha3 = ?", (alpha3,)
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO countries(alpha3, name_zh, name_en, source, created_at)
                    VALUES (?, ?, ?, 'builtin', ?)
                    """,
                    (alpha3, name_zh, name_en, now),
                )
                for alias in (name_zh, name_en, *aliases):
                    norm = normalize_key(alias)
                    if not norm:
                        continue
                    existing = conn.execute(
                        "SELECT alpha3 FROM country_aliases WHERE alias_norm = ?", (norm,)
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO country_aliases(alias_norm, alpha3, alias_raw, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (norm, alpha3, alias, now),
                        )

    def resolve(self, value: str | None) -> dict[str, Any] | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        row = self.db.fetchone(
            "SELECT * FROM countries WHERE upper(alpha3) = upper(?)", (text,)
        )
        if row is None:
            row = self.db.fetchone(
                """
                SELECT c.* FROM countries c
                  JOIN country_aliases a ON a.alpha3 = c.alpha3
                 WHERE a.alias_norm = ?
                """,
                (normalize_key(text),),
            )
        if row is None:
            return None
        aliases = [
            str(alias["alias_raw"])
            for alias in self.db.fetchall(
                "SELECT alias_raw FROM country_aliases WHERE alpha3 = ? ORDER BY alias_raw",
                (row["alpha3"],),
            )
        ]
        return {
            "alpha3": row["alpha3"],
            "name_zh": row["name_zh"],
            "name_en": row["name_en"],
            "name": row["name_zh"],
            "aliases": aliases,
        }

    def list_countries(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM countries ORDER BY name_zh, alpha3"
            )
        ]

    def country_aliases(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT a.alias_norm, a.alias_raw, a.alpha3, c.name_zh, c.name_en
                  FROM country_aliases a JOIN countries c ON c.alpha3 = a.alpha3
                 ORDER BY c.name_zh, a.alias_raw
                """
            )
        ]

    def validate_alias(self, alias: str, alpha3: str) -> tuple[str, str]:
        """Check an alias without writing. Returns ``(alias_norm, alpha3)``.

        Supplement intake validates all directives before applying any of them,
        so a later invalid line cannot leave a half-applied configuration.
        """

        norm = normalize_key(alias)
        if not norm:
            raise ValidationError("Country alias must not be empty")
        alpha3 = (alpha3 or "").strip().upper()
        country = self.db.fetchone(
            "SELECT * FROM countries WHERE alpha3 = ?", (alpha3,)
        )
        if country is None:
            raise ValidationError(f"Unknown ISO alpha-3 country code: {alpha3}")
        existing = self.db.fetchone(
            "SELECT * FROM country_aliases WHERE alias_norm = ?", (norm,)
        )
        if existing is not None and existing["alpha3"] != alpha3:
            raise ValidationError(
                f"Alias '{alias}' already maps to {existing['alpha3']}; "
                "conflicting mappings are not overwritten"
            )
        return norm, alpha3

    def add_alias(
        self,
        alias: str,
        alpha3: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm, alpha3 = self.validate_alias(alias, alpha3)
        existing = self.db.fetchone(
            "SELECT * FROM country_aliases WHERE alias_norm = ?", (norm,)
        )
        if existing is not None:
            return dict(existing)
        now = utc_now()
        previous = None
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO country_aliases(alias_norm, alpha3, alias_raw, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (norm, alpha3, alias.strip(), now),
            )
            conn.execute(
                """
                INSERT INTO config_versions(key, value_json, previous_json, actor, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"country_alias:{norm}",
                    json.dumps({"alias": alias.strip(), "alpha3": alpha3}, ensure_ascii=False),
                    previous,
                    actor,
                    reason,
                    now,
                ),
            )
        self.logger.info(
            "country_alias_added", actor=actor, alpha3=alpha3, alias=alias.strip()
        )
        return {"alias_norm": norm, "alpha3": alpha3, "alias_raw": alias.strip()}

    # ------------------------------------------------------------------
    # machines
    # ------------------------------------------------------------------
    def store_machines(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        source: str,
        actor: str = "system",
    ) -> dict[str, int]:
        """Import normalized machine rows.

        Re-importing identical data is safe. The same model may exist for
        different destination scopes (for example a Brazil-specific 915F next
        to the default variant). A duplicate model *within the same scope* with
        different declaration data is rejected instead of last-row-wins.
        """

        prepared: dict[tuple[str, str], dict[str, Any]] = {}
        declaration_keys = (
            "english_name",
            "hs_code",
            "product_name",
            "declaration",
            "notes",
        )
        metadata_keys = ("special", "verification")
        comparison_keys = declaration_keys + metadata_keys + (
            "special_rule_confirmed",
        )
        for raw in rows:
            model = str(raw.get("model") or "").strip()
            if not model:
                raise ValidationError("Machine reference row has no model")
            norm = normalize_key(model)
            scope = _scope(raw.get("destination_scope"))
            item = {
                "model": model,
                "destination_scope": scope,
                "english_name": _str_or_none(raw.get("english_name")),
                "hs_code": _str_or_none(raw.get("hs_code")),
                "product_name": _str_or_none(raw.get("product_name")),
                "declaration": _str_or_none(raw.get("declaration")),
                "notes": _str_or_none(raw.get("notes")),
                "special": _str_or_none(raw.get("special")),
                "verification": _str_or_none(raw.get("verification")),
                "special_rule_confirmed": _bool_value(
                    raw.get("special_rule_confirmed"), default=True
                ),
                "source_row": _int_or_none(raw.get("source_row")),
                "source": source,
                "aliases": [
                    str(alias).strip()
                    for alias in (raw.get("aliases") or [])
                    if str(alias).strip()
                ],
            }
            key = (norm, scope)
            if key in prepared:
                existing = prepared[key]
                comparable = {field: existing.get(field) for field in comparison_keys}
                incoming = {field: item.get(field) for field in comparison_keys}
                if comparable != incoming:
                    rows = [
                        str(value)
                        for value in (existing.get("source_row"), item.get("source_row"))
                        if value is not None
                    ]
                    row_detail = f" (source rows {', '.join(rows)})" if rows else ""
                    raise ValidationError(
                        f"Duplicate machine model '{model}' in scope "
                        f"'{scope or 'default'}' has conflicting declaration data"
                        f"{row_detail}"
                    )
                existing["aliases"] = sorted(set(existing["aliases"]) | set(item["aliases"]))
                if existing.get("source_row") is None:
                    existing["source_row"] = item.get("source_row")
            else:
                prepared[key] = item

        inserted = 0
        updated = 0
        now = utc_now()
        with self.db.transaction() as conn:
            for (norm, scope), item in prepared.items():
                existing = conn.execute(
                    """
                    SELECT * FROM machine_materials
                     WHERE model_norm = ? AND destination_scope = ?
                    """,
                    (norm, scope),
                ).fetchone()
                if existing is not None:
                    comparable_existing = {
                        field: existing[field] for field in declaration_keys
                    }
                    incoming = {field: item[field] for field in declaration_keys}
                    if comparable_existing != incoming:
                        row_detail = (
                            f" (incoming source row {item['source_row']})"
                            if item.get("source_row") is not None
                            else ""
                        )
                        raise ValidationError(
                            f"Machine model '{item['model']}' in scope "
                            f"'{scope or 'default'}' already exists with different data"
                            f"{row_detail}"
                        )
                    # Metadata is refreshed from the authoritative workbook
                    # without treating a metadata-only change as a declaration
                    # conflict. This also upgrades rows imported before
                    # special_rule_confirmed was introduced.
                    metadata_changes = {
                        field: item[field]
                        for field in metadata_keys
                        if existing[field] != item[field]
                    }
                    if bool(existing["special_rule_confirmed"]) != item[
                        "special_rule_confirmed"
                    ]:
                        metadata_changes["special_rule_confirmed"] = int(
                            item["special_rule_confirmed"]
                        )
                    if existing["source_row"] != item["source_row"]:
                        metadata_changes["source_row"] = item["source_row"]
                    if metadata_changes:
                        metadata_changes["source"] = item["source"]
                        metadata_changes["updated_at"] = now
                        assignments = ", ".join(
                            f"{field} = ?" for field in metadata_changes
                        )
                        conn.execute(
                            f"UPDATE machine_materials SET {assignments} "
                            "WHERE model_norm = ? AND destination_scope = ?",
                            (
                                *metadata_changes.values(),
                                norm,
                                scope,
                            ),
                        )
                else:
                    conn.execute(
                        """
                        INSERT INTO machine_materials (
                            model_norm, destination_scope, model, english_name, hs_code,
                            product_name, declaration, notes, special, verification,
                            special_rule_confirmed, source_row, source, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            norm,
                            scope,
                            item["model"],
                            item["english_name"],
                            item["hs_code"],
                            item["product_name"],
                            item["declaration"],
                            item["notes"],
                            item["special"],
                            item["verification"],
                            int(item["special_rule_confirmed"]),
                            item["source_row"],
                            item["source"],
                            now,
                        ),
                    )
                    inserted += 1
                for alias in item["aliases"]:
                    alias_norm = normalize_key(alias)
                    if not alias_norm:
                        continue
                    existing_alias = conn.execute(
                        """
                        SELECT * FROM machine_aliases
                         WHERE alias_norm = ? AND destination_scope = ?
                        """,
                        (alias_norm, scope),
                    ).fetchone()
                    if existing_alias is not None and existing_alias["model_norm"] != norm:
                        raise ValidationError(
                            f"Machine alias '{alias}' in scope '{scope or 'default'}' "
                            "already belongs to another model"
                        )
                    if existing_alias is None:
                        conn.execute(
                            """
                            INSERT INTO machine_aliases(
                                alias_norm, destination_scope, model_norm, alias_raw, created_at
                            )
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (alias_norm, scope, norm, alias, now),
                        )
                if existing is not None:
                    updated += 1
            conn.execute(
                """
                INSERT INTO config_versions(key, value_json, previous_json, actor, reason, created_at)
                VALUES ('machine_reference', ?, NULL, ?, 'machine reference import', ?)
                """,
                (
                    json.dumps(
                        {"source": source, "inserted": inserted, "updated": updated},
                        ensure_ascii=False,
                    ),
                    actor,
                    now,
                ),
            )
        self.logger.info(
            "machine_reference_imported",
            source=source,
            inserted=inserted,
            updated=updated,
        )
        return {"inserted": inserted, "updated": updated}

    def list_machines(self) -> list[dict[str, Any]]:
        machines = [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT * FROM machine_materials
                 ORDER BY model, destination_scope
                """
            )
        ]
        aliases = self.db.fetchall(
            "SELECT * FROM machine_aliases ORDER BY alias_raw, destination_scope"
        )
        by_model: dict[tuple[str, str], list[str]] = {}
        for row in aliases:
            key = (str(row["model_norm"]), str(row["destination_scope"]))
            by_model.setdefault(key, []).append(str(row["alias_raw"]))
        for machine in machines:
            key = (str(machine["model_norm"]), str(machine["destination_scope"]))
            machine["aliases"] = by_model.get(key, [])
            machine["special_rule_confirmed"] = bool(
                machine["special_rule_confirmed"]
            )
        return machines

    def machine_aliases(self) -> dict[str, str]:
        """Return unambiguous alias -> model mappings for the current core API.

        Scoped aliases are additionally exposed through
        :meth:`machine_aliases_scoped` so the core can resolve them using the
        destination. An alias that exists in more than one scope is omitted
        from this flat mapping to avoid guessing.
        """

        rows = self.db.fetchall(
            "SELECT alias_norm, model_norm, destination_scope FROM machine_aliases"
        )
        grouped: dict[str, set[str]] = {}
        for row in rows:
            grouped.setdefault(str(row["alias_norm"]), set()).add(str(row["model_norm"]))
        return {
            alias: next(iter(targets))
            for alias, targets in grouped.items()
            if len(targets) == 1
        }

    def machine_aliases_scoped(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.fetchall(
                """
                SELECT a.alias_norm, a.alias_raw, a.model_norm, m.model,
                       a.destination_scope
                  FROM machine_aliases a
                  JOIN machine_materials m
                    ON m.model_norm = a.model_norm
                   AND m.destination_scope = a.destination_scope
                 ORDER BY a.alias_norm, a.destination_scope
                """
            )
        ]

    # ------------------------------------------------------------------
    # snapshots for the core process_job request
    # ------------------------------------------------------------------
    def core_references(self) -> dict[str, Any]:
        countries: list[dict[str, Any]] = []
        for row in self.db.fetchall("SELECT * FROM countries ORDER BY alpha3"):
            aliases = [
                str(alias["alias_raw"])
                for alias in self.db.fetchall(
                    "SELECT alias_raw FROM country_aliases WHERE alpha3 = ?", (row["alpha3"],)
                )
            ]
            countries.append(
                {
                    "alpha3": row["alpha3"],
                    "name": row["name_zh"],
                    "name_zh": row["name_zh"],
                    "name_en": row["name_en"],
                    "aliases": aliases,
                }
            )
        machines = [
            {
                "model": row["model"],
                "destination_scope": row["destination_scope"],
                "english_name": row["english_name"],
                "hs_code": row["hs_code"],
                "product_name": row["product_name"],
                "declaration": row["declaration"],
                "notes": row["notes"],
                "special": row["special"],
                "verification": row["verification"],
                "special_rule_confirmed": bool(row["special_rule_confirmed"]),
                "source_row": row["source_row"],
                "aliases": [
                    str(alias["alias_raw"])
                    for alias in self.db.fetchall(
                        """
                        SELECT alias_raw FROM machine_aliases
                         WHERE model_norm = ? AND destination_scope = ?
                         ORDER BY alias_raw
                        """,
                        (row["model_norm"], row["destination_scope"]),
                    )
                ],
            }
            for row in self.db.fetchall(
                "SELECT * FROM machine_materials ORDER BY model, destination_scope"
            )
        ]
        return {
            "countries": countries,
            "machines": machines,
            "machine_aliases": self.machine_aliases(),
            "machine_aliases_scoped": self.machine_aliases_scoped(),
        }

    def reference_version(self) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(id), 0) AS version FROM config_versions"
        )
        return int(row["version"]) if row else 0


def _bool_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scope(value: Any) -> str:
    """Normalize a core destination scope. Empty/default means all countries."""

    if value is None:
        return ""
    scope = str(value).strip().upper()
    return "" if scope in {"", "DEFAULT", "ALL", "*"} else scope


def import_using(
    importer: Callable[[str], list[dict[str, Any]]],
    path: str,
    references: ReferenceService,
    *,
    actor: str = "system",
) -> dict[str, int]:
    rows = importer(path)
    if not isinstance(rows, list):
        raise ValidationError("import_machine_reference must return a list")
    return references.store_machines(rows, source=path, actor=actor)