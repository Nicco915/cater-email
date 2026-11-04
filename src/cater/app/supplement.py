"""Structured supplement parsing and task association.

Only documented, explicit forms are accepted. Free-form natural language is
never used to overwrite data. Quoted/forwarded history is excluded before
parsing so an old destination or mapping cannot be applied again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .db import Database
from .mail.message import ParsedMail, html_to_text

TASK_ID_PATTERN = r"task_[a-z0-9]{8,64}"
_MARKER_RE = re.compile(rf"\[CATER\s+({TASK_ID_PATTERN})\s+v(\d+)\]", re.I)
_TASK_LINE_RE = re.compile(
    rf"(?im)^\s*(?:任务编号|任务ID|task[-_ ]?id)\s*[:：]\s*({TASK_ID_PATTERN})\b"
)
_DEST_RE = re.compile(r"(?im)^\s*目的国\s*[:：]\s*(.+?)\s*$")
_MAPPING_RE = re.compile(
    r"(?im)^\s*国家映射\s*[:：]\s*(\S(?:.*?\S)?)\s*=\s*([A-Za-z]{3})\s*$"
)
_OVERRIDE_RE = re.compile(
    r"(?im)^\s*(?:修正|字段)\s*[:：]\s*(\S+?)\s*=\s*(.+?)\s*$"
)
_BARE_FIELD_RE = re.compile(r"^\s*(MSO|SO)\s*=\s*(.+?)\s*$", re.I)

_QUOTE_BOUNDARIES = (
    re.compile(r"^\s*On\s+.+\bwrote:\s*$", re.I),
    re.compile(r"^\s*在\s*.+写道\s*[:：]\s*$"),
    re.compile(r"^\s*-{2,}\s*(?:Original Message|原始邮件|原始郵件|转发的邮件|轉寄的郵件|Forwarded message)\s*-*\s*$", re.I),
    re.compile(r"^\s*_{10,}\s*$"),
    re.compile(
        r"^\s*(?:From|发件人|發件人|Sent|发送时间|發送時間|To|收件人|收件人|Subject|主题|主題)\s*[:：]",
        re.I,
    ),
    re.compile(r"^\s*-{5,}\s*$"),
    re.compile(r"^\s*>{1,}\s*", re.I),
)


@dataclass(frozen=True)
class CountryMapping:
    alias: str
    alpha3: str


@dataclass(frozen=True)
class OverrideDirective:
    record_key: str
    field: str
    value: str


@dataclass
class SupplementParse:
    task_ids: list[str] = field(default_factory=list)
    reprocess: bool = False
    destination: str | None = None
    mappings: list[CountryMapping] = field(default_factory=list)
    overrides: list[OverrideDirective] = field(default_factory=list)
    bare_fields: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    directive_count: int = 0

    @property
    def has_actions(self) -> bool:
        return bool(
            self.reprocess
            or self.destination
            or self.mappings
            or self.overrides
            or self.bare_fields
        )

    def has_task_signal(self) -> bool:
        return bool(self.task_ids)


@dataclass(frozen=True)
class Association:
    task_id: str | None
    method: str | None = None
    ambiguous: bool = False
    unknown_task_ids: tuple[str, ...] = ()
    reason: str | None = None


def strip_quoted(text: str) -> str:
    lines: list[str] = []
    for line in (text or "").splitlines():
        if any(pattern.match(line) for pattern in _QUOTE_BOUNDARIES):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def body_for_supplement(parsed: ParsedMail) -> str:
    if parsed.text_body.strip():
        return strip_quoted(parsed.text_body)
    return strip_quoted(html_to_text(parsed.html_body))


def _extract_task_ids(text: str) -> list[str]:
    found = [match.group(1).lower() for match in _MARKER_RE.finditer(text or "")]
    found.extend(match.group(1).lower() for match in _TASK_LINE_RE.finditer(text or ""))
    deduped: list[str] = []
    for task_id in found:
        if task_id not in deduped:
            deduped.append(task_id)
    return deduped


def parse_supplement(parsed: ParsedMail) -> SupplementParse:
    result = SupplementParse()
    subject = parsed.subject or ""
    result.task_ids = _extract_task_ids(subject)
    body = body_for_supplement(parsed)
    for task_id in _extract_task_ids(body):
        if task_id not in result.task_ids:
            result.task_ids.append(task_id)

    result.reprocess = any(line.strip() in {"重新处理", "确认重新处理"} for line in body.splitlines())
    if result.reprocess:
        result.directive_count += 1

    destinations = [match.group(1).strip() for match in _DEST_RE.finditer(body)]
    if destinations:
        distinct = list(dict.fromkeys(destinations))
        if len(distinct) > 1:
            result.errors.append("Multiple different 目的国 values were provided")
        else:
            result.destination = distinct[0]
            result.directive_count += 1

    mappings: list[CountryMapping] = []
    for match in _MAPPING_RE.finditer(body):
        alias = match.group(1).strip()
        alpha3 = match.group(2).strip().upper()
        if not alias or not re.fullmatch(r"[A-Za-z]{3}", alpha3):
            result.errors.append(f"Invalid 国家映射 line: {match.group(0).strip()}")
            continue
        mappings.append(CountryMapping(alias=alias, alpha3=alpha3))
    # Detect malformed mapping directives that were not matched.
    for line in body.splitlines():
        if re.match(r"^\s*国家映射\s*[:：]", line) and not _MAPPING_RE.match(line):
            result.errors.append(f"Invalid 国家映射 line: {line.strip()}")
    unique_mappings: list[CountryMapping] = []
    for mapping in mappings:
        key = (mapping.alias.casefold(), mapping.alpha3)
        if not any((item.alias.casefold(), item.alpha3) == key for item in unique_mappings):
            unique_mappings.append(mapping)
    result.mappings = unique_mappings
    result.directive_count += len(unique_mappings)

    overrides: list[OverrideDirective] = []
    for match in _OVERRIDE_RE.finditer(body):
        target = match.group(1).strip()
        value = match.group(2).strip()
        if "." not in target:
            result.errors.append(
                f"修正 line must use record_key.field=value: {match.group(0).strip()}"
            )
            continue
        record_key, field = target.rsplit(".", 1)
        if not record_key or not field or not value:
            result.errors.append(f"Invalid 修正 line: {match.group(0).strip()}")
            continue
        overrides.append(OverrideDirective(record_key=record_key, field=field, value=value))
    for line in body.splitlines():
        if re.match(r"^\s*(?:修正|字段)\s*[:：]", line) and not _OVERRIDE_RE.match(line):
            result.errors.append(f"Invalid 修正 line: {line.strip()}")
    unique_overrides: list[OverrideDirective] = []
    seen_override_keys: set[tuple[str, str]] = set()
    for item in overrides:
        key = (item.record_key.casefold(), item.field.casefold())
        if key in seen_override_keys:
            existing = next(
                entry
                for entry in unique_overrides
                if (entry.record_key.casefold(), entry.field.casefold()) == key
            )
            if existing.value != item.value:
                result.errors.append(
                    f"Conflicting 修正 values for {item.record_key}.{item.field}"
                )
            continue
        seen_override_keys.add(key)
        unique_overrides.append(item)
    result.overrides = unique_overrides
    result.directive_count += len(unique_overrides)

    bare: dict[str, str] = {}
    for line in body.splitlines():
        if re.match(r"^\s*(?:修正|字段|国家映射|目的国)\s*[:：]", line):
            continue
        segments = re.split(r"[；;]", line)
        for segment in segments:
            match = _BARE_FIELD_RE.match(segment)
            if not match:
                continue
            name = match.group(1).upper()
            value = match.group(2).strip()
            if not value:
                result.errors.append(f"Empty value for {name}")
                continue
            if name in bare and bare[name] != value:
                result.errors.append(f"Conflicting values for {name}")
                continue
            bare[name] = value
            result.directive_count += 1
    result.bare_fields = bare
    return result


def destination_only(parsed: ParsedMail) -> str | None:
    """Extract the initial destination using the same quoted-text rules."""

    body = body_for_supplement(parsed)
    values = [match.group(1).strip() for match in _DEST_RE.finditer(body)]
    distinct = list(dict.fromkeys(values))
    return distinct[0] if len(distinct) == 1 else None


class SupplementRouter:
    """Associate an inbound reply with exactly one task.

    Association uses only (a) our ``[CATER task_id vN]`` marker or an explicit
    ``任务编号：`` line and (b) In-Reply-To/References matching an outbox
    Message-ID. Conflicting or multiple matches are reported as ambiguous.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def _outbox_tasks(self, message_ids: Iterable[str]) -> set[str]:
        normalized = {
            item.strip().strip("<>") for item in message_ids if item and item.strip()
        }
        if not normalized:
            return set()
        placeholders = ",".join("?" for _ in normalized)
        rows = self.db.fetchall(
            f"""
            SELECT DISTINCT task_id FROM outbox
             WHERE trim(message_id, '<>') IN ({placeholders})
            """,
            tuple(sorted(normalized)),
        )
        return {str(row["task_id"]) for row in rows}

    def locate(
        self,
        explicit_task_ids: Iterable[str],
        *,
        in_reply_to: Iterable[str] = (),
        references: Iterable[str] = (),
    ) -> Association:
        explicit = [task_id.lower() for task_id in explicit_task_ids if task_id]
        reference_tasks = self._outbox_tasks([*in_reply_to, *references])

        unknown_explicit = tuple(
            task_id
            for task_id in explicit
            if self.db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (task_id,)) is None
        )
        known_explicit = [task_id for task_id in explicit if task_id not in unknown_explicit]
        candidates = set(known_explicit) | reference_tasks

        if len(candidates) > 1:
            return Association(
                task_id=None,
                ambiguous=True,
                unknown_task_ids=unknown_explicit,
                reason="multiple task references",
            )
        if unknown_explicit:
            if candidates:
                return Association(
                    task_id=None,
                    ambiguous=True,
                    unknown_task_ids=unknown_explicit,
                    reason="explicit task ID and reply headers disagree",
                )
            return Association(
                task_id=None,
                unknown_task_ids=unknown_explicit,
                reason="unknown task ID",
            )
        if not candidates:
            return Association(task_id=None)
        task_id = next(iter(candidates))
        if known_explicit and reference_tasks:
            method = "task_id+references"
        elif known_explicit:
            method = "task_id"
        else:
            method = "references"
        return Association(task_id=task_id, method=method)