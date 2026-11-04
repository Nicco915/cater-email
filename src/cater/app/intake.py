"""Inbound intake: allowlist, loop filters, dedupe, cursors and supplements."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import state
from .config import AppConfig
from .db import Database
from .errors import ValidationError
from .logging import JsonlLogger
from .mail.gateway import RawMessage
from .mail.message import ParsedAttachment, ParsedMail, parse_mail
from .mail_store import MailStore
from .reference import ReferenceService
from .settings import SettingsService
from .supplement import (
    Association,
    SupplementParse,
    SupplementRouter,
    body_for_supplement,
    destination_only,
    parse_supplement,
)
from .tasks import TaskRepository
from .util import (
    normalize_email,
    normalize_key,
    safe_filename,
    sha256_bytes,
    stored_filename,
    utc_now,
)

AUTO_PROCESS_SUPPLEMENT_STATUSES = {
    state.WAITING_INPUT,
    state.RETRY_PENDING,
    state.FAILED,
}


@dataclass
class IngestResult:
    mail_id: str
    task_id: str | None = None
    created: bool = False
    duplicate: bool = False
    filtered: bool = False
    filter_reason: str | None = None
    should_process: bool = False
    supplement: bool = False
    needs_question: bool = False
    waiting: bool = False
    note: str | None = None


class IntakeService:
    def __init__(
        self,
        db: Database,
        config: AppConfig,
        logger: JsonlLogger,
        tasks: TaskRepository,
        mail_store: MailStore,
        references: ReferenceService,
        settings: SettingsService,
    ) -> None:
        self.db = db
        self.config = config
        self.logger = logger
        self.tasks = tasks
        self.mail_store = mail_store
        self.references = references
        self.settings = settings
        self.router = SupplementRouter(db)

    # ------------------------------------------------------------------
    # filters
    # ------------------------------------------------------------------
    def _filter(
        self,
        mail: dict[str, Any],
        reason: str,
        *,
        task_id: str | None = None,
        note: str | None = None,
    ) -> IngestResult:
        self.mail_store.set_task(str(mail["id"]), task_id, reason)
        self.logger.info(
            "mail_filtered",
            mail_id=mail["id"],
            task_id=task_id,
            sender=mail.get("sender"),
            reason=reason,
        )
        return IngestResult(
            mail_id=str(mail["id"]),
            task_id=task_id,
            filtered=True,
            filter_reason=reason,
            note=note,
        )

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------
    def ingest(self, raw_message: RawMessage) -> IngestResult:
        parsed = parse_mail(
            raw_message.raw, max_attachment_bytes=self.config.mail.max_attachment_bytes
        )
        raw_hash = sha256_bytes(raw_message.raw)
        raw_path = self.mail_store.save_raw(raw_message.raw, raw_hash)
        mail, created = self.mail_store.record(
            account=raw_message.account,
            folder=raw_message.folder,
            uid_validity=raw_message.uid_validity,
            uid=raw_message.uid,
            raw_hash=raw_hash,
            parsed=parsed,
            raw_path=str(raw_path),
            size_bytes=len(raw_message.raw),
        )
        if not created:
            # A coordinate alone is not proof of completion: if a previous
            # crash left the row unlinked and unfiltered, retry its intake.
            if mail.get("task_id") or mail.get("filter_reason"):
                self.logger.info(
                    "mail_dropped_duplicate_uid",
                    mail_id=mail["id"],
                    task_id=mail.get("task_id"),
                    uid=raw_message.uid,
                )
                return IngestResult(
                    mail_id=str(mail["id"]),
                    task_id=mail.get("task_id"),
                    duplicate=True,
                    filtered=True,
                    filter_reason="duplicate_mailbox_coordinate",
                )
            self.logger.warning(
                "mail_reprocessing_orphan_row",
                mail_id=mail["id"],
                uid=raw_message.uid,
            )

        sender = normalize_email(parsed.sender)
        active = self.settings.current().allowlist
        if not active.contains(sender):
            return self._filter(mail, "sender_not_allowlisted")
        own = {normalize_email(self.config.mail.account)} if self.config.mail.account else set()
        if sender in own:
            return self._filter(mail, "self_message")
        if parsed.is_bounce:
            return self._filter(mail, "bounce")
        if parsed.is_auto:
            return self._filter(mail, "automated_reply")

        supplement = parse_supplement(parsed)
        association = self.router.locate(
            supplement.task_ids,
            in_reply_to=parsed.in_reply_to,
            references=parsed.references,
        )
        if association.ambiguous:
            return self._filter(
                mail,
                "ambiguous_task_reference",
                note=association.reason,
            )
        if association.task_id is None and association.unknown_task_ids:
            return self._filter(
                mail,
                "unknown_task_id",
                note=", ".join(association.unknown_task_ids),
            )

        if association.task_id:
            return self._handle_supplement(mail, parsed, supplement, association)
        return self._handle_new(mail, parsed, supplement)

    # ------------------------------------------------------------------
    # new business mail
    # ------------------------------------------------------------------
    def _handle_new(
        self, mail: dict[str, Any], parsed: ParsedMail, supplement: SupplementParse
    ) -> IngestResult:
        prior_mails = self.mail_store.find_by_raw_hash(str(mail["raw_hash"]))
        for prior in prior_mails:
            if prior["id"] == mail["id"]:
                continue
            if prior.get("task_id"):
                self.mail_store.set_task(str(mail["id"]), str(prior["task_id"]), "duplicate_raw_content")
                self.logger.info(
                    "mail_duplicate_raw_content",
                    mail_id=mail["id"],
                    task_id=prior["task_id"],
                    original_mail_id=prior["id"],
                )
                return IngestResult(
                    mail_id=str(mail["id"]),
                    task_id=str(prior["task_id"]),
                    duplicate=True,
                    filtered=True,
                    filter_reason="duplicate_raw_content",
                )
            if prior.get("filter_reason"):
                return self._filter(mail, "duplicate_raw_content")

        destination_name = supplement.destination or destination_only(parsed)
        resolved = self.references.resolve(destination_name) if destination_name else None
        task = self.tasks.create(
            owner=normalize_email(parsed.sender),
            owner_name=parsed.sender_name,
            kind="auto",
            origin_mail_id=str(mail["id"]),
            destination_name=resolved["name_zh"] if resolved else destination_name,
            destination_alpha3=resolved["alpha3"] if resolved else None,
        )
        self.mail_store.set_task(str(mail["id"]), str(task["id"]))
        self.logger.info(
            "mail_accepted",
            mail_id=mail["id"],
            task_id=task["id"],
            message_id=parsed.message_id,
            attachment_count=len(parsed.attachments),
        )

        attachment_issues = self._check_attachments(parsed)
        saved_ok = self._save_attachments(
            task_id=str(task["id"]),
            version=int(task["version"]),
            mail_id=str(mail["id"]),
            attachments=parsed.attachments,
        )
        if not saved_ok:
            attachment_issues.append(
                {
                    "code": "ATTACHMENT_WRITE_FAILED",
                    "record_key": None,
                    "field": None,
                    "message": "An attachment could not be stored safely.",
                    "sources": [],
                    "required_action": "Resend the xlsx attachment.",
                }
            )

        if destination_name and resolved is None:
            attachment_issues.insert(
                0,
                {
                    "code": "DESTINATION_UNKNOWN",
                    "record_key": None,
                    "field": "destination",
                    "message": (
                        f"目的国 '{destination_name}' is not in the configured country mapping. "
                        "Reply with 目的国：<known name> or 国家映射：<alias>=<ISO alpha-3>."
                    ),
                    "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                    "required_action": "Provide a valid destination or country mapping.",
                },
            )

        if attachment_issues:
            self._wait_with_issues(task, attachment_issues)
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=str(task["id"]),
                created=True,
                needs_question=True,
            )
        return IngestResult(
            mail_id=str(mail["id"]),
            task_id=str(task["id"]),
            created=True,
            should_process=True,
        )

    # ------------------------------------------------------------------
    # supplements
    # ------------------------------------------------------------------
    def _handle_supplement(
        self,
        mail: dict[str, Any],
        parsed: ParsedMail,
        supplement: SupplementParse,
        association: Association,
    ) -> IngestResult:
        task = self.tasks.require(str(association.task_id))
        if normalize_email(parsed.sender) != normalize_email(str(task["owner"])):
            return self._filter(
                mail,
                "supplement_from_non_owner",
                task_id=str(task["id"]),
            )
        self.mail_store.set_task(str(mail["id"]), str(task["id"]))
        version = int(task["version"])
        self.logger.info(
            "supplement_received",
            mail_id=mail["id"],
            task_id=task["id"],
            version=version,
            method=association.method,
            directives=supplement.directive_count,
        )

        if supplement.errors:
            issue = {
                "code": "INVALID_SUPPLEMENT",
                "record_key": None,
                "field": None,
                "message": "The supplement contains invalid structured fields: "
                + "; ".join(supplement.errors),
                "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                "required_action": (
                    "Use 目的国：法国, 国家映射：France=FRA, "
                    "or 修正：<record_key>.<field>=<value>."
                ),
            }
            self.tasks.add_issue(str(task["id"]), version, issue)
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=str(task["id"]),
                supplement=True,
                needs_question=True,
                note="invalid_supplement",
            )

        if not supplement.has_actions:
            issue = {
                "code": "UNSTRUCTURED_SUPPLEMENT",
                "record_key": None,
                "field": None,
                "message": (
                    "No machine-readable supplement fields were found. "
                    "Free-form text is not used to change task data."
                ),
                "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                "required_action": (
                    "Reply with 目的国：法国, 国家映射：France=FRA, "
                    "or 修正：<record_key>.<field>=<value>."
                ),
            }
            self.tasks.add_issue(str(task["id"]), version, issue)
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=str(task["id"]),
                supplement=True,
                needs_question=True,
                note="unstructured_supplement",
            )

        if str(task["status"]) not in AUTO_PROCESS_SUPPLEMENT_STATUSES:
            issue = {
                "code": "SUPPLEMENT_REQUIRES_REVIEW",
                "record_key": None,
                "field": None,
                "message": (
                    "Structured supplement received, but the task is not awaiting input. "
                    "No data was changed automatically."
                ),
                "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                "required_action": "Use the admin reprocess action after reviewing the supplement.",
            }
            self.tasks.add_issue(str(task["id"]), version, issue)
            self.logger.warning(
                "supplement_requires_review",
                task_id=task["id"],
                mail_id=mail["id"],
                status=task["status"],
            )
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=str(task["id"]),
                supplement=True,
                note="supplement_requires_review",
            )

        attachment_issues = self._check_attachments(parsed)
        if attachment_issues:
            for issue in attachment_issues:
                issue["sources"] = [{"kind": "mail", "mail_id": mail["id"]}]
                self.tasks.add_issue(str(task["id"]), version, issue)
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=str(task["id"]),
                supplement=True,
                needs_question=True,
                note="supplement_attachment_issue",
            )

        applied_errors: list[dict[str, Any]] = []
        # Validate every directive before writing anything. A malformed later
        # line must not leave a half-applied alias or destination change.
        seen_alias_codes: dict[str, str] = {}
        for mapping in supplement.mappings:
            try:
                alias_norm, alpha3 = self.references.validate_alias(
                    mapping.alias, mapping.alpha3
                )
            except ValidationError as exc:
                applied_errors.append(
                    {
                        "code": "COUNTRY_MAPPING_CONFLICT",
                        "record_key": None,
                        "field": "destination",
                        "message": str(exc),
                        "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                        "required_action": "Confirm a non-conflicting country mapping explicitly.",
                    }
                )
                continue
            if alias_norm in seen_alias_codes and seen_alias_codes[alias_norm] != alpha3:
                applied_errors.append(
                    {
                        "code": "COUNTRY_MAPPING_CONFLICT",
                        "record_key": None,
                        "field": "destination",
                        "message": (
                            f"Same alias '{mapping.alias}' maps to multiple codes in this reply."
                        ),
                        "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                        "required_action": "Send one unambiguous mapping per alias.",
                    }
                )
            seen_alias_codes[alias_norm] = alpha3

        destination = None
        if supplement.destination:
            destination = self.references.resolve(supplement.destination)
            # A reply may introduce the alias and use it as 目的国 in the same
            # message. Resolve against the pending mapping without writing yet.
            if destination is None:
                pending_code = seen_alias_codes.get(normalize_key(supplement.destination))
                if pending_code:
                    destination = self.references.resolve(pending_code)
            if destination is None:
                applied_errors.append(
                    {
                        "code": "DESTINATION_UNKNOWN",
                        "record_key": None,
                        "field": "destination",
                        "message": f"目的国 '{supplement.destination}' is not configured.",
                        "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                        "required_action": "Provide a valid destination or country mapping.",
                    }
                )

        bare_overrides: list[dict[str, str]] = []
        if supplement.bare_fields:
            open_issues = [
                issue
                for issue in self.tasks.issues(str(task["id"]), version)
                if issue["status"] == "open" and issue.get("record_key")
            ]
            record_keys = list(dict.fromkeys(str(issue["record_key"]) for issue in open_issues))
            if len(record_keys) != 1:
                applied_errors.append(
                    {
                        "code": "AMBIGUOUS_RECORD_KEY",
                        "record_key": None,
                        "field": None,
                        "message": (
                            "MSO=/SO= fields require exactly one open issue with a record key; "
                            f"found {len(record_keys)}."
                        ),
                        "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                        "required_action": "Use 修正：<record_key>.<field>=<value>.",
                    }
                )
            else:
                record_key = record_keys[0]
                bare_overrides = [
                    {"record_key": record_key, "field": field, "value": value}
                    for field, value in supplement.bare_fields.items()
                ]

        if applied_errors:
            for issue in applied_errors:
                self.tasks.add_issue(str(task["id"]), version, issue)
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=str(task["id"]),
                supplement=True,
                needs_question=True,
                note="supplement_validation_failed",
            )

        # All directives are valid; now write the alias configuration. Each
        # call is individually audited in config_versions.
        for mapping in supplement.mappings:
            try:
                self.references.add_alias(
                    mapping.alias,
                    mapping.alpha3,
                    actor=str(task["owner"]),
                    reason=f"supplement {mail['id']}",
                )
            except ValidationError as exc:  # defensive: pre-validation passed
                issue = {
                    "code": "COUNTRY_MAPPING_CONFLICT",
                    "record_key": None,
                    "field": "destination",
                    "message": str(exc),
                    "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                    "required_action": "Confirm a non-conflicting country mapping explicitly.",
                }
                self.tasks.add_issue(str(task["id"]), version, issue)
                return IngestResult(
                    mail_id=str(mail["id"]),
                    task_id=str(task["id"]),
                    supplement=True,
                    needs_question=True,
                    note="supplement_validation_failed",
                )

        task = self.tasks.start_next_version(
            str(task["id"]),
            actor=str(task["owner"]),
            reason=f"supplement {mail['id']}",
            copy_inputs=True,
        )
        version = int(task["version"])
        if destination:
            self.tasks.update_destination(
                str(task["id"]), destination["name_zh"], destination["alpha3"]
            )
        for item in supplement.overrides:
            self.tasks.add_override(
                str(task["id"]),
                record_key=item.record_key,
                field=item.field,
                value=item.value,
                source_message_id=parsed.message_id,
            )
        for item in bare_overrides:
            self.tasks.add_override(
                str(task["id"]),
                record_key=item["record_key"],
                field=item["field"],
                value=item["value"],
                source_message_id=parsed.message_id,
            )
        saved_ok = self._save_attachments(
            task_id=str(task["id"]),
            version=version,
            mail_id=str(mail["id"]),
            attachments=parsed.attachments,
        )
        if not saved_ok:
            self.tasks.add_issue(
                str(task["id"]),
                version,
                {
                    "code": "ATTACHMENT_WRITE_FAILED",
                    "record_key": None,
                    "field": "attachment",
                    "message": "补件附件无法安全保存，本次未自动重新处理。",
                    "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                    "required_action": "请重新发送 xlsx 附件。",
                },
            )
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=str(task["id"]),
                supplement=True,
                needs_question=True,
                note="supplement_attachment_write_failed",
            )
        self.logger.info(
            "supplement_applied",
            task_id=task["id"],
            mail_id=mail["id"],
            version=version,
            mappings=len(supplement.mappings),
            overrides=len(supplement.overrides) + len(bare_overrides),
            destination=destination["alpha3"] if destination else None,
        )
        return IngestResult(
            mail_id=str(mail["id"]),
            task_id=str(task["id"]),
            supplement=True,
            should_process=True,
        )

    # ------------------------------------------------------------------
    # attachments
    # ------------------------------------------------------------------
    def _check_attachments(self, parsed: ParsedMail) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        non_inline = [item for item in parsed.attachments if not item.is_inline]
        total = sum(item.size for item in non_inline)
        if any(item.size > self.config.mail.max_attachment_bytes for item in non_inline):
            issues.append(
                {
                    "code": "ATTACHMENT_TOO_LARGE",
                    "record_key": None,
                    "field": "attachment",
                    "message": "One or more attachments exceed the per-file size limit.",
                    "sources": [],
                    "required_action": "Resend a smaller xlsx attachment.",
                }
            )
        if total > self.config.mail.max_total_attachment_bytes:
            issues.append(
                {
                    "code": "ATTACHMENT_TOTAL_TOO_LARGE",
                    "record_key": None,
                    "field": "attachment",
                    "message": "Attachments exceed the total size limit.",
                    "sources": [],
                    "required_action": "Resend only the required xlsx attachment.",
                }
            )
        for item in non_inline:
            suffix = item.suffix
            is_xlsx_type = item.content_type in {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/xlsx",
            }
            if suffix != "xlsx" and not is_xlsx_type:
                issues.append(
                    {
                        "code": "UNSUPPORTED_ATTACHMENT",
                        "record_key": None,
                        "field": "attachment",
                        "message": (
                            f"Attachment '{item.filename}' is not a supported xlsx workbook. "
                            "Images, PDFs, xls, encrypted or password-protected files are not guessed."
                        ),
                        "sources": [],
                        "required_action": "Resend the data as an unencrypted xlsx attachment.",
                    }
                )
        if any("attachment_too_large" in warning for warning in parsed.parse_warnings):
            if not any(issue["code"] == "ATTACHMENT_TOO_LARGE" for issue in issues):
                issues.append(
                    {
                        "code": "ATTACHMENT_TOO_LARGE",
                        "record_key": None,
                        "field": "attachment",
                        "message": "An attachment was truncated because it exceeds its size limit.",
                        "sources": [],
                        "required_action": "Resend a smaller xlsx attachment.",
                    }
                )
        return issues

    def _save_attachments(
        self,
        *,
        task_id: str,
        version: int,
        mail_id: str,
        attachments: list[ParsedAttachment],
    ) -> bool:
        valid: list[ParsedAttachment] = []
        for item in attachments:
            if item.is_inline:
                continue
            suffix = item.suffix
            is_xlsx_type = item.content_type in {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/xlsx",
            }
            if suffix != "xlsx" and not is_xlsx_type:
                continue
            if item.size > self.config.mail.max_attachment_bytes:
                continue
            valid.append(item)
        if sum(item.size for item in valid) > self.config.mail.max_total_attachment_bytes:
            return False
        directory = self.config.tasks_dir / task_id / f"v{version}" / "inputs"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for item in valid:
                filename = stored_filename(item.sha256, item.filename)
                path = directory / safe_filename(filename)
                if not path.exists():
                    temporary = path.with_name(path.name + ".tmp")
                    temporary.write_bytes(item.data)
                    os.replace(temporary, path)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                self.tasks.add_input(
                    task_id=task_id,
                    version=version,
                    mail_id=mail_id,
                    file_hash=item.sha256,
                    path=str(path),
                    filename=item.filename,
                    source_type="attachment",
                    mime_type=item.content_type,
                    size_bytes=item.size,
                )
        except OSError:
            self.logger.exception(
                "attachment_store_failed_task", OSError("attachment write failed"), task_id=task_id
            )
            return False
        return True

    def _wait_with_issues(self, task: dict[str, Any], issues: list[dict[str, Any]]) -> None:
        task_id = str(task["id"])
        if task["status"] == state.RECEIVED:
            self.tasks.transition(task_id, state.VALIDATING, stage="intake")
            task = self.tasks.require(task_id)
        if task["status"] == state.VALIDATING:
            self.tasks.transition(task_id, state.WAITING_INPUT, stage="intake", code="input_issue")
        for issue in issues:
            self.tasks.add_issue(task_id, int(task["version"]), issue)
        self.logger.warning(
            "task_waiting_input",
            task_id=task_id,
            version=task["version"],
            issue_codes=[issue.get("code") for issue in issues],
        )