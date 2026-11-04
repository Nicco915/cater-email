"""Application service used by CLI, polling loop and the local web UI."""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Callable

from . import state
from .config import AppConfig, CredentialProvider, ensure_runtime_dirs
from .db import Database
from .engine import CoreProcessEngine, CoreUnavailable, ProcessEngine, load_import_machine_reference
from .errors import (
    ConfigurationError,
    SendNotAllowedError,
    StateTransitionError,
    ValidationError,
)
from .intake import IngestResult, IntakeService
from .logging import JsonlLogger
from .mail.gateway import CompositeGateway, MailGateway, RawMessage
from .mail.message import ParsedAttachment, parse_mail
from .mail.msg import MsgParseError, parse_msg
from .mail_store import MailStore
from .maintenance import MaintenanceService
from .outbox import OutboxRepository
from .reference import ReferenceService
from .response import validate_core_response
from .settings import SettingsService
from .supplement import destination_only
from .tasks import TaskRepository
from .util import (
    EMAIL_RE,
    is_within,
    new_id,
    normalize_email,
    normalize_key,
    safe_filename,
    sha256_bytes,
    utc_now,
)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MSG_MIME = "application/vnd.ms-outlook"


@dataclass
class PollReport:
    scanned: int = 0
    accepted: int = 0
    filtered: int = 0
    duplicates: int = 0
    created_tasks: int = 0
    processed: int = 0
    waiting: int = 0
    errors: list[str] = field(default_factory=list)
    first_scan: bool = False
    uidvalidity_changed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "accepted": self.accepted,
            "filtered": self.filtered,
            "duplicates": self.duplicates,
            "created_tasks": self.created_tasks,
            "processed": self.processed,
            "waiting": self.waiting,
            "errors": list(self.errors),
            "first_scan": self.first_scan,
            "uidvalidity_changed": self.uidvalidity_changed,
        }


class AppService:
    def __init__(
        self,
        config: AppConfig,
        *,
        engine: ProcessEngine | None = None,
        gateway: MailGateway | None = None,
        credentials: CredentialProvider | None = None,
        logger: JsonlLogger | None = None,
        reference_importer: Callable[[str], list[dict[str, Any]]] | None = None,
        recover: bool = True,
    ) -> None:
        self.config = config
        ensure_runtime_dirs(config)
        self.logger = logger or JsonlLogger(
            config.logs_dir,
            max_bytes=config.logging.max_bytes,
            retention_days=config.logging.retention_days,
        )
        self.db = Database(config.db_path)
        self.references = ReferenceService(self.db, self.logger)
        self.references.ensure_seeded()
        self.settings = SettingsService(self.db, self.logger, config)
        self.tasks = TaskRepository(self.db, self.logger)
        self.mail_store = MailStore(self.db, config)
        self.outbox = OutboxRepository(self.db, self.logger)
        self.credentials = credentials or CredentialProvider()
        self.engine = engine or CoreProcessEngine()
        self.gateway = gateway or CompositeGateway(config.mail, self.credentials)
        self.intake = IntakeService(
            self.db,
            config,
            self.logger,
            self.tasks,
            self.mail_store,
            self.references,
            self.settings,
        )
        # Imported after core classes to avoid a circular import at module load.
        from .delivery import DeliveryService

        self.delivery = DeliveryService(
            self.db,
            config,
            self.settings,
            self.logger,
            self.tasks,
            self.outbox,
            self.mail_store,
            self.gateway,
        )
        self._reference_importer = reference_importer
        self.recovery_report: dict[str, Any] = {}
        self.maintenance = MaintenanceService(
            db=self.db,
            config=config,
            logger=self.logger,
            health_provider=self.health,
            recovery_provider=lambda: self.recovery_report,
        )
        self.logger.info(
            "application_started",
            dry_run=self.settings.current().sending.dry_run,
            sending_enabled=self.settings.current().sending.enabled,
            allowlist_mode=self.settings.current().allowlist.mode,
            schema_version=self.db.schema_version(),
        )
        if recover:
            self.recovery_report = self.recover()

    # ------------------------------------------------------------------
    # lifecycle / recovery
    # ------------------------------------------------------------------
    def recover(self) -> dict[str, Any]:
        interrupted_tasks = self.tasks.recover_incomplete()
        uncertain_outbox = self.outbox.recover_sending()
        send_unknown_tasks: list[str] = []
        for row in uncertain_outbox:
            try:
                self.tasks.add_event(
                    str(row["task_id"]),
                    stage="recovery",
                    code="send_uncertain_after_restart",
                    detail={"outbox_id": row["id"]},
                )
            except Exception:
                pass
            if row["purpose"] == "delivery":
                task = self.tasks.get(str(row["task_id"]))
                if task is not None and task["status"] == state.READY_TO_SEND:
                    try:
                        self.tasks.transition(
                            str(task["id"]),
                            state.SEND_UNKNOWN,
                            stage="recovery",
                            code="send_uncertain_after_restart",
                            detail={"outbox_id": row["id"]},
                        )
                        send_unknown_tasks.append(str(task["id"]))
                    except StateTransitionError:
                        pass
        if interrupted_tasks or uncertain_outbox:
            self.logger.warning(
                "startup_recovery",
                interrupted_tasks=interrupted_tasks,
                uncertain_outbox=[row["id"] for row in uncertain_outbox],
                send_unknown_tasks=send_unknown_tasks,
            )
        return {
            "interrupted_tasks": interrupted_tasks,
            "uncertain_outbox": [str(row["id"]) for row in uncertain_outbox],
            "send_unknown_tasks": send_unknown_tasks,
        }

    def close(self) -> None:
        try:
            self.gateway.close()
        finally:
            self.db.close()

    # ------------------------------------------------------------------
    # web/reference helpers
    # ------------------------------------------------------------------
    def import_machine_reference(self, path: str, *, actor: str = "admin") -> dict[str, Any]:
        importer = self._reference_importer or load_import_machine_reference()
        rows = importer(path)
        if not isinstance(rows, list):
            raise ValidationError("import_machine_reference must return a list")
        summary = self.references.store_machines(rows, source=path, actor=actor)
        self.logger.info("machine_reference_import", path=path, **summary)
        return summary

    def update_settings(
        self,
        changes: dict[str, Any],
        *,
        actor: str,
        reason: str | None = None,
        production_ack: bool = False,
    ) -> dict[str, Any]:
        effective = self.settings.update(
            changes, actor=actor, reason=reason, production_ack=production_ack
        )
        return effective.as_dict()

    def task_detail(self, task_id: str, *, version: int | None = None) -> dict[str, Any]:
        task = self.tasks.require(task_id)
        selected_version = int(version or task["version"])
        origin = (
            self.mail_store.get(str(task["origin_mail_id"]))
            if task.get("origin_mail_id")
            else None
        )
        task["mail_subject"] = origin.get("subject") if origin else None
        version_row = self.tasks.get_version(task_id, selected_version) or {}
        task["output_dir"] = version_row.get("output_dir")
        task["record_count"] = version_row.get("record_count", 0)
        versions = [
            dict(row)
            for row in self.db.fetchall(
                "SELECT * FROM task_versions WHERE task_id = ? ORDER BY version DESC",
                (task_id,),
            )
        ]
        return {
            "task": task,
            "version": selected_version,
            "versions": versions,
            "issues": self.tasks.issues(task_id, selected_version),
            "provenance": self.tasks.provenance(task_id, selected_version),
            "artifacts": self.tasks.artifacts(task_id, selected_version),
            "events": self.tasks.events(task_id),
            "outbox": self.outbox.for_task(task_id),
            "mails": self.mail_store.for_task(task_id),
            "inputs": self.tasks.inputs(task_id, selected_version),
            "overrides": self.tasks.overrides(task_id),
        }

    def health(self) -> dict[str, Any]:
        effective = self.settings.current()
        credentials = self.credentials.describe(self.config.mail.account)
        return {
            "schema_version": self.db.schema_version(),
            "quick_check": self.db.quick_check(),
            "tasks": self.tasks.counts(),
            "outbox_unknown": len(self.outbox.unknown()),
            "dry_run": effective.sending.dry_run,
            "sending_enabled": effective.sending.enabled,
            "allowlist_mode": effective.allowlist.mode,
            "active_allowlist_count": len(effective.allowlist.active()),
            "imap_host_configured": bool(self.config.mail.imap_host),
            "smtp_host_configured": bool(self.config.mail.smtp_host),
            "account_configured": bool(self.config.mail.account),
            # Booleans only: never render secret values.
            "credential_presence": credentials,
        }

    # ------------------------------------------------------------------
    # ingestion / polling
    # ------------------------------------------------------------------
    def ingest(self, raw_message: RawMessage) -> IngestResult:
        result = self.intake.ingest(raw_message)
        if result.should_process and result.task_id:
            self.process_task(result.task_id, actor="intake")
            task = self.tasks.require(result.task_id)
            if task["status"] == state.WAITING_INPUT:
                result.waiting = True
        if result.needs_question and result.task_id:
            task = self.tasks.require(result.task_id)
            if task["status"] == state.WAITING_INPUT:
                self._enqueue_question_and_dispatch(task, actor="intake")
        return result

    def poll_once(self, *, include_history: bool = False) -> PollReport:
        account = self.config.mail.account
        if not account:
            raise ConfigurationError("mail.account is not configured")
        folder = self.config.mail.folder
        cursor = self.mail_store.get_cursor(account, folder)
        # On the very first run the factory default is to establish a watermark
        # only, so an old mailbox is never processed by surprise. History is
        # processed only when explicitly requested (CLI --history) or when the
        # operator deliberately disabled first_run_new_only.
        first_run = cursor is None
        process_history = include_history or (
            first_run and not self.config.mail.first_run_new_only
        )
        last_uid = None if first_run or include_history else int(cursor["last_uid"])
        known_validity = None if first_run else cursor["uid_validity"]
        scan = self.gateway.scan(
            account,
            folder,
            last_uid=last_uid,
            uid_validity=known_validity,
            include_history=process_history,
        )
        report = PollReport(
            scanned=len(scan.messages),
            first_scan=scan.first_scan,
            uidvalidity_changed=scan.uidvalidity_changed,
        )
        if scan.first_scan:
            self.mail_store.save_cursor(
                account,
                folder,
                uid_validity=scan.uid_validity,
                last_uid=scan.last_uid,
                first_scan_completed=True,
            )
            self.logger.info(
                "mailbox_watermark_initialized",
                account=account,
                folder=folder,
                last_uid=scan.last_uid,
                note="history is not processed on first run unless explicitly requested",
            )
            return report
        if scan.uidvalidity_changed:
            self.logger.warning(
                "mailbox_uidvalidity_changed",
                account=account,
                folder=folder,
                new_uid_validity=scan.uid_validity,
                note="rescanning; existing fingerprints prevent duplicate tasks",
            )
        if scan.skipped_oversized:
            self.logger.error(
                "mail_oversized_skipped",
                account=account,
                folder=folder,
                skipped=list(scan.skipped_oversized),
            )
            report.errors.append(
                f"skipped {len(scan.skipped_oversized)} oversized message(s)"
            )
        last_safe_uid = (
            0
            if scan.uidvalidity_changed
            else (int(cursor["last_uid"]) if cursor else 0)
        )
        scan_complete = True
        for raw in scan.messages:
            try:
                result = self.ingest(raw)
            except Exception as exc:
                self.logger.exception(
                    "mail_ingest_failed",
                    exc,
                    account=raw.account,
                    folder=raw.folder,
                    uid=raw.uid,
                )
                report.errors.append(f"UID {raw.uid}: {type(exc).__name__}")
                # Do not advance past a message that failed before a task/filter
                # record was durably created.
                scan_complete = False
                break
            report.accepted += 1 if result.task_id and not result.filtered else 0
            report.filtered += 1 if result.filtered else 0
            report.duplicates += 1 if result.duplicate else 0
            report.created_tasks += 1 if result.created else 0
            if result.task_id:
                task = self.tasks.get(result.task_id)
                if task is not None:
                    if task["status"] == state.WAITING_INPUT:
                        report.waiting += 1
                    if task["status"] in {
                        state.READY_TO_SEND,
                        state.COMPLETED,
                        state.SEND_UNKNOWN,
                    }:
                        report.processed += 1
            last_safe_uid = max(last_safe_uid, raw.uid)
            self.mail_store.save_cursor(
                account,
                folder,
                uid_validity=scan.uid_validity if scan.uid_validity is not None else known_validity,
                last_uid=last_safe_uid,
                first_scan_completed=True,
            )
        # Advance over intentionally skipped messages (for example oversized
        # ones that are reported instead of retried), but never past a message
        # whose ingestion failed.
        if scan_complete:
            last_safe_uid = max(last_safe_uid, scan.last_uid)
        self.mail_store.save_cursor(
            account,
            folder,
            uid_validity=scan.uid_validity if scan.uid_validity is not None else known_validity,
            last_uid=last_safe_uid,
            first_scan_completed=True,
        )
        self.logger.info("mail_scan_completed", **report.as_dict())
        return report

    # ------------------------------------------------------------------
    # processing
    # ------------------------------------------------------------------
    def process_task(self, task_id: str, *, actor: str = "system") -> dict[str, Any]:
        return self._process_task(task_id, actor=actor, allow_completed=False, deliver=True)

    def _process_task(
        self,
        task_id: str,
        *,
        actor: str,
        allow_completed: bool,
        deliver: bool = True,
        explicit_reprocess: bool = False,
    ) -> dict[str, Any]:
        task = self.tasks.require(task_id)
        status = str(task["status"])
        if status in {state.CANCELLED, state.SEND_UNKNOWN}:
            raise StateTransitionError(f"Task {task_id} is {status}; resolve it before processing")
        if status == state.COMPLETED and not allow_completed:
            raise StateTransitionError(
                "Completed tasks require an explicit reprocess action creating a new version"
            )
        attempt_id = new_id("attempt")
        if status == state.RECEIVED:
            task = self.tasks.transition(
                task_id,
                state.VALIDATING,
                stage="validation",
                actor=actor,
                attempt_id=attempt_id,
            )
            status = str(task["status"])
        if status == state.VALIDATING:
            task = self.tasks.transition(
                task_id,
                state.PROCESSING,
                stage="processing",
                actor=actor,
                attempt_id=attempt_id,
                code="processing_started",
            )
        elif status in {
            state.WAITING_INPUT,
            state.RETRY_PENDING,
            state.FAILED,
            state.READY_TO_SEND,
            state.COMPLETED,
        }:
            if status == state.COMPLETED and not allow_completed:
                raise StateTransitionError("Completed task cannot be processed in place")
            task = self.tasks.transition(
                task_id,
                state.PROCESSING,
                stage="processing",
                actor=actor,
                attempt_id=attempt_id,
                code="processing_restarted",
            )
        else:
            raise StateTransitionError(f"Task {task_id} cannot start processing from {status}")
        self.tasks.clear_retry_state(task_id)
        version = int(task["version"])
        self.logger.info(
            "task_processing_started",
            task_id=task_id,
            version=version,
            attempt_id=attempt_id,
            kind=task["kind"],
        )

        if not task.get("destination_name"):
            issue = {
                "code": "DESTINATION_MISSING",
                "record_key": None,
                "field": "destination",
                "message": "No 目的国 was provided with the business mail.",
                "sources": [],
                "required_action": (
                    "Reply with 目的国：<known name> (for example 目的国：法国) "
                    "or 国家映射：<alias>=<alpha3>."
                ),
            }
            self.tasks.replace_issues(task_id, version, [issue])
            self.tasks.transition(
                task_id,
                state.WAITING_INPUT,
                stage="validation",
                actor=actor,
                attempt_id=attempt_id,
                code="destination_missing",
            )
            fresh = self.tasks.require(task_id)
            self._enqueue_question_and_dispatch(fresh, actor=actor)
            return fresh

        if task.get("destination_name") and not task.get("destination_alpha3"):
            issue = {
                "code": "DESTINATION_UNKNOWN",
                "record_key": None,
                "field": "destination",
                "message": f"目的国 '{task['destination_name']}' 未在配置映射中确认。",
                "sources": [],
                "required_action": "Reply with 目的国：<known name> or 国家映射：<alias>=<alpha3>.",
            }
            self.tasks.replace_issues(task_id, version, [issue])
            self.tasks.transition(
                task_id,
                state.WAITING_INPUT,
                stage="validation",
                actor=actor,
                attempt_id=attempt_id,
                code="destination_unknown",
            )
            fresh = self.tasks.require(task_id)
            self._enqueue_question_and_dispatch(fresh, actor=actor)
            return fresh

        try:
            request = self._build_request(task, version)
        except Exception as exc:
            self.logger.exception(
                "task_request_build_failed", exc, task_id=task_id, version=version
            )
            return self.tasks.set_retry_state(
                task_id,
                status=state.RETRY_PENDING,
                error=f"{type(exc).__name__}: {exc}",
                stage="processing",
                actor=actor,
                attempt_id=attempt_id,
                code="request_build_failed",
            )

        try:
            response_raw = self.engine.process_job(request)
        except CoreUnavailable as exc:
            self.logger.error(
                "core_unavailable", task_id=task_id, version=version, error=str(exc)
            )
            return self.tasks.set_retry_state(
                task_id,
                status=state.RETRY_PENDING,
                error=str(exc),
                stage="processing",
                actor=actor,
                attempt_id=attempt_id,
                code="core_unavailable",
            )
        except Exception as exc:
            self.logger.exception(
                "core_processing_failed",
                exc,
                task_id=task_id,
                version=version,
                attempt_id=attempt_id,
            )
            return self.tasks.set_retry_state(
                task_id,
                status=state.RETRY_PENDING,
                error=f"{type(exc).__name__}: {exc}",
                stage="processing",
                actor=actor,
                attempt_id=attempt_id,
                code="core_processing_error",
            )

        output_dir = self._output_dir(task_id, version)
        try:
            response = validate_core_response(response_raw, output_dir=output_dir)
        except Exception as exc:
            self.logger.exception(
                "core_response_invalid",
                exc,
                task_id=task_id,
                version=version,
                attempt_id=attempt_id,
            )
            return self.tasks.set_retry_state(
                task_id,
                status=state.RETRY_PENDING,
                error=f"invalid core response: {exc}",
                stage="validation",
                actor=actor,
                attempt_id=attempt_id,
                code="invalid_core_response",
            )

        self.tasks.set_kind(task_id, str(response["kind"]), actor=actor)
        task = self.tasks.require(task_id)
        self.tasks.update_result(
            task_id,
            version,
            content_hash=response.get("content_hash"),
            business_identity=response.get("business_identity"),
            template_hash=None,
            output_dir=str(output_dir),
            status=state.PROCESSING,
            record_count=len(response["records"]),
        )
        self.tasks.replace_issues(task_id, version, response["issues"])
        self.tasks.replace_provenance(task_id, version, response["provenance"])
        self.tasks.clear_artifacts(task_id, version)
        for artifact in response["artifacts"]:
            self.tasks.add_artifact(
                task_id,
                version,
                path=artifact["path"],
                filename=artifact["filename"],
                sha256=artifact["sha256"],
                size_bytes=artifact["size_bytes"],
                kind=artifact.get("kind"),
            )

        response_status = str(response["status"])
        if response_status == "waiting_input":
            self.tasks.transition(
                task_id,
                state.WAITING_INPUT,
                stage="validation",
                actor=actor,
                attempt_id=attempt_id,
                code="business_issues",
            )
            fresh = self.tasks.require(task_id)
            self.logger.info(
                "task_waiting_input",
                task_id=task_id,
                version=version,
                issue_count=len(response["issues"]),
            )
            self._enqueue_question_and_dispatch(fresh, actor=actor)
            return self.tasks.require(task_id)

        if response_status == "unsupported":
            issues = response["issues"] or [
                {
                    "code": "UNSUPPORTED_INPUT",
                    "record_key": None,
                    "field": None,
                    "message": "The supplied input format is not supported.",
                    "sources": [],
                    "required_action": "Supply an unencrypted xlsx workbook or supported mail table.",
                    "blocking": True,
                }
            ]
            self.tasks.replace_issues(task_id, version, issues)
            self.tasks.transition(
                task_id,
                state.WAITING_INPUT,
                stage="validation",
                actor=actor,
                attempt_id=attempt_id,
                code="unsupported_input",
            )
            fresh = self.tasks.require(task_id)
            self._enqueue_question_and_dispatch(fresh, actor=actor)
            return self.tasks.require(task_id)

        # ----- ready: duplicate and identity checks, then delivery --------
        content_hash = str(response["content_hash"])
        reprocess_override = explicit_reprocess or bool(task.get("reprocess_confirmed"))
        previous_same_task = self.tasks.find_previous_content_hash(
            task_id, version, content_hash
        )
        if (
            previous_same_task
            and not reprocess_override
            and previous_same_task["status"]
            in {
                state.COMPLETED,
                state.READY_TO_SEND,
                state.SEND_UNKNOWN,
            }
        ):
            self.tasks.transition(
                task_id,
                state.COMPLETED,
                stage="dedupe",
                actor=actor,
                attempt_id=attempt_id,
                code="duplicate_content_same_task",
                detail={
                    "content_hash": content_hash,
                    "previous_version": previous_same_task["version"],
                },
            )
            self.logger.info(
                "task_duplicate_content_same_task",
                task_id=task_id,
                version=version,
                previous_version=previous_same_task["version"],
            )
            return self.tasks.require(task_id)

        existing = self.tasks.find_by_content_hash(
            str(task["owner"]), str(task["kind"]), content_hash, exclude_task_id=task_id
        )
        if (
            existing
            and not reprocess_override
            and existing["status"]
            in {
                state.COMPLETED,
                state.READY_TO_SEND,
                state.SEND_UNKNOWN,
            }
        ):
            self.tasks.transition(
                task_id,
                state.COMPLETED,
                stage="dedupe",
                actor=actor,
                attempt_id=attempt_id,
                code="duplicate_content_existing_task",
                detail={"content_hash": content_hash, "existing_task_id": existing["id"]},
            )
            fresh = self.tasks.require(task_id)
            body = (
                f"{self._marker(fresh)} 已处理提示\n\n"
                "该邮件内容与已处理任务一致，本次未重复生成或发送业务文件。\n"
                f"已有任务编号：{existing['id']}\n"
                f"本次任务编号：{task_id}\n\n"
                "如需重新处理，请由管理员在管理页明确执行重新处理。\n"
            )
            try:
                notice, _ = self.delivery.enqueue_notice(
                    fresh,
                    body=body,
                    dedupe_suffix=f"duplicate:{existing['id']}:{content_hash}",
                )
                self._dispatch_if_pending(notice, actor=actor)
            except Exception as exc:
                self.logger.exception(
                    "duplicate_notice_failed", exc, task_id=task_id
                )
            self.logger.info(
                "task_duplicate_content_existing",
                task_id=task_id,
                existing_task_id=existing["id"],
            )
            return self.tasks.require(task_id)

        identity = response.get("business_identity")
        if identity:
            candidates = [
                item
                for item in self.tasks.find_by_business_identity(
                    str(task["owner"]), str(task["kind"]), str(identity), exclude_task_id=task_id
                )
                if item["status"]
                not in {state.FAILED, state.RETRY_PENDING, state.CANCELLED}
            ]
            if candidates and not task.get("reprocess_confirmed"):
                candidate = candidates[0]
                issue = {
                    "code": "REPROCESS_CONFIRMATION_REQUIRED",
                    "record_key": None,
                    "field": None,
                    "message": (
                        "同一票身份已有任务 "
                        f"{candidate['id']}，但本次内容发生变化。"
                        "未确认重新处理前不会覆盖或发送。"
                    ),
                    "sources": [
                        {"kind": "task", "task_id": candidate["id"]},
                    ],
                    "required_action": (
                        "管理员核对后确认重新处理；用户回复不会自动完成重新处理确认。"
                    ),
                    "blocking": True,
                }
                self.tasks.replace_issues(task_id, version, [issue])
                self.tasks.transition(
                    task_id,
                    state.WAITING_INPUT,
                    stage="dedupe",
                    actor=actor,
                    attempt_id=attempt_id,
                    code="reprocess_confirmation_required",
                    detail={"existing_task_id": candidate["id"]},
                )
                fresh = self.tasks.require(task_id)
                self._enqueue_question_and_dispatch(fresh, actor=actor)
                self.logger.warning(
                    "reprocess_confirmation_required",
                    task_id=task_id,
                    existing_task_id=candidate["id"],
                )
                return self.tasks.require(task_id)

        self.tasks.transition(
            task_id,
            state.READY_TO_SEND,
            stage="rendering",
            actor=actor,
            attempt_id=attempt_id,
            code="outputs_ready",
            detail={"artifacts": len(response["artifacts"]), "records": len(response["records"])},
        )
        fresh = self.tasks.require(task_id)
        if not deliver:
            self.tasks.add_event(
                task_id,
                stage="rendering",
                actor=actor,
                code="regenerate_only_ready",
                detail={"artifacts": len(response["artifacts"])},
            )
            self.logger.info("task_regenerated_only", task_id=task_id, version=version)
            return fresh
        artifacts = self.tasks.artifacts(task_id, version)
        try:
            delivery_row, _ = self.delivery.enqueue_delivery(
                fresh,
                record_count=len(response["records"]),
                artifacts=artifacts,
                content_hash=content_hash,
            )
        except Exception as exc:
            self.tasks.add_event(
                task_id,
                stage="delivery",
                code="delivery_enqueue_failed",
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )
            self.logger.exception(
                "delivery_enqueue_failed", exc, task_id=task_id, version=version
            )
            return self.tasks.require(task_id)
        self._dispatch_if_pending(delivery_row, actor=actor)
        self.logger.info(
            "task_ready",
            task_id=task_id,
            version=version,
            content_hash=content_hash,
            record_count=len(response["records"]),
            artifact_count=len(artifacts),
        )
        return self.tasks.require(task_id)

    # ------------------------------------------------------------------
    # processing helpers
    # ------------------------------------------------------------------
    def _version_dir(self, task_id: str, version: int) -> Path:
        return self.config.tasks_dir / task_id / f"v{version}"

    def _output_dir(self, task_id: str, version: int) -> Path:
        return self._version_dir(task_id, version) / "outputs"

    @staticmethod
    def _reference_scope(value: Any) -> str:
        text = str(value or "").strip().upper()
        return "" if text in {"", "DEFAULT", "ALL", "*"} else text

    def _normalize_reference_snapshot(
        self, references: dict[str, Any]
    ) -> dict[str, Any]:
        """Backfill special-rule confirmation on snapshots written before v5.

        Older task snapshots do not contain ``special_rule_confirmed``. Core
        defaults a missing value to True, which would silently confirm an
        unconfirmed special rule (for example SEM919BRZ for Brazil). Fill the
        field from the current reference table; when no current row exists,
        only the default scope is treated as confirmed.
        """

        machines = references.get("machines")
        if not isinstance(machines, list):
            return references
        missing = [
            entry
            for entry in machines
            if isinstance(entry, dict) and "special_rule_confirmed" not in entry
        ]
        if not missing:
            return references
        current = {
            (normalize_key(str(row["model"])), str(row["destination_scope"])): bool(
                row["special_rule_confirmed"]
            )
            for row in self.references.core_references()["machines"]
        }
        for entry in missing:
            scope = self._reference_scope(entry.get("destination_scope"))
            key = (normalize_key(str(entry.get("model") or "")), scope)
            entry["special_rule_confirmed"] = current.get(key, not scope)
        return references

    def _build_request(self, task: dict[str, Any], version: int) -> dict[str, Any]:
        task_id = str(task["id"])
        version_dir = self._version_dir(task_id, version)
        version_dir.mkdir(parents=True, exist_ok=True)
        output_dir = version_dir / "outputs"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        references_path = version_dir / "references.json"
        if references_path.exists():
            try:
                references = json.loads(references_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValidationError(f"Reference snapshot is unreadable: {exc}") from exc
            references = self._normalize_reference_snapshot(references)
        else:
            references = self.references.core_references()
            temporary = references_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(references, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, references_path)
            try:
                os.chmod(references_path, 0o600)
            except OSError:
                pass
            self.tasks.update_config_version(task_id, self.references.reference_version())

        mail_payload = {"html": "", "text": "", "message_id": None}
        origin_id = task.get("origin_mail_id")
        if origin_id:
            origin = self.mail_store.get(str(origin_id))
            if origin and origin.get("raw_path"):
                raw_path = Path(str(origin["raw_path"]))
                if raw_path.is_file():
                    parsed = parse_mail(raw_path.read_bytes())
                    mail_payload = {
                        "html": parsed.html_body,
                        "text": parsed.text_body,
                        "message_id": parsed.message_id,
                    }

        sources: list[dict[str, Any]] = []
        seen: set[Path] = set()
        inputs = self.tasks.inputs(task_id, version)
        for item in inputs:
            # Saved .msg/.eml files are audit evidence. Their body and workbook
            # attachments were already extracted during offline intake.
            if item.get("source_type") == "offline_original":
                continue
            path = Path(str(item["path"])).resolve()
            if path in seen:
                continue
            if not path.is_file():
                raise ValidationError(f"Task input is missing: {item['filename']}")
            seen.add(path)
            mime_type = item.get("mime_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            sources.append(
                {
                    "path": str(path),
                    "filename": str(item["filename"]),
                    "mime_type": mime_type,
                }
            )

        overrides = [
            {
                "record_key": item["record_key"],
                "field": item["field"],
                "value": item["value"],
                "source_message_id": item["source_message_id"],
            }
            for item in self.tasks.overrides(task_id)
        ]
        destination = None
        if task.get("destination_alpha3"):
            destination = {
                "name": task.get("destination_name") or task["destination_alpha3"],
                "alpha3": task["destination_alpha3"],
            }
        request = {
            "task_id": task_id,
            "kind": str(task["kind"]),
            "destination": destination,
            "sources": sources,
            "mail": mail_payload,
            "references": references,
            "output_dir": str(output_dir),
            "overrides": overrides,
        }
        request_path = version_dir / "request.json"
        temporary = request_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, request_path)
        try:
            os.chmod(request_path, 0o600)
        except OSError:
            pass
        self.logger.info(
            "task_request_built",
            task_id=task_id,
            version=version,
            kind=task["kind"],
            source_count=len(sources),
            override_count=len(overrides),
            destination_alpha3=task.get("destination_alpha3"),
        )
        return request

    def _enqueue_question_and_dispatch(
        self, task: dict[str, Any], *, actor: str
    ) -> dict[str, Any] | None:
        open_issues = [
            issue for issue in self.tasks.issues(str(task["id"])) if issue["status"] == "open"
        ]
        if not open_issues:
            return None
        try:
            row, _ = self.delivery.enqueue_question(task, open_issues)
        except Exception as exc:
            self.logger.exception(
                "question_enqueue_failed", exc, task_id=task["id"]
            )
            return None
        self._dispatch_if_pending(row, actor=actor)
        return row

    def _dispatch_if_pending(self, outbox_row: dict[str, Any], *, actor: str) -> None:
        if outbox_row.get("status") != "pending":
            return
        try:
            self.delivery.dispatch(str(outbox_row["id"]), actor=actor)
        except SendNotAllowedError as exc:
            self.logger.warning(
                "send_blocked_by_settings", outbox_id=outbox_row["id"], error=str(exc)
            )
        except Exception as exc:
            self.logger.exception(
                "outbox_auto_dispatch_failed", exc, outbox_id=outbox_row["id"]
            )

    def _marker(self, task: dict[str, Any]) -> str:
        return f"[CATER {task['id']} v{task['version']}]"

    # ------------------------------------------------------------------
    # admin actions
    # ------------------------------------------------------------------
    def retry_task(self, task_id: str, *, actor: str = "admin") -> dict[str, Any]:
        task = self.tasks.require(task_id)
        if task["status"] not in {state.RETRY_PENDING, state.FAILED}:
            raise StateTransitionError("Only RETRY_PENDING or FAILED tasks can be retried")
        return self.process_task(task_id, actor=actor)

    def reprocess_task(
        self, task_id: str, *, actor: str = "admin", regenerate_only: bool = False
    ) -> dict[str, Any]:
        task = self.tasks.require(task_id)
        if task["status"] in {state.PROCESSING, state.VALIDATING, state.SEND_UNKNOWN}:
            raise StateTransitionError(
                f"Task {task_id} is {task['status']}; resolve it before reprocessing"
            )
        old_status = str(task["status"])
        task = self.tasks.start_next_version(
            task_id,
            actor=actor,
            reason="admin reprocess" + (" (regenerate only)" if regenerate_only else ""),
            copy_inputs=True,
        )
        # A regenerate-only run creates a new version and outputs but leaves the
        # outbox untouched; delivering requires a separate explicit action.
        result = self._process_task(
            task_id,
            actor=actor,
            allow_completed=old_status == state.COMPLETED,
            deliver=not regenerate_only,
            explicit_reprocess=True,
        )
        if regenerate_only:
            self.tasks.add_event(
                task_id,
                stage="admin",
                actor=actor,
                code="regenerate_only",
                detail={"status": result["status"]},
            )
        return result

    def confirm_reprocess(self, task_id: str, *, actor: str = "admin") -> dict[str, Any]:
        task = self.tasks.require(task_id)
        if not any(
            issue["code"] == "REPROCESS_CONFIRMATION_REQUIRED" and issue["status"] == "open"
            for issue in self.tasks.issues(task_id)
        ):
            raise ValidationError("Task has no open reprocess confirmation issue")
        self.tasks.confirm_reprocess(task_id, actor=actor, reason="admin confirmation")
        return self._process_task(
            task_id,
            actor=actor,
            allow_completed=False,
            deliver=True,
            explicit_reprocess=True,
        )

    def deliver_task(self, task_id: str, *, actor: str = "admin") -> dict[str, Any]:
        """Create and dispatch a delivery for the current ready version.

        Used after an explicit "regenerate only" admin run, which deliberately
        leaves the outbox untouched.
        """

        task = self.tasks.require(task_id)
        if task["status"] != state.READY_TO_SEND:
            raise StateTransitionError(
                "Only READY_TO_SEND tasks can be delivered; reprocess or resolve first"
            )
        version = int(task["version"])
        artifacts = self.tasks.artifacts(task_id, version)
        if not artifacts:
            raise ValidationError("The current task version has no output artifacts")
        version_row = self.tasks.get_version(task_id, version) or {}
        row, _ = self.delivery.enqueue_delivery(
            task,
            record_count=int(version_row.get("record_count") or 0),
            artifacts=artifacts,
            content_hash=task.get("content_hash"),
        )
        self._dispatch_if_pending(row, actor=actor)
        self.tasks.add_event(
            task_id,
            stage="admin",
            actor=actor,
            code="delivery_requested",
            detail={"outbox_id": row["id"], "version": version},
        )
        return self.tasks.require(task_id)

    def retry_pending_tasks(self, *, limit: int = 10, actor: str = "admin") -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for task_id in self.tasks.due_for_retry(limit=limit):
            try:
                results.append(self.process_task(task_id, actor=actor))
            except Exception as exc:
                self.logger.exception("retry_pending_failed", exc, task_id=task_id)
        return results

    def send_pending(self, *, limit: int = 20, actor: str = "admin") -> list[dict[str, Any]]:
        return self.delivery.send_due(limit=limit)

    def release_dry_run(self, outbox_id: str, *, actor: str = "admin") -> dict[str, Any]:
        return self.delivery.release_dry_run(outbox_id, actor=actor)

    def recheck_unknown(self, outbox_id: str, *, actor: str = "admin") -> bool:
        return self.delivery.recheck_unknown(outbox_id, actor=actor)

    def confirm_unknown_sent(self, outbox_id: str, *, actor: str = "admin") -> dict[str, Any]:
        return self.delivery.confirm_unknown_sent(outbox_id, actor=actor)

    def confirm_unknown_not_sent(
        self, outbox_id: str, *, actor: str = "admin"
    ) -> dict[str, Any]:
        return self.delivery.confirm_unknown_not_sent(outbox_id, actor=actor)

    # ------------------------------------------------------------------
    # offline submission
    # ------------------------------------------------------------------
    def _assert_sender_allowed(self, sender: str) -> str:
        normalized = normalize_email(sender)
        if not normalized or not EMAIL_RE.match(normalized):
            raise ValidationError("A valid sender e-mail address is required")
        if not self.settings.current().allowlist.contains(normalized):
            raise ValidationError(
                "Sender is not in the active test/production allowlist; offline processing "
                "uses the same allowlist isolation as polling"
            )
        return normalized

    def _next_offline_uid(self, account: str) -> int:
        value = self.db.scalar(
            "SELECT COALESCE(MAX(uid), 0) FROM mail_messages WHERE account = ? AND folder = 'OFFLINE'",
            (account,),
        )
        return int(value or 0) + 1

    def _store_synthetic_mail(
        self,
        *,
        sender: str,
        sender_name: str | None,
        subject: str,
        body: str,
        html_body: str = "",
    ) -> tuple[dict[str, Any], Any]:
        account = self.config.mail.account or "offline@localhost"
        message = EmailMessage()
        message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
        message["To"] = account
        message["Subject"] = subject
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = f"offline.{int(time.time() * 1_000_000)}.{os.getpid()}@cater.local"
        message.set_content(body or "")
        if html_body.strip():
            message.add_alternative(html_body, subtype="html")
        raw = message.as_bytes()
        parsed = parse_mail(raw)
        raw_hash = sha256_bytes(raw)
        raw_path = self.mail_store.save_raw(raw, raw_hash)
        mail, _ = self.mail_store.record(
            account=account,
            folder="OFFLINE",
            uid_validity=0,
            uid=self._next_offline_uid(account),
            raw_hash=raw_hash,
            parsed=parsed,
            raw_path=str(raw_path),
            size_bytes=len(raw),
        )
        return mail, parsed

    def process_eml_file(
        self, path: str | Path, *, actor: str = "admin"
    ) -> IngestResult:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        raw = source.read_bytes()
        account = self.config.mail.account or "offline@localhost"
        raw_message = RawMessage(
            account=account,
            folder="OFFLINE",
            uid_validity=0,
            uid=self._next_offline_uid(account),
            raw=raw,
            size=len(raw),
        )
        return self.ingest(raw_message)

    def _expand_offline_sources(
        self, source_paths: list[str | Path]
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """Read direct workbooks plus saved ``.msg``/``.eml`` mails.

        A saved mail contributes its body and workbook attachments. The original
        message is retained for audit but never handed to the business core,
        whose contract accepts workbook sources only.
        """

        text_bodies: list[str] = []
        html_bodies: list[str] = []
        expanded: list[dict[str, Any]] = []
        for raw_path in source_paths:
            path = Path(raw_path)
            if not path.is_file():
                raise FileNotFoundError(path)
            suffix = path.suffix.lower().lstrip(".")
            if suffix in {"msg", "eml"}:
                try:
                    if suffix == "msg":
                        parsed = parse_msg(
                            path,
                            max_attachment_bytes=self.config.mail.max_attachment_bytes,
                        )
                    else:
                        parsed = parse_mail(
                            path.read_bytes(),
                            max_attachment_bytes=self.config.mail.max_attachment_bytes,
                        )
                except MsgParseError as exc:
                    raise ValidationError(str(exc)) from exc
                body = parsed.body_text.strip()
                if body:
                    text_bodies.append(body)
                if parsed.html_body.strip():
                    html_bodies.append(parsed.html_body)
                expanded.append(
                    {
                        "data": path.read_bytes(),
                        "filename": safe_filename(path.name),
                        "mime_type": MSG_MIME if suffix == "msg" else "message/rfc822",
                        "source_type": "offline_original",
                    }
                )
                total = 0
                for attachment in parsed.attachments:
                    if attachment.suffix not in {"xlsx", "xlsm"}:
                        continue
                    total += attachment.size
                    if total > self.config.mail.max_total_attachment_bytes:
                        raise ValidationError(
                            f"Attachments in {path.name} exceed the configured total limit"
                        )
                    expanded.append(
                        {
                            "data": attachment.data,
                            "filename": attachment.filename,
                            "mime_type": XLSX_MIME,
                            "source_type": "offline_attachment",
                        }
                    )
                continue
            if suffix not in {"xlsx", "xlsm"}:
                raise ValidationError(f"Unsupported offline source type: {path.name}")
            expanded.append(
                {
                    "data": path.read_bytes(),
                    "filename": safe_filename(path.name),
                    "mime_type": XLSX_MIME,
                    "source_type": "offline_source",
                }
            )
        return (
            {"text": "\n\n".join(text_bodies), "html": "\n".join(html_bodies)},
            expanded,
        )

    def submit_offline_sources(
        self,
        *,
        sender: str,
        sender_name: str | None = None,
        kind: str = "auto",
        destination: str | None = None,
        source_paths: list[str | Path] | None = None,
        subject: str | None = None,
        body: str = "",
        actor: str = "cli",
    ) -> dict[str, Any]:
        if kind not in {"auto", "parts", "machine"}:
            raise ValidationError("kind must be auto, parts or machine")
        normalized_sender = self._assert_sender_allowed(sender)
        resolved = self.references.resolve(destination) if destination else None
        destination_name = resolved["name_zh"] if resolved else destination
        destination_alpha3 = resolved["alpha3"] if resolved else None
        extracted, expanded_sources = self._expand_offline_sources(
            source_paths or []
        )
        html_body = ""
        if not body.strip():
            body = extracted["text"]
            html_body = extracted["html"]
        mail, _ = self._store_synthetic_mail(
            sender=normalized_sender,
            sender_name=sender_name,
            subject=subject or "离线任务",
            body=body,
            html_body=html_body,
        )
        task = self.tasks.create(
            owner=normalized_sender,
            owner_name=sender_name,
            kind=kind,
            origin_mail_id=str(mail["id"]),
            destination_name=destination_name,
            destination_alpha3=destination_alpha3,
        )
        self.mail_store.set_task(str(mail["id"]), str(task["id"]))
        directory = self._version_dir(str(task["id"]), 1) / "inputs"
        directory.mkdir(parents=True, exist_ok=True)
        for source in expanded_sources:
            data = source["data"]
            digest = sha256_bytes(data)
            filename = safe_filename(str(source["filename"]))
            target = directory / f"{digest[:16]}_{filename}"
            if not target.exists():
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_bytes(data)
                os.replace(temporary, target)
            self.tasks.add_input(
                task_id=str(task["id"]),
                version=1,
                mail_id=str(mail["id"]),
                file_hash=digest,
                path=str(target),
                filename=filename,
                source_type=str(source["source_type"]),
                mime_type=str(source["mime_type"]),
                size_bytes=len(data),
            )
        self.logger.info(
            "offline_task_created",
            task_id=task["id"],
            mail_id=mail["id"],
            kind=kind,
            source_count=len(source_paths or []),
        )
        if destination and resolved is None:
            issue = {
                "code": "DESTINATION_UNKNOWN",
                "record_key": None,
                "field": "destination",
                "message": f"目的国 '{destination}' is not configured.",
                "sources": [],
                "required_action": "Use a configured country or add an explicit country mapping.",
            }
            self.tasks.replace_issues(str(task["id"]), 1, [issue])
            self.tasks.transition(
                str(task["id"]),
                state.VALIDATING,
                stage="validation",
                actor=actor,
            )
            self.tasks.transition(
                str(task["id"]),
                state.WAITING_INPUT,
                stage="validation",
                actor=actor,
                code="destination_unknown",
            )
            fresh = self.tasks.require(str(task["id"]))
            self._enqueue_question_and_dispatch(fresh, actor=actor)
            return self.tasks.require(str(task["id"]))
        return self.process_task(str(task["id"]), actor=actor)