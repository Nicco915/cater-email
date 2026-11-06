"""Inbound intake: allowlist, loop filters, dedupe, cursors and supplements."""

from __future__ import annotations

import os
import shutil
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
from ..core.parsing.mail_html import mail_has_business_table
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
    normalize_filename,
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

XLSX_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/xlsx",
}
XLS_CONTENT_TYPES = {
    "application/vnd.ms-excel",
    "application/x-xls",
    "application/msexcel",
}
WORKBOOK_SUFFIXES = {"xlsx", "xls"}


def _is_workbook_attachment(item: ParsedAttachment) -> bool:
    """按扩展名或 MIME 识别可解析工作簿（xlsx 与真正 BIFF xls）。"""
    if str(item.suffix).lower() in WORKBOOK_SUFFIXES:
        return True
    content_type = str(item.content_type or "").lower()
    return content_type in XLSX_CONTENT_TYPES or content_type in XLS_CONTENT_TYPES


@dataclass
class IngestResult:
    mail_id: str
    task_id: str | None = None
    created: bool = False
    duplicate: bool = False
    filtered: bool = False
    filter_reason: str | None = None
    reprocess_requested: bool = False
    should_process: bool = False
    supplement: bool = False
    needs_question: bool = False
    waiting: bool = False
    status_query: bool = False
    retry_command: bool = False
    retry_waiting: bool = False
    notice: str | None = None
    notice_suffix: str | None = None
    guidance: str | None = None
    note: str | None = None


# Generic, task-free guidance for command mails that cannot be linked to a
# task. It intentionally contains no task information.
GUIDANCE_UNKNOWN_TASK = (
    "您好：\n\n"
    "这封邮件包含操作指令，但我们无法确定它对应哪一票任务，因此没有执行任何操作，也没有创建新任务。\n"
    "如需查询进度或重试，请直接回复系统发送给您的最新通知邮件；\n"
    "或者新写一封邮件，第一行填写：任务编号：task_xxx（把 task_xxx 换成通知邮件主题或正文中的任务编号），"
    "第二行单独写：查询状态 或 重试。\n"
    "如仍无法解决，请联系维护人员。"
)

GUIDANCE_NOT_OWNER = (
    "您好：\n\n"
    "这封邮件关联的任务只能由原发件人本人通过邮件操作，我们未执行任何操作，也无法提供该任务的信息。\n"
    "如需查询进度，请由原发件人回复最新通知邮件，或新写邮件注明任务编号并单独写：查询状态。\n"
    "如有疑问，请联系维护人员。"
)

GUIDANCE_DELETED_TASK = (
    "您好：\n\n"
    "您回复的任务已从列表删除并进入回收站，本次未执行重新处理、重试或资料补充，也没有发送任何结果。\n"
    "如需恢复该任务，请联系维护人员从回收站恢复后再操作；恢复本身不会自动重发或重新生成。\n"
    "查询状态请单独写：查询状态。"
)


def _mail_fingerprint(mail: dict[str, Any]) -> str:
    return str(mail.get("raw_hash") or mail.get("message_id") or mail["id"])


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
        if supplement.has_command:
            # A command that cannot be pinned to exactly one task must still
            # get the sender usable guidance (no task information is leaked),
            # and it must never spawn a business task.
            if association.ambiguous:
                result = self._filter(
                    mail,
                    "ambiguous_task_reference",
                    note=association.reason,
                )
                result.guidance = GUIDANCE_UNKNOWN_TASK
                return result
            if association.task_id is None and association.unknown_task_ids:
                result = self._filter(
                    mail,
                    "unknown_task_id",
                    note=", ".join(association.unknown_task_ids),
                )
                result.guidance = GUIDANCE_UNKNOWN_TASK
                return result
            if association.task_id is None:
                result = self._filter(mail, "command_without_task_reference")
                result.guidance = GUIDANCE_UNKNOWN_TASK
                return result
        else:
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
                    "required_action": "Resend the xlsx or xls attachment.",
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
            result = self._filter(
                mail,
                "supplement_from_non_owner",
                task_id=str(task["id"]),
            )
            result.guidance = GUIDANCE_NOT_OWNER
            return result
        self.mail_store.set_task(str(mail["id"]), str(task["id"]))
        version = int(task["version"])
        # A trashed (recycle-bin) task is frozen: data supplements and
        # reprocess/retry commands are refused. A status query is the only
        # command still answered, and it only tells the owner the task was
        # deleted and can be recovered by maintenance — no other task is leaked.
        if task.get("deleted_at") is not None:
            if supplement.has_command and supplement.commands[0] == "status":
                return self._handle_command(mail, parsed, supplement, task, version)
            result = self._filter(
                mail,
                "task_deleted",
                task_id=str(task["id"]),
            )
            result.guidance = GUIDANCE_DELETED_TASK
            return result
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

        # ----- owner commands: 查询状态 / 重试 / 确认重新处理 ---------------
        # A mail may carry at most one command, and a command mail must not
        # bundle attachments or data directives; nothing may be half-applied.
        command_count = len(supplement.commands)
        non_inline_attachments = [item for item in parsed.attachments if not item.is_inline]
        if command_count > 1:
            return self._reject_command(
                mail,
                task,
                version,
                "command_mixed",
                "一封邮件只能包含一个指令，本次未执行任何操作。"
                "查询进度请单独写：查询状态；技术故障重试请单独写：重试；"
                "重新处理请单独写：确认重新处理。",
            )
        if command_count == 1 and (
            supplement.destination
            or supplement.mappings
            or supplement.overrides
            or supplement.bare_fields
            or non_inline_attachments
            or mail_has_business_table(
                parsed.html_body, parsed.text_body,
                self.config.core.header_aliases or None)
        ):
            return self._reject_command(
                mail,
                task,
                version,
                "command_with_payload",
                "指令邮件不能夹带附件、业务表格或字段修改，本次未执行任何操作，您的资料未作改动。"
                "请先按提示格式发送需要补充的附件或业务表，再单独发送指令。",
            )
        if command_count == 1:
            return self._handle_command(mail, parsed, supplement, task, version)

        # ----- data supplements and/or attachment replacement -------------
        business_attachments = self._business_attachments(parsed)
        # 表格补件：原任务发起人直接回复完整业务表（HTML 或以 Tab/空格
        # 明确分列的文本，quoted 历史不计）即视为实质性补充——缺失字段由
        # 核心按 MSO 合并填充，不要求逐条 修正： 指令。
        table_present = mail_has_business_table(
            parsed.html_body, parsed.text_body,
            self.config.core.header_aliases or None)
        if not supplement.has_actions and not business_attachments and not table_present:
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
                    "修正：<record_key>.<field>=<value>, "
                    "or paste the full business table (header + MSO column)."
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

        # Plan attachment replacement before any write: same-name attachments
        # replace the matching input for the new version only (old files and
        # old version rows stay untouched); different or conflicting names
        # pause the task with an explanation instead of guessing.
        staged: list[dict[str, Any]] = []
        replaced_names: set[str] = set()
        if business_attachments:
            plan_issue, staged, replaced_names = self._stage_replacements(
                str(task["id"]), version, str(mail["id"]), business_attachments
            )
            if plan_issue is not None:
                plan_issue["sources"] = [{"kind": "mail", "mail_id": mail["id"]}]
                self.tasks.add_issue(str(task["id"]), version, plan_issue)
                self.logger.warning(
                    "attachment_replacement_blocked",
                    task_id=task["id"],
                    mail_id=mail["id"],
                    code=plan_issue["code"],
                )
                return IngestResult(
                    mail_id=str(mail["id"]),
                    task_id=str(task["id"]),
                    supplement=True,
                    needs_question=True,
                    note="attachment_replacement_blocked",
                )

        applied_errors: list[dict[str, Any]] = []
        # Validate every directive before writing anything. A malformed later
        # line must not leave a half-applied alias or destination change.
        validated_aliases: list[tuple[str, str, str]] = []
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
                continue
            seen_alias_codes[alias_norm] = alpha3
            validated_aliases.append((alias_norm, mapping.alias.strip(), alpha3))

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
            self._discard_staged(str(task["id"]), str(mail["id"]), staged)
            for issue in applied_errors:
                self.tasks.add_issue(str(task["id"]), version, issue)
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=str(task["id"]),
                supplement=True,
                needs_question=True,
                note="supplement_validation_failed",
            )

        # All directives are valid. Phase 1: move the staged files into the
        # future version's inputs directory. Phase 2: commit every database
        # change (aliases with audit, the new version, destination, overrides
        # and input rows) in ONE transaction. A failure in either phase
        # removes the unreferenced files and leaves the old active version,
        # its inputs and the reference configuration untouched.
        task_id = str(task["id"])
        future_version = version + 1
        directory = self.config.tasks_dir / task_id / f"v{future_version}" / "inputs"
        moved: list[Path] = []
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for item in staged:
                target = directory / safe_filename(str(item["stored_name"]))
                os.replace(item["path"], target)
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
                moved.append(target)
        except OSError:
            self._cleanup_moved(task_id, str(mail["id"]), moved)
            self.logger.exception(
                "attachment_store_failed_task",
                OSError("attachment commit failed"),
                task_id=task_id,
            )
            self.tasks.add_issue(
                task_id,
                version,
                {
                    "code": "ATTACHMENT_WRITE_FAILED",
                    "record_key": None,
                    "field": "attachment",
                    "message": "补件附件无法安全保存，本次未自动重新处理。",
                    "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                    "required_action": "请重新发送 xlsx 或 xls 附件。",
                },
            )
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=task_id,
                supplement=True,
                needs_question=True,
                note="supplement_attachment_write_failed",
            )

        input_rows = [
            {
                "mail_id": str(mail["id"]),
                "file_hash": str(item["sha256"]),
                "path": str(directory / safe_filename(str(item["stored_name"]))),
                "filename": str(item["filename"]),
                "mime_type": str(item["mime_type"]),
                "size_bytes": int(item["size"]),
            }
            for item in staged
        ]
        override_rows = [
            {
                "record_key": item.record_key,
                "field": item.field,
                "value": item.value,
                "source_message_id": parsed.message_id,
            }
            for item in supplement.overrides
        ] + [
            {
                "record_key": item["record_key"],
                "field": item["field"],
                "value": item["value"],
                "source_message_id": parsed.message_id,
            }
            for item in bare_overrides
        ]
        try:
            task = self.tasks.apply_supplement(
                task_id,
                actor=str(task["owner"]),
                reason=f"supplement {mail['id']}",
                exclude_filenames=replaced_names,
                aliases=validated_aliases,
                destination=(
                    (destination["name_zh"], destination["alpha3"]) if destination else None
                ),
                overrides=override_rows,
                inputs=input_rows,
                version_mails=(
                    [(str(mail["id"]), "supplement_table")] if table_present else []
                ),
            )
        except Exception:
            self._cleanup_moved(task_id, str(mail["id"]), moved)
            self.logger.exception(
                "supplement_apply_failed",
                Exception("supplement transaction failed"),
                task_id=task_id,
            )
            self.tasks.add_issue(
                task_id,
                version,
                {
                    "code": "SUPPLEMENT_APPLY_FAILED",
                    "record_key": None,
                    "field": None,
                    "message": "本次补充内容未能保存，任务未作任何改动。",
                    "sources": [{"kind": "mail", "mail_id": mail["id"]}],
                    "required_action": "请重新发送补充内容；多次失败请联系维护人员。",
                },
            )
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=task_id,
                supplement=True,
                needs_question=True,
                note="supplement_apply_failed",
            )
        shutil.rmtree(
            self.config.tasks_dir / task_id / "incoming" / str(mail["id"]),
            ignore_errors=True,
        )
        version = int(task["version"])
        self.logger.info(
            "supplement_applied",
            task_id=task["id"],
            mail_id=mail["id"],
            version=version,
            mappings=len(validated_aliases),
            overrides=len(override_rows),
            replaced_inputs=sorted(replaced_names),
            destination=destination["alpha3"] if destination else None,
        )
        return IngestResult(
            mail_id=str(mail["id"]),
            task_id=task_id,
            supplement=True,
            should_process=True,
        )

    def _cleanup_moved(
        self, task_id: str, mail_id: str, moved: list[Path]
    ) -> None:
        """Remove unreferenced files after a failed commit and drop staging.

        The aborted future version directory only contains files this commit
        attempt moved there, so it is removed whole once they are unlinked.
        """

        for path in moved:
            try:
                path.unlink()
            except OSError:
                pass
        if moved:
            version_dir = moved[0].parent.parent
            shutil.rmtree(version_dir, ignore_errors=True)
        shutil.rmtree(self.config.tasks_dir / task_id / "incoming" / mail_id, ignore_errors=True)

    # ------------------------------------------------------------------
    # owner mail commands
    # ------------------------------------------------------------------
    def _handle_command(
        self,
        mail: dict[str, Any],
        parsed: ParsedMail,
        supplement: SupplementParse,
        task: dict[str, Any],
        version: int,
    ) -> IngestResult:
        task_id = str(task["id"])
        # Replay protection: the same mail (same raw content or Message-ID)
        # must never execute a command twice, however the server redelivers it.
        if self._command_replay(task_id, mail):
            reason = {
                "status": "duplicate_status_query",
                "retry": "duplicate_retry_command",
                "reprocess": "duplicate_reprocess_confirmation",
            }[supplement.commands[0]]
            return self._filter(mail, reason, task_id=task_id)
        command = supplement.commands[0]
        if command == "status":
            self.tasks.add_event(
                task_id,
                stage="intake",
                code="mail_status_query",
                actor=str(task["owner"]),
                detail={"mail_id": mail["id"], "version": version},
            )
            return IngestResult(
                mail_id=str(mail["id"]), task_id=task_id, supplement=True, status_query=True
            )
        if command == "reprocess":
            return self._handle_reprocess_command(mail, parsed, task, version)
        return self._handle_retry_command(mail, task, version)

    def _handle_reprocess_command(
        self,
        mail: dict[str, Any],
        parsed: ParsedMail,
        task: dict[str, Any],
        version: int,
    ) -> IngestResult:
        task_id = str(task["id"])
        if task["status"] not in {
            state.COMPLETED,
            state.WAITING_INPUT,
            state.FAILED,
            state.RETRY_PENDING,
            state.READY_TO_SEND,
        }:
            return self._reject_command(
                mail, task, version, "reprocess_status_blocked",
                "该任务当前不能确认重新处理；请等待当前处理完成，或联系维护人员。",
            )
        pending = self.db.fetchone(
            "SELECT id FROM outbox WHERE task_id=? AND status IN ('pending','sending','send_unknown') LIMIT 1",
            (task_id,),
        )
        if pending:
            return self._reject_command(
                mail, task, version, "reprocess_delivery_unsettled",
                "该任务有结果邮件尚未确认发送结果，暂时不能重新处理；请先联系维护人员核对，避免重复发送。",
            )
        # A late reply to an older version must not repeat a newer delivery.
        import re

        versions = re.findall(
            r"\[CATER\s+" + re.escape(task_id) + r"\s+v(\d+)\]",
            parsed.subject or "",
            flags=re.I,
        )
        if versions and any(int(value) != version for value in versions):
            return self._reject_command(
                mail, task, version, "reprocess_stale_version",
                "这封邮件对应的是旧版本任务。请回复最新的通知邮件（主题含最新版本号），"
                "新增正文单独写：确认重新处理。",
            )
        self.tasks.add_event(
            task_id,
            stage="intake",
            code="mail_reprocess_confirmed",
            actor=str(task["owner"]),
            detail={"mail_id": mail["id"], "version": version},
        )
        return IngestResult(
            mail_id=str(mail["id"]), task_id=task_id, supplement=True, reprocess_requested=True
        )

    def _handle_retry_command(
        self, mail: dict[str, Any], task: dict[str, Any], version: int
    ) -> IngestResult:
        task_id = str(task["id"])
        status = str(task["status"])
        if status in {state.RETRY_PENDING, state.FAILED}:
            self.tasks.add_event(
                task_id,
                stage="intake",
                code="mail_retry_requested",
                actor=str(task["owner"]),
                detail={"mail_id": mail["id"], "version": version},
            )
            return IngestResult(
                mail_id=str(mail["id"]), task_id=task_id, supplement=True, retry_command=True
            )
        if status == state.WAITING_INPUT:
            # Answered per command mail: older question rows are deduped by
            # issue digest and may never resend, but a fresh active command
            # must still receive the concrete missing-items guidance.
            return IngestResult(
                mail_id=str(mail["id"]),
                task_id=task_id,
                supplement=True,
                retry_waiting=True,
                note="retry_waiting_input",
            )
        if status in {state.RECEIVED, state.VALIDATING, state.PROCESSING}:
            return self._reject_command(
                mail, task, version, "retry_in_progress",
                "任务正在处理中，无需重试；完成后会按当前设置发送结果通知。",
            )
        if status == state.READY_TO_SEND:
            return self._reject_command(
                mail, task, version, "retry_already_ready",
                "结果文件已生成，正在等待发送，无需重试。"
                "如需基于最新资料重新生成，请回复：确认重新处理",
            )
        if status == state.COMPLETED:
            return self._reject_command(
                mail, task, version, "retry_completed",
                "该任务已完成，不会通过“重试”重复发送。"
                "如需基于最新资料重新处理，请回复：确认重新处理",
            )
        if status == state.SEND_UNKNOWN:
            return self._reject_command(
                mail, task, version, "retry_send_unknown",
                "该任务的结果邮件发送情况待人工确认，暂时不能重试，以免重复发送；请联系维护人员核对。",
            )
        return self._reject_command(
            mail, task, version, "retry_cancelled",
            "该任务已取消，不能通过邮件重试；请联系维护人员。",
        )

    def _reject_command(
        self,
        mail: dict[str, Any],
        task: dict[str, Any],
        version: int,
        reason: str,
        guidance: str,
    ) -> IngestResult:
        """Filter a rejected owner command without leaving the owner silent."""

        result = self._filter(mail, reason, task_id=str(task["id"]))
        result.notice = guidance
        result.notice_suffix = f"command_rejected:{reason}:v{version}"
        return result

    def _command_replay(self, task_id: str, mail: dict[str, Any]) -> bool:
        prior = self.db.fetchone(
            "SELECT id FROM mail_messages WHERE id<>? AND task_id=? AND "
            "(raw_hash=? OR (message_id IS NOT NULL AND message_id<>'' AND message_id=?)) LIMIT 1",
            (str(mail["id"]), task_id, mail["raw_hash"], mail.get("message_id")),
        )
        return prior is not None

    # ------------------------------------------------------------------
    # attachment replacement (supplements)
    # ------------------------------------------------------------------
    def _business_attachments(self, parsed: ParsedMail) -> list[ParsedAttachment]:
        valid: list[ParsedAttachment] = []
        for item in parsed.attachments:
            if item.is_inline:
                continue
            if not _is_workbook_attachment(item):
                continue
            if item.size > self.config.mail.max_attachment_bytes:
                continue
            valid.append(item)
        return valid

    def _stage_replacements(
        self,
        task_id: str,
        version: int,
        mail_id: str,
        attachments: list[ParsedAttachment],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], set[str]]:
        """Validate same-name replacement and stage files before any DB write.

        Returns ``(issue, staged, replaced_names)``. When ``issue`` is not
        None the caller must change nothing; ``staged`` is then empty and no
        file or version has been touched.
        """

        inputs = [
            item
            for item in self.tasks.inputs(task_id, version)
            if item.get("source_type") != "offline_original"
        ]
        # 内容判重覆盖本任务全部已存附件（含离线原始件）：相同字节再次上传
        # （同名或换名）等价于已保存附件，不产生第二个候选、不阻塞；既有
        # 输入行与来源审计原样保留。
        existing_hashes = {
            str(item.get("file_hash"))
            for item in self.tasks.inputs(task_id, version)
            if item.get("file_hash")
        }
        groups: dict[str, list[ParsedAttachment]] = {}
        for item in attachments:
            groups.setdefault(normalize_filename(item.filename), []).append(item)
        for group in groups.values():
            if len({item.sha256 for item in group}) > 1:
                return (
                    {
                        "code": "ATTACHMENT_NAME_CONFLICT",
                        "record_key": None,
                        "field": "attachment",
                        "message": group[0].filename,
                        "sources": [],
                        "required_action": (
                            "同一邮件中同名附件内容不一致；请只保留一份正确文件后重新发送。"
                        ),
                    },
                    [],
                    set(),
                )
        replaced: set[str] = set()
        unmatched: list[str] = []
        duplicate_names: set[str] = set()
        for name, group in groups.items():
            if any(normalize_filename(str(item["filename"])) == name for item in inputs):
                replaced.add(name)
            elif all(str(item.sha256) in existing_hashes for item in group):
                # 与已保存附件字节完全一致：不视为新附件，不阻塞不换名
                duplicate_names.add(name)
            else:
                unmatched.append(group[0].filename)
        if unmatched:
            if not inputs and len(groups) == 1:
                # The task has no business workbook yet (for example the
                # original mail had none or only an unsupported file): a
                # single recognizable attachment is accepted as the first
                # supply. Several different-name attachments stay ambiguous.
                pass
            elif not inputs:
                return (
                    {
                        "code": "ATTACHMENT_ROLE_UNKNOWN",
                        "record_key": None,
                        "field": "attachment",
                        "message": "、".join(unmatched),
                        "sources": [],
                        "required_action": (
                            "本任务尚未保存任何 Excel 附件，无法判别多份附件各自的角色，"
                            "本次未采用任何一份。请只发送本任务需要的一份 Excel；"
                            "如确需多份，请分多次发送并在每次沿用对应的原附件名，"
                            "或联系维护人员确认。"
                        ),
                    },
                    [],
                    set(),
                )
            else:
                current = "、".join(
                    dict.fromkeys(str(item["filename"]) for item in inputs)
                ) or "（当前版本没有已保存的 Excel 附件）"
                return (
                    {
                        "code": "ATTACHMENT_NAME_MISMATCH",
                        "record_key": None,
                        "field": "attachment",
                        "message": "、".join(unmatched),
                        "sources": [],
                        "required_action": (
                            "请沿用原附件名重新发送该 Excel 文件；"
                            f"当前参与本任务处理的附件名：{current}。"
                        ),
                    },
                    [],
                    set(),
                )
        staging = self.config.tasks_dir / task_id / "incoming" / mail_id
        staged: list[dict[str, Any]] = []
        try:
            staging.mkdir(parents=True, exist_ok=True)
            for item in attachments:
                if normalize_filename(item.filename) in duplicate_names:
                    # 与已保存附件完全相同：不落盘、不加输入行；已存附件即唯一候选
                    continue
                stored = stored_filename(item.sha256, item.filename)
                target = staging / safe_filename(stored)
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_bytes(item.data)
                os.replace(temporary, target)
                staged.append(
                    {
                        "path": target,
                        "stored_name": stored,
                        "filename": item.filename,
                        "sha256": item.sha256,
                        "size": item.size,
                        "mime_type": item.content_type,
                    }
                )
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            self.logger.exception(
                "attachment_store_failed_task",
                OSError("attachment staging failed"),
                task_id=task_id,
            )
            return (
                {
                    "code": "ATTACHMENT_WRITE_FAILED",
                    "record_key": None,
                    "field": "attachment",
                    "message": "、".join(group[0].filename for group in groups.values()),
                    "sources": [],
                    "required_action": "补件附件暂时无法安全保存，本次未作任何改动；请重新发送 xlsx 或 xls 附件。",
                },
                [],
                set(),
            )
        return None, staged, replaced

    def _discard_staged(
        self, task_id: str, mail_id: str, staged: list[dict[str, Any]]
    ) -> None:
        if not staged:
            return
        shutil.rmtree(self.config.tasks_dir / task_id / "incoming" / mail_id, ignore_errors=True)

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
                    "required_action": "Resend a smaller xlsx or xls attachment.",
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
                    "required_action": "Resend only the required xlsx or xls attachment.",
                }
            )
        for item in non_inline:
            if not _is_workbook_attachment(item):
                issues.append(
                    {
                        "code": "UNSUPPORTED_ATTACHMENT",
                        "record_key": None,
                        "field": "attachment",
                        "message": (
                            f"Attachment '{item.filename}' is not a supported workbook "
                            "(xlsx or xls). Images, PDFs, encrypted or password-protected "
                            "files are not guessed."
                        ),
                        "sources": [],
                        "required_action": "Resend the data as an unencrypted xlsx or xls attachment.",
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
                        "required_action": "Resend a smaller xlsx or xls attachment.",
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
            if not _is_workbook_attachment(item):
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