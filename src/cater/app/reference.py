"""Versioned country and machine reference data.

Matching is exact (normalized whitespace/case only). Unknown values are
returned as missing; this module never guesses a country or machine model.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from .db import Database
from .errors import ValidationError
from .logging import JsonlLogger
from .runtime_meta import get_meta
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


# Bump when the seed set changes so old databases get a one-time, safe backfill
# of newly supported countries/alpha2 aliases (never resurrecting user deletes).
SEED_VERSION = 2


def _seed_country_rows() -> list[dict[str, Any]]:
    """Build the complete supported-country seed.

    The core keeps a comprehensive ISO list (alpha2 + alpha3 + names). The app
    list adds operator-facing extra aliases. Merging both ensures the SQLite
    seed (which is authoritative for new tasks) contains every country/alpha2
    the conversion core can resolve — in particular CHN/CN and TGO, which the
    older app-only list lacked. If the core is not importable we fall back to
    the app list rather than failing startup.
    """

    try:  # Lazy import keeps app usable without the core at module import time.
        from ..core.countries import BUILTIN_COUNTRIES as CORE
    except Exception:  # pragma: no cover - core is present in this repo
        CORE = []
    app = {a3: (zh, en, tuple(extra)) for (a3, zh, en, extra) in BUILTIN_COUNTRIES}
    merged: dict[str, dict[str, Any]] = {}
    for alpha2, alpha3, zh, en in CORE:
        entry = merged.setdefault(
            alpha3,
            {"alpha3": alpha3, "name_zh": zh, "name_en": en, "aliases": set()},
        )
        entry["aliases"].update(item for item in (alpha2, alpha3, zh, en) if item)
    for alpha3, (zh, en, extra) in app.items():
        entry = merged.setdefault(
            alpha3, {"alpha3": alpha3, "name_zh": zh, "name_en": en, "aliases": set()}
        )
        # App names win for display; all names remain resolvable as aliases.
        entry["name_zh"] = zh
        entry["name_en"] = en
        entry["aliases"].update(item for item in (zh, en, *extra) if item)
    return list(merged.values())


class ReferenceService:
    def __init__(
        self, db: Database, logger: JsonlLogger, tasks_dir: Any | None = None
    ) -> None:
        self.db = db
        self.logger = logger
        # Used to prove a machine row is unreferenced by any historical task
        # snapshot before allowing a physical delete.
        self.tasks_dir = tasks_dir

    # ------------------------------------------------------------------
    # countries
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # tombstones (physically deleted references are never re-seeded)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_tombstoned(conn, kind: str, key: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM reference_tombstones WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
            is not None
        )

    def _add_tombstone(self, conn, kind: str, key: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO reference_tombstones(kind, key, created_at) VALUES (?, ?, ?)",
            (kind, key, utc_now()),
        )

    def ensure_seeded(self) -> None:
        """Seed / version-backfill the authoritative country table.

        The SQLite reference data is authoritative for new tasks: a country an
        operator deleted or disabled must never come back after a restart, and
        the core's built-in fallback must not bypass that decision.

        Seeding runs once. When ``SEED_VERSION`` increases (for example to add
        CHN/CN, TGO and other alpha2 aliases the older app-only list lacked),
        missing countries/aliases are **backfilled** on upgrade — but the
        backfill never updates an existing row/alias, never re-adds anything a
        user physically deleted (tracked in ``reference_tombstones``), and never
        re-enables a disabled country. Every seed/backfill is audited.
        """

        from .runtime_meta import get_meta, set_meta, set_meta_if_absent

        stored_version = int(get_meta(self.db, "reference_seed_version") or 0)
        already_seeded = get_meta(self.db, "reference_seeded") == "1"
        if already_seeded and stored_version >= SEED_VERSION:
            return
        first_seed = not already_seeded
        rows = _seed_country_rows()
        now = utc_now()
        inserted_countries = 0
        inserted_aliases = 0
        with self.db.transaction() as conn:
            for entry in rows:
                alpha3 = str(entry["alpha3"])
                if self._is_tombstoned(conn, "country", alpha3):
                    # User physically deleted this country; never resurrect it
                    # or any of its aliases.
                    continue
                country_exists = conn.execute(
                    "SELECT 1 FROM countries WHERE alpha3 = ?", (alpha3,)
                ).fetchone()
                if country_exists is None:
                    conn.execute(
                        """
                        INSERT INTO countries(alpha3, name_zh, name_en, source, enabled, created_at)
                        VALUES (?, ?, ?, 'builtin', 1, ?)
                        """,
                        (alpha3, entry["name_zh"], entry["name_en"], now),
                    )
                    inserted_countries += 1
                # Insert only missing aliases; never update existing ones and
                # skip aliases the user explicitly deleted.
                for alias in sorted(entry["aliases"]):
                    norm = normalize_key(alias)
                    if not norm or self._is_tombstoned(conn, "country_alias", norm):
                        continue
                    existing = conn.execute(
                        "SELECT 1 FROM country_aliases WHERE alias_norm = ?", (norm,)
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO country_aliases(alias_norm, alpha3, alias_raw, enabled, created_at)
                            VALUES (?, ?, ?, 1, ?)
                            """,
                            (norm, alpha3, alias, now),
                        )
                        inserted_aliases += 1
            conn.execute(
                """
                INSERT INTO config_versions(key, value_json, previous_json, actor, reason, created_at)
                VALUES ('reference_seed', ?, NULL, 'system', ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "seed_version": SEED_VERSION,
                            "first_seed": first_seed,
                            "inserted_countries": inserted_countries,
                            "inserted_aliases": inserted_aliases,
                            "authoritative": True,
                        },
                        ensure_ascii=False,
                    ),
                    "initial country seed" if first_seed else "country seed backfill",
                    now,
                ),
            )
        set_meta_if_absent(self.db, "reference_seeded", "1")
        set_meta(self.db, "reference_seed_version", str(SEED_VERSION))

    def resolve(self, value: str | None) -> dict[str, Any] | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        # Only enabled countries and enabled aliases are authoritative for new
        # tasks. A disabled country (and all of its aliases) is intentionally
        # unresolvable here so the core cannot match it.
        row = self.db.fetchone(
            "SELECT * FROM countries WHERE upper(alpha3) = upper(?) AND enabled = 1",
            (text,),
        )
        if row is None:
            row = self.db.fetchone(
                """
                SELECT c.* FROM countries c
                  JOIN country_aliases a ON a.alpha3 = c.alpha3
                 WHERE a.alias_norm = ? AND a.enabled = 1 AND c.enabled = 1
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

    def list_countries(
        self,
        *,
        enabled_only: bool = False,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if search:
            clauses.append("(alpha3 LIKE ? OR name_zh LIKE ? OR name_en LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        rows = self.db.fetchall(
            f"SELECT * FROM countries {where} ORDER BY name_zh, alpha3 LIMIT ? OFFSET ?",
            params,
        )
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def count_countries(self, *, enabled_only: bool = False, search: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if search:
            clauses.append("(alpha3 LIKE ? OR name_zh LIKE ? OR name_en LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return int(self.db.scalar(f"SELECT COUNT(*) FROM countries {where}", params) or 0)

    def count_machines(self, *, enabled_only: bool = False, search: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if search:
            clauses.append("(model LIKE ? OR english_name LIKE ? OR product_name LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return int(self.db.scalar(f"SELECT COUNT(*) FROM machine_materials {where}", params) or 0)

    def country_aliases(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        clauses = []
        if enabled_only:
            clauses.append("a.enabled = 1")
            clauses.append("c.enabled = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            dict(row)
            for row in self.db.fetchall(
                f"""
                SELECT a.alias_norm, a.alias_raw, a.alpha3, a.enabled,
                       c.name_zh, c.name_en
                  FROM country_aliases a JOIN countries c ON c.alpha3 = a.alpha3
                 {where}
                 ORDER BY c.name_zh, a.alias_raw
                """
            )
        ]

    def get_country(self, alpha3: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM countries WHERE alpha3 = ?", (alpha3.strip().upper(),)
        )
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item

    # ------------------------------------------------------------------
    # audited reference changes (before/after, actor, time, reason)
    # ------------------------------------------------------------------
    def _audit(
        self,
        conn,
        *,
        key: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        actor: str,
        reason: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO config_versions(key, value_json, previous_json, actor, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                json.dumps(after, ensure_ascii=False, default=str),
                json.dumps(before, ensure_ascii=False, default=str) if before else None,
                actor,
                reason,
                utc_now(),
            ),
        )

    @staticmethod
    def _normalize_alpha3(alpha3: str) -> str:
        code = (alpha3 or "").strip().upper()
        if not code or len(code) != 3 or not code.isalpha():
            raise ValidationError("国家三位码必须为三位大写字母（alpha3）")
        return code

    def create_country(
        self,
        alpha3: str,
        name_zh: str,
        name_en: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        code = self._normalize_alpha3(alpha3)
        zh = (name_zh or "").strip()
        en = (name_en or "").strip()
        if not zh:
            raise ValidationError("国家中文名不能为空")
        if self.get_country(code) is not None:
            raise ValidationError(f"国家 {code} 已存在；如需调整请编辑而非新建")
        now = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO countries(alpha3, name_zh, name_en, source, enabled, created_at)
                VALUES (?, ?, ?, 'manual', 1, ?)
                """,
                (code, zh, en or None, now),
            )
            self._audit(
                conn,
                key=f"country:{code}",
                before=None,
                after={"alpha3": code, "name_zh": zh, "name_en": en, "enabled": True},
                actor=actor,
                reason=reason,
            )
        self.logger.info("country_created", actor=actor, alpha3=code)
        return self.get_country(code)  # type: ignore[return-value]

    def update_country(
        self,
        alpha3: str,
        *,
        name_zh: str | None = None,
        name_en: str | None = None,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Edit a country's names. The alpha3 code is a stable primary key and
        is never changed here; migrating to a new code requires creating it and
        moving aliases explicitly."""

        code = self._normalize_alpha3(alpha3)
        current = self.get_country(code)
        if current is None:
            raise ValidationError(f"国家 {code} 不存在")
        zh = current["name_zh"] if name_zh is None else (name_zh or "").strip()
        en = current["name_en"] if name_en is None else (name_en or "").strip() or None
        if not zh:
            raise ValidationError("国家中文名不能为空")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE countries SET name_zh = ?, name_en = ? WHERE alpha3 = ?",
                (zh, en, code),
            )
            self._audit(
                conn,
                key=f"country:{code}",
                before=current,
                after={**current, "name_zh": zh, "name_en": en},
                actor=actor,
                reason=reason,
            )
        return self.get_country(code)  # type: ignore[return-value]

    def country_referenced(self, alpha3: str) -> dict[str, Any]:
        """Return what currently references a country, for delete protection."""

        code = alpha3.strip().upper()
        aliases = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM country_aliases WHERE alpha3 = ?", (code,)
        )
        machines = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM machine_materials WHERE destination_scope = ?",
            (code,),
        )
        tasks = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM tasks WHERE destination_alpha3 = ?", (code,)
        )
        return {
            "aliases": int(aliases["n"]) if aliases else 0,
            "machine_scopes": int(machines["n"]) if machines else 0,
            "tasks": int(tasks["n"]) if tasks else 0,
        }

    def _apply_country_enabled(
        self, conn, current: dict[str, Any], enabled: bool, *, actor: str, reason: str | None, now: str
    ) -> None:
        code = str(current["alpha3"])
        conn.execute(
            "UPDATE countries SET enabled = ?, disabled_at = ?, disabled_reason = ? WHERE alpha3 = ?",
            (1 if enabled else 0, None if enabled else now, reason if not enabled else None, code),
        )
        # Do NOT rewrite child alias enabled flags here. An alias has its own
        # enable/disable intent; disabling the parent only affects matching
        # (resolve/core require country.enabled AND alias.enabled), so
        # re-enabling the country must not resurrect aliases the operator
        # deliberately disabled individually.
        self._audit(
            conn,
            key=f"country:{code}:enabled",
            before=current,
            after={**current, "enabled": bool(enabled)},
            actor=actor,
            reason=reason,
        )

    def set_country_enabled(
        self,
        alpha3: str,
        enabled: bool,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        code = self._normalize_alpha3(alpha3)
        current = self.get_country(code)
        if current is None:
            raise ValidationError(f"国家 {code} 不存在")
        now = utc_now()
        with self.db.transaction() as conn:
            self._apply_country_enabled(
                conn, current, enabled, actor=actor, reason=reason, now=now
            )
        self.logger.info(
            "country_enabled_changed", actor=actor, alpha3=code, enabled=enabled
        )
        return self.get_country(code)  # type: ignore[return-value]

    def delete_country(
        self,
        alpha3: str,
        *,
        actor: str,
        reason: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Default behaviour is a soft disable (historical tasks stay
        traceable). A physical delete is only allowed when the country is
        provably unreferenced and ``force`` is set; otherwise the operator must
        first handle aliases and machine scopes."""

        code = self._normalize_alpha3(alpha3)
        current = self.get_country(code)
        if current is None:
            raise ValidationError(f"国家 {code} 不存在")
        refs = self.country_referenced(code)
        if force:
            if refs["aliases"] or refs["machine_scopes"] or refs["tasks"]:
                # Historical tasks referencing the country are proof it is in
                # use; physical delete would break traceability. Force a
                # conservative disable instead.
                raise ValidationError(
                    f"国家 {code} 仍被别名/机型 scope/历史任务引用，不能物理删除；请改用停用"
                )
            with self.db.transaction() as conn:
                # Tombstone the country and each of its aliases so a future
                # seed backfill cannot resurrect any of them.
                for alias_row in conn.execute(
                    "SELECT alias_norm FROM country_aliases WHERE alpha3 = ?", (code,)
                ).fetchall():
                    self._add_tombstone(conn, "country_alias", str(alias_row["alias_norm"]))
                self._add_tombstone(conn, "country", code)
                conn.execute("DELETE FROM country_aliases WHERE alpha3 = ?", (code,))
                conn.execute("DELETE FROM countries WHERE alpha3 = ?", (code,))
                self._audit(
                    conn,
                    key=f"country:{code}:delete",
                    before=current,
                    after=None,
                    actor=actor,
                    reason=reason,
                )
            self.logger.info("country_deleted", actor=actor, alpha3=code, force=True)
            return {"alpha3": code, "deleted": True, "physical": True}
        # Default: soft disable. Historical task snapshots and rows are untouched.
        self.set_country_enabled(code, False, actor=actor, reason=reason or "删除（停用）")
        return {"alpha3": code, "deleted": True, "physical": False}

    def delete_alias(
        self,
        alias_norm: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm = (alias_norm or "").strip()
        if not norm:
            raise ValidationError("别名标识不能为空")
        current = self.db.fetchone(
            "SELECT * FROM country_aliases WHERE alias_norm = ?", (norm,)
        )
        if current is None:
            raise ValidationError("别名不存在")
        with self.db.transaction() as conn:
            # Tombstone so a later seed backfill cannot re-add this alias.
            self._add_tombstone(conn, "country_alias", norm)
            conn.execute("DELETE FROM country_aliases WHERE alias_norm = ?", (norm,))
            self._audit(
                conn,
                key=f"country_alias:{norm}:delete",
                before=dict(current),
                after=None,
                actor=actor,
                reason=reason,
            )
        self.logger.info("country_alias_deleted", actor=actor, alias_norm=norm)
        return {"alias_norm": norm, "deleted": True}

    def set_alias_enabled(
        self,
        alias_norm: str,
        enabled: bool,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm = (alias_norm or "").strip()
        current = self.db.fetchone(
            "SELECT * FROM country_aliases WHERE alias_norm = ?", (norm,)
        )
        if current is None:
            raise ValidationError("别名不存在")
        with self.db.transaction() as conn:
            self._apply_country_alias_enabled(
                conn, dict(current), enabled, actor=actor, reason=reason
            )
        return {"alias_norm": norm, "enabled": bool(enabled)}

    def _apply_country_alias_enabled(
        self, conn, current: dict[str, Any], enabled: bool, *, actor: str, reason: str | None
    ) -> None:
        norm = str(current["alias_norm"])
        conn.execute(
            "UPDATE country_aliases SET enabled = ? WHERE alias_norm = ?",
            (1 if enabled else 0, norm),
        )
        self._audit(
            conn,
            key=f"country_alias:{norm}:enabled",
            before=current,
            after={**current, "enabled": bool(enabled)},
            actor=actor,
            reason=reason,
        )

    def update_country_alias(
        self,
        alias_norm: str,
        new_raw: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Edit a country alias's raw text (the normalized key is re-derived).

        The target country is unchanged; a collision with an alias of another
        country is refused rather than silently merged.
        """

        norm = (alias_norm or "").strip()
        current = self.db.fetchone(
            "SELECT * FROM country_aliases WHERE alias_norm = ?", (norm,)
        )
        if current is None:
            raise ValidationError("别名不存在")
        raw = (new_raw or "").strip()
        new_norm = normalize_key(raw)
        if not new_norm:
            raise ValidationError("别名不能为空")
        clash = self.db.fetchone(
            "SELECT alpha3 FROM country_aliases WHERE alias_norm = ?", (new_norm,)
        )
        if clash is not None and new_norm != norm and str(clash["alpha3"]) != str(
            current["alpha3"]
        ):
            raise ValidationError(
                f"别名 {raw!r} 已映射 {clash['alpha3']}，跨国家冲突不静默覆盖"
            )
        with self.db.transaction() as conn:
            if new_norm != norm:
                # The old normalized alias is being renamed away; tombstone it so
                # a later backfill cannot restore the original text alongside the
                # renamed one.
                self._add_tombstone(conn, "country_alias", norm)
            conn.execute(
                "DELETE FROM country_aliases WHERE alias_norm = ?", (norm,)
            )
            conn.execute(
                """
                INSERT INTO country_aliases(alias_norm, alpha3, alias_raw, enabled, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    new_norm,
                    current["alpha3"],
                    raw,
                    int(current["enabled"]),
                    current["created_at"],
                ),
            )
            self._audit(
                conn,
                key=f"country_alias:{norm}:edit",
                before=dict(current),
                after={"alias_norm": new_norm, "alias_raw": raw, "alpha3": current["alpha3"]},
                actor=actor,
                reason=reason,
            )
        return {"alias_norm": new_norm, "alias_raw": raw, "alpha3": current["alpha3"]}

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

    def list_machines(
        self,
        *,
        enabled_only: bool = False,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if search:
            clauses.append("(model LIKE ? OR english_name LIKE ? OR product_name LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        machines = [
            dict(row)
            for row in self.db.fetchall(
                f"""
                SELECT * FROM machine_materials
                 {where}
                 ORDER BY model, destination_scope
                 LIMIT ? OFFSET ?
                """,
                params,
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
            machine["enabled"] = bool(machine["enabled"])
        return machines

    def get_machine(self, model_norm: str, scope: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            """
            SELECT * FROM machine_materials
             WHERE model_norm = ? AND destination_scope = ?
            """,
            (normalize_key(model_norm), _scope(scope)),
        )
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["special_rule_confirmed"] = bool(item["special_rule_confirmed"])
        return item

    # ------------------------------------------------------------------
    # machine CRUD
    # ------------------------------------------------------------------
    DECLARATION_FIELDS = (
        "english_name",
        "hs_code",
        "product_name",
        "declaration",
        "notes",
        "special",
        "verification",
    )

    def create_machine(
        self,
        model: str,
        destination_scope: str = "",
        *,
        english_name: str | None = None,
        hs_code: str | None = None,
        product_name: str | None = None,
        declaration: str | None = None,
        notes: str | None = None,
        special: str | None = None,
        verification: str | None = None,
        special_rule_confirmed: bool = False,
        aliases: Iterable[str] = (),
        actor: str = "system",
        reason: str | None = None,
    ) -> dict[str, Any]:
        model = (model or "").strip()
        if not model:
            raise ValidationError("机型不能为空")
        scope = _scope(destination_scope)
        norm = normalize_key(model)
        if self.get_machine(norm, scope) is not None:
            raise ValidationError(f"机型 {model!r} 在 scope {scope or 'default'} 已存在")
        now = utc_now()
        payload = {
            "model": model,
            "destination_scope": scope,
            "english_name": _str_or_none(english_name),
            "hs_code": _str_or_none(hs_code),
            "product_name": _str_or_none(product_name),
            "declaration": _str_or_none(declaration),
            "notes": _str_or_none(notes),
            "special": _str_or_none(special),
            "verification": _str_or_none(verification),
            "special_rule_confirmed": bool(special_rule_confirmed),
            "enabled": True,
        }
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO machine_materials (
                    model_norm, destination_scope, model, english_name, hs_code,
                    product_name, declaration, notes, special, verification,
                    special_rule_confirmed, source, enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', 1, ?)
                """,
                (
                    norm,
                    scope,
                    model,
                    payload["english_name"],
                    payload["hs_code"],
                    payload["product_name"],
                    payload["declaration"],
                    payload["notes"],
                    payload["special"],
                    payload["verification"],
                    int(payload["special_rule_confirmed"]),
                    now,
                ),
            )
            for alias in aliases:
                self._insert_machine_alias(conn, norm, scope, str(alias), now)
            self._audit(
                conn,
                key=f"machine:{norm}:{scope}",
                before=None,
                after=payload,
                actor=actor,
                reason=reason,
            )
        self.logger.info("machine_created", actor=actor, model=model, scope=scope)
        return self.get_machine(norm, scope)  # type: ignore[return-value]

    def update_machine(
        self,
        model_norm: str,
        destination_scope: str,
        *,
        english_name: str | None = None,
        hs_code: str | None = None,
        product_name: str | None = None,
        declaration: str | None = None,
        notes: str | None = None,
        special: str | None = None,
        verification: str | None = None,
        special_rule_confirmed: bool | None = None,
        actor: str = "system",
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm = normalize_key(model_norm)
        scope = _scope(destination_scope)
        current = self.get_machine(norm, scope)
        if current is None:
            raise ValidationError("机型不存在")
        after = dict(current)
        for field, value in {
            "english_name": english_name,
            "hs_code": hs_code,
            "product_name": product_name,
            "declaration": declaration,
            "notes": notes,
            "special": special,
            "verification": verification,
        }.items():
            if value is not None:
                after[field] = _str_or_none(value)
        # special_rule_confirmed is never promoted implicitly: only an explicit
        # True/False argument changes it, so an unconfirmed special rule (for
        # example SEM919BRZ) cannot be confirmed by editing another field.
        if special_rule_confirmed is not None:
            after["special_rule_confirmed"] = bool(special_rule_confirmed)
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE machine_materials
                   SET english_name = ?, hs_code = ?, product_name = ?,
                       declaration = ?, notes = ?, special = ?, verification = ?,
                       special_rule_confirmed = ?, updated_at = ?
                 WHERE model_norm = ? AND destination_scope = ?
                """,
                (
                    after["english_name"],
                    after["hs_code"],
                    after["product_name"],
                    after["declaration"],
                    after["notes"],
                    after["special"],
                    after["verification"],
                    int(after["special_rule_confirmed"]),
                    utc_now(),
                    norm,
                    scope,
                ),
            )
            self._audit(
                conn,
                key=f"machine:{norm}:{scope}",
                before=current,
                after={k: after[k] for k in after if k != "aliases"},
                actor=actor,
                reason=reason,
            )
        return self.get_machine(norm, scope)  # type: ignore[return-value]

    def machine_referenced(self, model_norm: str, scope: str) -> dict[str, Any]:
        norm = normalize_key(model_norm)
        sc = _scope(scope)
        aliases = self.db.fetchone(
            """
            SELECT COUNT(*) AS n FROM machine_aliases
             WHERE model_norm = ? AND destination_scope = ?
            """,
            (norm, sc),
        )
        snapshots = self._snapshot_machine_references(norm, sc)
        return {
            "aliases": int(aliases["n"]) if aliases else 0,
            "snapshots": snapshots,
            # When we cannot inspect snapshots (no tasks dir) we cannot prove the
            # row is unreferenced, so it is treated as referenced.
            "provable": self.tasks_dir is not None,
        }

    def _snapshot_machine_references(self, norm: str, scope: str) -> int:
        """Count historical task reference snapshots that include the machine.

        Machine rows are embedded in each task version's ``references.json``.
        Physical deletion must not proceed when an old snapshot still uses the
        row, so we scan the stored snapshots. When no tasks dir is configured we
        cannot prove absence and return 1 (treated as referenced).
        """

        if self.tasks_dir is None:
            return 1
        root = Path(self.tasks_dir)
        if not root.is_dir():
            return 0
        found = 0
        for references_path in root.glob("*/v*/references.json"):
            try:
                data = json.loads(references_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Unreadable snapshot: treat as a possible reference.
                return max(found, 1)
            for entry in data.get("machines", []) or []:
                if not isinstance(entry, dict):
                    continue
                entry_scope = _scope(entry.get("destination_scope"))
                if (
                    normalize_key(str(entry.get("model") or "")) == norm
                    and entry_scope == scope
                ):
                    found += 1
                    break
        return found

    def set_machine_enabled(
        self,
        model_norm: str,
        destination_scope: str,
        enabled: bool,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm = normalize_key(model_norm)
        scope = _scope(destination_scope)
        current = self.get_machine(norm, scope)
        if current is None:
            raise ValidationError("机型不存在")
        now = utc_now()
        with self.db.transaction() as conn:
            self._apply_machine_enabled(
                conn, current, enabled, actor=actor, reason=reason, now=now
            )
        self.logger.info(
            "machine_enabled_changed", actor=actor, model=norm, scope=scope, enabled=enabled
        )
        return self.get_machine(norm, scope)  # type: ignore[return-value]

    def _apply_machine_enabled(
        self, conn, current: dict[str, Any], enabled: bool, *, actor: str, reason: str | None, now: str
    ) -> None:
        norm = str(current["model_norm"])
        scope = str(current["destination_scope"])
        conn.execute(
            """
            UPDATE machine_materials
               SET enabled = ?, disabled_at = ?, disabled_reason = ?, updated_at = ?
             WHERE model_norm = ? AND destination_scope = ?
            """,
            (
                1 if enabled else 0,
                None if enabled else now,
                reason if not enabled else None,
                now,
                norm,
                scope,
            ),
        )
        # Do NOT rewrite alias enabled flags: a disabled scope is blocked by
        # MachineIndex (which knows the scope exists but is disabled, so it does
        # not fall back to default), and alias intent is preserved. Re-enabling
        # the scope must not resurrect individually-disabled aliases.
        self._audit(
            conn,
            key=f"machine:{norm}:{scope}:enabled",
            before=current,
            after={**current, "enabled": bool(enabled)},
            actor=actor,
            reason=reason,
        )

    def delete_machine(
        self,
        model_norm: str,
        destination_scope: str,
        *,
        actor: str,
        reason: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        norm = normalize_key(model_norm)
        scope = _scope(destination_scope)
        current = self.get_machine(norm, scope)
        if current is None:
            raise ValidationError("机型不存在")
        refs = self.machine_referenced(norm, scope)
        if force:
            if not refs["provable"]:
                raise ValidationError(
                    "无法检查历史任务快照，不能证明该机型未被引用；请改用停用"
                )
            if refs["aliases"] or refs["snapshots"]:
                raise ValidationError(
                    f"机型 {current['model']} scope {scope or 'default'} 仍有别名或历史任务快照引用，"
                    "不能物理删除；请改用停用"
                )
            with self.db.transaction() as conn:
                conn.execute(
                    "DELETE FROM machine_aliases WHERE model_norm = ? AND destination_scope = ?",
                    (norm, scope),
                )
                conn.execute(
                    "DELETE FROM machine_materials WHERE model_norm = ? AND destination_scope = ?",
                    (norm, scope),
                )
                self._audit(
                    conn,
                    key=f"machine:{norm}:{scope}:delete",
                    before=current,
                    after=None,
                    actor=actor,
                    reason=reason,
                )
            self.logger.info("machine_deleted", actor=actor, model=norm, scope=scope, force=True)
            return {"model_norm": norm, "destination_scope": scope, "deleted": True, "physical": True}
        return self.set_machine_enabled(
            norm, scope, False, actor=actor, reason=reason or "删除（停用）"
        )

    def _insert_machine_alias(
        self, conn, norm: str, scope: str, alias: str, now: str
    ) -> None:
        alias_norm = normalize_key(alias)
        if not alias_norm:
            return
        existing = conn.execute(
            "SELECT * FROM machine_aliases WHERE alias_norm = ? AND destination_scope = ?",
            (alias_norm, scope),
        ).fetchone()
        if existing is not None and str(existing["model_norm"]) != norm:
            raise ValidationError(
                f"机型别名 {alias!r} 在 scope {scope or 'default'} 已属于其他机型"
            )
        if existing is None:
            conn.execute(
                """
                INSERT INTO machine_aliases(
                    alias_norm, destination_scope, model_norm, alias_raw, enabled, created_at
                ) VALUES (?, ?, ?, ?, 1, ?)
                """,
                (alias_norm, scope, norm, alias, now),
            )

    def add_machine_alias(
        self,
        model: str,
        alias: str,
        destination_scope: str = "",
        *,
        actor: str = "system",
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm = normalize_key(model)
        scope = _scope(destination_scope)
        if self.get_machine(norm, scope) is None:
            raise ValidationError("目标机型不存在")
        alias_norm = normalize_key(alias)
        if not alias_norm:
            raise ValidationError("机型别名不能为空")
        now = utc_now()
        with self.db.transaction() as conn:
            self._insert_machine_alias(conn, norm, scope, alias.strip(), now)
            self._audit(
                conn,
                key=f"machine_alias:{alias_norm}:{scope}",
                before=None,
                after={"alias": alias.strip(), "model_norm": norm, "scope": scope},
                actor=actor,
                reason=reason,
            )
        return {"alias_norm": alias_norm, "model_norm": norm, "destination_scope": scope}

    def delete_machine_alias(
        self,
        alias_norm: str,
        destination_scope: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm_key = (alias_norm or "").strip()
        scope = _scope(destination_scope)
        current = self.db.fetchone(
            "SELECT * FROM machine_aliases WHERE alias_norm = ? AND destination_scope = ?",
            (norm_key, scope),
        )
        if current is None:
            raise ValidationError("机型别名不存在")
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM machine_aliases WHERE alias_norm = ? AND destination_scope = ?",
                (norm_key, scope),
            )
            self._audit(
                conn,
                key=f"machine_alias:{norm_key}:{scope}:delete",
                before=dict(current),
                after=None,
                actor=actor,
                reason=reason,
            )
        return {"alias_norm": norm_key, "destination_scope": scope, "deleted": True}

    def update_machine_alias(
        self,
        alias_norm: str,
        destination_scope: str,
        new_raw: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm = (alias_norm or "").strip()
        scope = _scope(destination_scope)
        current = self.db.fetchone(
            "SELECT * FROM machine_aliases WHERE alias_norm = ? AND destination_scope = ?",
            (norm, scope),
        )
        if current is None:
            raise ValidationError("机型别名不存在")
        raw = (new_raw or "").strip()
        new_norm = normalize_key(raw)
        if not new_norm:
            raise ValidationError("机型别名不能为空")
        clash = self.db.fetchone(
            "SELECT model_norm FROM machine_aliases WHERE alias_norm = ? AND destination_scope = ?",
            (new_norm, scope),
        )
        if clash is not None and new_norm != norm and str(clash["model_norm"]) != str(
            current["model_norm"]
        ):
            raise ValidationError(
                f"机型别名 {raw!r} 在 scope {scope or 'default'} 已属于其他机型"
            )
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM machine_aliases WHERE alias_norm = ? AND destination_scope = ?",
                (norm, scope),
            )
            conn.execute(
                """
                INSERT INTO machine_aliases(
                    alias_norm, destination_scope, model_norm, alias_raw, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_norm,
                    scope,
                    current["model_norm"],
                    raw,
                    int(current["enabled"]),
                    current["created_at"],
                ),
            )
            self._audit(
                conn,
                key=f"machine_alias:{norm}:{scope}:edit",
                before=dict(current),
                after={"alias_norm": new_norm, "alias_raw": raw, "scope": scope},
                actor=actor,
                reason=reason,
            )
        return {"alias_norm": new_norm, "destination_scope": scope, "alias_raw": raw}

    # ------------------------------------------------------------------
    # batch enable/disable (validate-all-then-commit)
    # ------------------------------------------------------------------
    def set_machine_alias_enabled(
        self,
        alias_norm: str,
        destination_scope: str,
        enabled: bool,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        norm = (alias_norm or "").strip()
        scope = _scope(destination_scope)
        current = self.db.fetchone(
            "SELECT * FROM machine_aliases WHERE alias_norm = ? AND destination_scope = ?",
            (norm, scope),
        )
        if current is None:
            raise ValidationError("机型别名不存在")
        with self.db.transaction() as conn:
            self._apply_machine_alias_enabled(
                conn, dict(current), enabled, actor=actor, reason=reason
            )
        return {"alias_norm": norm, "destination_scope": scope, "enabled": bool(enabled)}

    def _apply_machine_alias_enabled(
        self, conn, current: dict[str, Any], enabled: bool, *, actor: str, reason: str | None
    ) -> None:
        norm = str(current["alias_norm"])
        scope = str(current["destination_scope"])
        conn.execute(
            "UPDATE machine_aliases SET enabled = ? WHERE alias_norm = ? AND destination_scope = ?",
            (1 if enabled else 0, norm, scope),
        )
        self._audit(
            conn,
            key=f"machine_alias:{norm}:{scope}:enabled",
            before=current,
            after={**current, "enabled": bool(enabled)},
            actor=actor,
            reason=reason,
        )

    def batch_set_enabled(
        self,
        resource: str,
        keys: list[dict[str, Any]],
        enabled: bool,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Enable/disable many references atomically.

        Supports all four reference types. Re-reads every target inside ONE
        BEGIN IMMEDIATE transaction; a missing item aborts the whole batch with
        no partial apply, and a write failure rolls every item back.
        """

        if resource not in {"country", "machine", "country_alias", "machine_alias"}:
            raise ValidationError("resource 必须为国家/机型或其别名")
        if not keys:
            raise ValidationError("未选择任何记录")
        now = utc_now()
        applied = 0
        with self.db.transaction() as conn:
            resolved: list[tuple[str, Any]] = []
            for key in keys:
                if resource == "country":
                    code = self._normalize_alpha3(str(key.get("alpha3", "")))
                    row = conn.execute(
                        "SELECT * FROM countries WHERE alpha3 = ?", (code,)
                    ).fetchone()
                    if row is None:
                        raise ValidationError(f"国家 {code} 不存在，整批已撤销")
                    resolved.append(("country", dict(row)))
                elif resource == "machine":
                    model = str(key.get("model", "")).strip()
                    if not model:
                        raise ValidationError("机型不能为空，整批已撤销")
                    scope = _scope(key.get("destination_scope"))
                    norm = normalize_key(model)
                    row = conn.execute(
                        "SELECT * FROM machine_materials WHERE model_norm = ? AND destination_scope = ?",
                        (norm, scope),
                    ).fetchone()
                    if row is None:
                        raise ValidationError(
                            f"机型 {model!r} scope {scope or 'default'} 不存在，整批已撤销"
                        )
                    resolved.append(("machine", dict(row)))
                elif resource == "country_alias":
                    norm = str(key.get("alias_norm", "")).strip()
                    row = conn.execute(
                        "SELECT * FROM country_aliases WHERE alias_norm = ?", (norm,)
                    ).fetchone()
                    if row is None:
                        raise ValidationError(f"国家别名 {norm!r} 不存在，整批已撤销")
                    resolved.append(("country_alias", dict(row)))
                else:  # machine_alias
                    norm = str(key.get("alias_norm", "")).strip()
                    scope = _scope(key.get("destination_scope"))
                    row = conn.execute(
                        "SELECT * FROM machine_aliases WHERE alias_norm = ? AND destination_scope = ?",
                        (norm, scope),
                    ).fetchone()
                    if row is None:
                        raise ValidationError(
                            f"机型别名 {norm!r} scope {scope or 'default'} 不存在，整批已撤销"
                        )
                    resolved.append(("machine_alias", dict(row)))
            # Apply all inside the same transaction.
            for kind, current in resolved:
                if kind == "country":
                    self._apply_country_enabled(
                        conn, current, enabled, actor=actor, reason=reason, now=now
                    )
                elif kind == "machine":
                    self._apply_machine_enabled(
                        conn, current, enabled, actor=actor, reason=reason, now=now
                    )
                elif kind == "country_alias":
                    self._apply_country_alias_enabled(
                        conn, current, enabled, actor=actor, reason=reason
                    )
                else:
                    self._apply_machine_alias_enabled(
                        conn, current, enabled, actor=actor, reason=reason
                    )
                applied += 1
        return {"resource": resource, "count": applied, "enabled": bool(enabled)}

    def machine_aliases(self) -> dict[str, str]:
        """Return unambiguous enabled alias -> model mappings for the core API.

        Scoped aliases are additionally exposed through
        :meth:`machine_aliases_scoped` so the core can resolve them using the
        destination. An alias that exists in more than one scope is omitted
        from this flat mapping to avoid guessing. Disabled aliases are excluded.
        """

        rows = self.db.fetchall(
            """
            SELECT alias_norm, model_norm, destination_scope
              FROM machine_aliases
             WHERE enabled = 1
               AND EXISTS (
                   SELECT 1 FROM machine_materials m
                    WHERE m.model_norm = machine_aliases.model_norm
                      AND m.destination_scope = machine_aliases.destination_scope
                      AND m.enabled = 1
               )
            """
        )
        grouped: dict[str, set[str]] = {}
        for row in rows:
            grouped.setdefault(str(row["alias_norm"]), set()).add(str(row["model_norm"]))
        return {
            alias: next(iter(targets))
            for alias, targets in grouped.items()
            if len(targets) == 1
        }

    def list_machine_aliases(self) -> list[dict[str, Any]]:
        """All machine aliases (including disabled) for the management UI."""

        rows = self.db.fetchall(
            """
            SELECT a.alias_norm, a.alias_raw, a.model_norm, a.destination_scope,
                   a.enabled, m.model
              FROM machine_aliases a
              LEFT JOIN machine_materials m
                ON m.model_norm = a.model_norm
               AND m.destination_scope = a.destination_scope
             ORDER BY a.alias_raw, a.destination_scope
            """
        )
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["destination_scope"] = str(item["destination_scope"] or "")
            result.append(item)
        return result

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
                 WHERE a.enabled = 1 AND m.enabled = 1
                 ORDER BY a.alias_norm, a.destination_scope
                """
            )
        ]

    # ------------------------------------------------------------------
    # snapshots for the core process_job request
    # ------------------------------------------------------------------
    def core_references(self) -> dict[str, Any]:
        """Build the reference snapshot sent to the core.

        The snapshot is explicitly marked ``authoritative``: the SQLite data is
        the source of truth for new tasks, and the core must not fall back to
        its built-in country table or to a disabled machine scope. Disabled
        countries and aliases are omitted so they cannot match; disabled
        machine scopes are included with ``enabled=False`` so the core can tell
        "this scope is disabled, do not fall back to default" apart from "no
        scope entry exists, default is acceptable".
        """

        countries: list[dict[str, Any]] = []
        for row in self.db.fetchall(
            "SELECT * FROM countries WHERE enabled = 1 ORDER BY alpha3"
        ):
            aliases = [
                str(alias["alias_raw"])
                for alias in self.db.fetchall(
                    "SELECT alias_raw FROM country_aliases WHERE alpha3 = ? AND enabled = 1 ORDER BY alias_raw",
                    (row["alpha3"],),
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
                "enabled": bool(row["enabled"]),
                "aliases": [
                    str(alias["alias_raw"])
                    for alias in self.db.fetchall(
                        """
                        SELECT alias_raw FROM machine_aliases
                         WHERE model_norm = ? AND destination_scope = ? AND enabled = 1
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
            "authoritative": True,
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

    # ------------------------------------------------------------------
    # Excel machine import: preview then confirm (no silent overwrite)
    # ------------------------------------------------------------------
    IMPORT_PREVIEW_TTL_SECONDS = 3600

    _COMPARISON_DECL_FIELDS = ("english_name", "hs_code", "product_name", "declaration")

    def preview_machine_import(
        self,
        rows: list[dict[str, Any]],
        *,
        filename: str,
        source_path: str,
        actor: str,
    ) -> dict[str, Any]:
        """Compute a write-free diff of parsed import rows against the DB.

        Returns a summary grouped into new / unchanged / modified / disabled /
        conflict entries and stores a short-lived token bound to the current
        reference version. Nothing is written. An intra-batch conflict (the
        same model+scope appearing with different declaration data) rejects the
        whole preview, matching the atomic import contract.
        """

        prepared: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in rows:
            model = str(raw.get("model") or "").strip()
            if not model:
                raise ValidationError("机型参考行缺少机型")
            norm = normalize_key(model)
            scope = _scope(raw.get("destination_scope"))
            item = self._normalize_import_row(raw, model, scope)
            key = (norm, scope)
            if key in prepared:
                prior = prepared[key]
                # Compare field-by-field so swapped values (e.g. two different
                # columns exchanged) are detected as a conflict rather than
                # collapsing to the same set.
                prior_fields = {
                    field: prior.get(field) for field in self._COMPARISON_DECL_FIELDS
                }
                item_fields = {
                    field: item.get(field) for field in self._COMPARISON_DECL_FIELDS
                }
                if prior_fields != item_fields or prior.get("special") != item.get("special"):
                    raise ValidationError(
                        f"同批导入中 {model!r} scope {scope or 'default'} 申报资料矛盾，整体拒绝"
                    )
                prior["aliases"] = sorted(set(prior["aliases"]) | set(item["aliases"]))
            else:
                prepared[key] = item

        new: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        disabled: list[dict[str, Any]] = []
        for (norm, scope), item in prepared.items():
            existing = self.get_machine(norm, scope)
            entry = {
                "model": item["model"],
                "destination_scope": scope,
                "english_name": item["english_name"],
                "hs_code": item["hs_code"],
                "product_name": item["product_name"],
                "declaration": item["declaration"],
                "special": item["special"],
                "verification": item["verification"],
                "source_row": item["source_row"],
            }
            if existing is None:
                entry["action"] = "new"
                new.append(entry)
            elif not existing["enabled"]:
                # A disabled row is never resurrected by import. It is reported
                # so the operator sees the overlap, but confirm skips it.
                entry["action"] = "disabled"
                disabled.append(entry)
            else:
                decl_match = all(
                    existing[field] == item[field] for field in self._COMPARISON_DECL_FIELDS
                )
                if decl_match and existing.get("special") == item.get("special"):
                    entry["action"] = "unchanged"
                    unchanged.append(entry)
                else:
                    entry["action"] = "modified"
                    entry["previous"] = {
                        "english_name": existing["english_name"],
                        "hs_code": existing["hs_code"],
                        "product_name": existing["product_name"],
                        "declaration": existing["declaration"],
                        "special": existing["special"],
                    }
                    modified.append(entry)
        import secrets
        from datetime import datetime, timedelta, timezone

        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=self.IMPORT_PREVIEW_TTL_SECONDS)
        summary = {
            "new": len(new),
            "unchanged": len(unchanged),
            "modified": len(modified),
            "disabled": len(disabled),
        }
        payload = {
            "items": list(prepared.values()),
            "new": new,
            "unchanged": unchanged,
            "modified": modified,
            "disabled": disabled,
        }
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO import_previews (
                    token, filename, source_path, reference_version, rows_json,
                    summary_json, status, actor, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    token,
                    filename,
                    source_path,
                    self.reference_version(),
                    json.dumps(payload, ensure_ascii=False, default=str),
                    json.dumps(summary, ensure_ascii=False),
                    actor,
                    now.isoformat().replace("+00:00", "Z"),
                    expires.isoformat().replace("+00:00", "Z"),
                ),
            )
        self.logger.info(
            "machine_import_previewed", actor=actor, filename=filename, **summary
        )
        return {
            "token": token,
            "filename": filename,
            "summary": summary,
            "new": new,
            "unchanged": unchanged,
            "modified": modified,
            "disabled": disabled,
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        }

    def get_preview(self, token: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM import_previews WHERE token = ?", (token.strip(),)
        )
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def _normalize_import_row(raw: dict[str, Any], model: str, scope: str) -> dict[str, Any]:
        return {
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
            "source": raw.get("source") or "import",
            "aliases": [
                str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip()
            ],
        }

    def confirm_machine_import(
        self,
        token: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a previewed import atomically.

        Refuses an expired or already-used token, and refuses if the reference
        data changed between preview and confirm (TOCTOU). Disabled rows are
        skipped (never resurrected). New rows are inserted enabled; modified
        rows have their declaration fields and metadata updated. The whole
        apply runs in one transaction; any failure rolls back with nothing
        partially written.
        """

        from datetime import datetime, timezone

        now = utc_now()
        inserted = 0
        updated = 0
        skipped_disabled = 0
        # Everything (token re-read, expiry/version re-check, apply, token
        # consume) happens inside ONE BEGIN IMMEDIATE transaction. A concurrent
        # writer cannot change references between the check and the apply, and
        # the token cannot be consumed twice.
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM import_previews WHERE token = ?", (token.strip(),)
            ).fetchone()
            if row is None:
                raise ValidationError("预览不存在或已失效")
            if str(row["status"]) != "open":
                raise ValidationError("该预览已使用，请重新预览后再确认")
            expires = datetime.fromisoformat(
                str(row["expires_at"]).replace("Z", "+00:00")
            )
            if expires < datetime.now(timezone.utc):
                raise ValidationError("预览已过期，请重新预览后再确认")
            current_version = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS version FROM config_versions"
            ).fetchone()
            if int(row["reference_version"]) != int(current_version["version"]):
                raise ValidationError(
                    "预览后参考资料已变化，为避免覆盖请重新预览后再确认"
                )
            payload = json.loads(str(row["rows_json"]))
            items = payload["items"] if isinstance(payload, dict) else payload
            for raw in items:
                model = str(raw["model"]).strip()
                norm = normalize_key(model)
                scope = str(raw.get("destination_scope") or "")
                item = self._normalize_import_row(raw, model, scope)
                existing = conn.execute(
                    """
                    SELECT * FROM machine_materials
                     WHERE model_norm = ? AND destination_scope = ?
                    """,
                    (norm, scope),
                ).fetchone()
                if existing is not None:
                    if not bool(existing["enabled"]):
                        skipped_disabled += 1
                        continue
                    conn.execute(
                        """
                        UPDATE machine_materials
                           SET english_name = ?, hs_code = ?, product_name = ?,
                               declaration = ?, notes = ?, special = ?,
                               verification = ?, special_rule_confirmed = ?,
                               source_row = ?, source = ?, updated_at = ?
                         WHERE model_norm = ? AND destination_scope = ?
                        """,
                        (
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
                            norm,
                            scope,
                        ),
                    )
                    updated += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO machine_materials (
                            model_norm, destination_scope, model, english_name,
                            hs_code, product_name, declaration, notes, special,
                            verification, special_rule_confirmed, source_row,
                            source, enabled, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            norm,
                            scope,
                            model,
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
                    self._insert_machine_alias(conn, norm, scope, alias, now)
            conn.execute(
                "UPDATE import_previews SET status = 'confirmed' WHERE token = ?",
                (token.strip(),),
            )
            conn.execute(
                """
                INSERT INTO config_versions(key, value_json, previous_json, actor, reason, created_at)
                VALUES ('machine_reference', ?, NULL, ?, ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "source": str(row["filename"]),
                            "inserted": inserted,
                            "updated": updated,
                            "skipped_disabled": skipped_disabled,
                        },
                        ensure_ascii=False,
                    ),
                    actor,
                    reason or "machine reference import (preview confirmed)",
                    now,
                ),
            )
        self.logger.info(
            "machine_reference_imported",
            source=str(row["filename"]),
            inserted=inserted,
            updated=updated,
            skipped_disabled=skipped_disabled,
        )
        return {
            "inserted": inserted,
            "updated": updated,
            "skipped_disabled": skipped_disabled,
        }


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