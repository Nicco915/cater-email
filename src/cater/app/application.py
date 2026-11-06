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



def _xls_dependency_installed() -> bool:
    """运行环境是否具备 .xls 解析依赖（xlrd）。布尔值，不暴露环境细节。"""
    import importlib.util

    return importlib.util.find_spec("xlrd") is not None


def _semantic_tables(layout: dict) -> dict:
    """按语义角色归并结构表（不按 sheet 名）：sheet 重命名、增删表
    都会被差异对比发现。"""
    roles: dict[str, dict] = {}
    for key in ("parts_invoice", "packing_total"):
        t = layout.get(key)
        if isinstance(t, dict) and t.get("sheet"):
            roles[key] = t
    for key in ("machine_table", "weight_table"):
        for index, t in enumerate(layout.get(key) or []):
            if isinstance(t, dict) and t.get("sheet"):
                roles[f"{key}#{index}"] = t
    return roles


_ROLE_LABELS = {
    "parts_invoice": "最终发票表",
    "packing_total": "箱单TOTAL",
}


def _role_label(role: str) -> str:
    if role in _ROLE_LABELS:
        return _ROLE_LABELS[role]
    for key, label in (("machine_table#", "机器信息表"),
                       ("weight_table#", "箱单重量表")):
        if role.startswith(key):
            suffix = role[len(key):]
            return f"{label}（第{int(suffix) + 1}张）" if suffix.isdigit() else label
    return role


_FIELD_LABELS = {
    "part_code": "料号", "qty": "数量", "unit_price": "单价",
    "coo": "原产国", "unit_weight": "单件重", "net_weight": "总净重",
    "hs_code_titled": "表头HS",
    "packages": "件数", "gross_weight": "总毛重",
    "model": "机型", "mso": "MSO", "serial": "序列号", "pin": "PIN",
    "engine": "发动机号",
    "description": "品名", "net": "净重", "gross": "毛重",
}


def _table_column_lines(layout: dict) -> list[str]:
    """本版采用表的字段列位行（不依赖差异对比/基线）：首次使用与已确认
    布局都列出关键字段取自哪张表、哪一行表头、哪一列，含次表头（双行
    表头）取值说明，使数量/单价/重量来源可追溯。"""
    lines: list[str] = []
    for role, table in _semantic_tables(layout).items():
        columns = table.get("columns") or {}
        if not isinstance(columns, dict) or not columns:
            continue
        parts = [f"{_FIELD_LABELS.get(name, name)}→{col}"
                 for name, col in columns.items()]
        text = f"{_role_label(role)}（工作表 {table.get('sheet')}）：" \
               f"表头第{table.get('header_row')}行；" + "、".join(parts)
        sub = table.get("subheader_row")
        if sub:
            text += f"；单件重/总净重取自第{sub}行次表头"
        supp = table.get("supplementary") or {}
        if isinstance(supp, dict) and supp.get("hs_code"):
            text += f"；补充HS列→{supp['hs_code']}"
        lines.append(text)
    return lines


def _diff_layout_structure(baseline: dict, current: dict) -> list[str]:
    """对比当前布局与已确认基准布局，列出结构变化（不含数值/正文）。

    表按语义角色配对（parts_invoice↔parts_invoice、machine_table#0↔
    machine_table#0……），不按 sheet 名配对：sheet 重命名体现为
    “工作表 X→Y”，角色缺失/新增体现为“新增/移除表”；
    只报告字段定位差异（sheet、表头行、列位），供成功邮件提示人工重点核对。
    """
    changes: list[str] = []
    FIELD_LABELS = {
        "part_code": "料号", "qty": "数量", "unit_price": "单价",
        "unit_weight": "单件重", "net_weight": "总净重", "coo": "原产国",
        "hs_code_titled": "表头HS编码", "hs_code": "商品编码",
        "product_name": "商品名称", "spec": "规格型号",
        "packages": "件数", "gross_weight": "毛重",
        "model": "机型", "mso": "MSO", "serial": "序列号",
        "pin": "PIN", "engine": "发动机号",
        "description": "描述", "net": "净重", "gross": "毛重(箱单)",
    }

    def diff_table(name: str, base: dict, cur: dict) -> None:
        if base.get("sheet") != cur.get("sheet"):
            changes.append(f"{name}：工作表 {base.get('sheet')}→{cur.get('sheet')}（重命名/换表）")
        for row_key, label in (("header_row", "表头行"), ("subheader_row", "次表头行"),
                               ("total_row", "TOTAL行"),
                               ("data_first_row", "数据起始行"), ("data_last_row", "数据结束行")):
            b, c = base.get(row_key), cur.get(row_key)
            if b is not None and c is not None and b != c:
                changes.append(f"{name}：{label} {b}→{c}")
        base_cols = dict(base.get("columns") or {})
        cur_cols = dict(cur.get("columns") or {})
        for field in sorted(set(base_cols) | set(cur_cols)):
            b, c = base_cols.get(field), cur_cols.get(field)
            label = FIELD_LABELS.get(field, field)
            if b != c:
                changes.append(f"{name}·{label}：{b or '无'}→{c or '无'}")
        base_supp = dict(base.get("supplementary") or {})
        cur_supp = dict(cur.get("supplementary") or {})
        for field in sorted(set(base_supp) | set(cur_supp)):
            b, c = base_supp.get(field), cur_supp.get(field)
            label = FIELD_LABELS.get(field, field)
            if b != c:
                changes.append(f"{name}·补充列{label}：{b or '无'}→{c or '无'}")

    base_roles = _semantic_tables(baseline or {})
    cur_roles = _semantic_tables(current or {})
    for role in sorted(set(base_roles) | set(cur_roles)):
        label = _role_label(role)
        base, cur = base_roles.get(role), cur_roles.get(role)
        if base is None:
            changes.append(f"新增{label}（工作表 {cur.get('sheet')}）")
        elif cur is None:
            changes.append(f"移除{label}（原工作表 {base.get('sheet')}）")
        else:
            diff_table(label, base, cur)
    # 去重保序
    seen: set[str] = set()
    unique = []
    for item in changes:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


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
        self.references = ReferenceService(
            self.db, self.logger, tasks_dir=config.tasks_dir
        )
        self.references.ensure_seeded()
        self.settings = SettingsService(self.db, self.logger, config)
        self.record_environment_identity()
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
        # Global, task-less poll failures (for example IMAP down) surface only
        # here and in the logs; they never trigger customer-facing mail.
        self.last_poll_error: str | None = None
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
    def _require_active(self, task: dict[str, Any], action: str) -> None:
        """Refuse processing-style actions on a trashed (recycle-bin) task.

        Archive is display-only and never blocks anything; only deletion does.
        Restoring a task is always allowed and is the explicit way back.
        """

        if task.get("deleted_at") is not None:
            raise StateTransitionError(
                f"任务 {task['id']} 已删除到回收站，不能{action}；请先从回收站恢复"
            )
    def recover(self) -> dict[str, Any]:
        interrupted_tasks = self.tasks.recover_incomplete()
        uncertain_outbox = self.outbox.recover_sending()
        send_unknown_tasks: list[str] = []
        for row in uncertain_outbox:
            if row["task_id"]:
                self.tasks.add_event(
                    str(row["task_id"]),
                    stage="recovery",
                    code="send_uncertain_after_restart",
                    detail={"outbox_id": row["id"]},
                )
            if row["purpose"] == "delivery" and row["task_id"]:
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
        layout = self.tasks.layout(task_id, selected_version)
        layout_confirmed = False
        if layout and layout.get("fingerprint"):
            layout_confirmed = bool(self.tasks.find_confirmed_layout(
                str(layout.get("kind") or task.get("kind") or ""),
                str(layout.get("fingerprint"))))
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
            "layout": layout,
            "layout_confirmed": layout_confirmed,
        }

    def health(self) -> dict[str, Any]:
        effective = self.settings.current()
        credentials = self.credentials.describe(self.config.mail.account)
        task_counts = self.tasks.counts()
        return {
            "schema_version": self.db.schema_version(),
            "quick_check": self.db.quick_check(),
            "tasks": task_counts,
            "outbox_unknown": len(self.outbox.unknown()),
            "retry_pending": task_counts.get(state.RETRY_PENDING, 0),
            "failed": task_counts.get(state.FAILED, 0),
            "last_poll_error": self.last_poll_error,
            "dry_run": effective.sending.dry_run,
            "sending_enabled": effective.sending.enabled,
            "allowlist_mode": effective.allowlist.mode,
            "active_allowlist_count": len(effective.allowlist.active()),
            "imap_host_configured": bool(self.config.mail.imap_host),
            "smtp_host_configured": bool(self.config.mail.smtp_host),
            "account_configured": bool(self.config.mail.account),
            # 显式依赖检查：.xls 解析需要 xlrd；缺失时 health 可见，
            # 避免 .xls 附件被误报为“缺少附件”（部署问题 ≠ 输入缺失）。
            "xls_dependency_installed": _xls_dependency_installed(),
            # Booleans only: never render secret values.
            "credential_presence": credentials,
        }

    # ------------------------------------------------------------------
    # ingestion / polling
    # ------------------------------------------------------------------
    def ingest(self, raw_message: RawMessage) -> IngestResult:
        result = self.intake.ingest(raw_message)
        if result.reprocess_requested and result.task_id:
            self.reprocess_task(result.task_id, actor="mail-owner")
        elif result.retry_command and result.task_id:
            self._mail_retry(result.task_id)
        elif result.should_process and result.task_id:
            self.process_task(result.task_id, actor="intake")
            task = self.tasks.require(result.task_id)
            if task["status"] == state.WAITING_INPUT:
                result.waiting = True
        if result.status_query and result.task_id:
            self._answer_status_query(result.task_id, result.mail_id)
        if result.retry_waiting and result.task_id:
            self._answer_retry_waiting(result.task_id, result.mail_id)
        if result.needs_question and result.task_id:
            task = self.tasks.require(result.task_id)
            self._enqueue_question_and_dispatch(task, actor="intake")
        if result.notice and result.task_id:
            self._send_guidance_notice(result)
        if result.guidance:
            self._send_taskless_guidance(result)
        return result

    def _send_taskless_guidance(self, result: IngestResult) -> None:
        """Persist generic guidance for a mail that links to no task.

        Uses the same outbox/dry-run/allowlist pipeline as task mail; the
        dedupe key is the querying mail's fingerprint, so redelivery never
        triggers a second guidance message.
        """

        mail = self.mail_store.get(result.mail_id)
        if mail is None:
            return
        recipient = normalize_email(str(mail.get("sender") or ""))
        fingerprint = str(mail.get("raw_hash") or mail.get("message_id") or result.mail_id)
        message_id = str(mail.get("message_id") or "")
        try:
            row, _ = self.delivery.enqueue_guidance(
                recipient=recipient,
                recipient_name=mail.get("sender_name"),
                subject=f"Re: {str(mail.get('subject') or '操作指引')}",
                body=str(result.guidance),
                in_reply_to=message_id or None,
                references=[message_id] if message_id else [],
                dedupe_key=f"guidance:{fingerprint}",
            )
        except Exception as exc:
            self.logger.exception("taskless_guidance_failed", exc, mail_id=result.mail_id)
            return
        self._dispatch_if_pending(row, actor="intake")

    def _answer_retry_waiting(self, task_id: str, mail_id: str) -> None:
        task = self.tasks.require(task_id)
        open_issues = [
            issue for issue in self.tasks.issues(task_id) if issue["status"] == "open"
        ]
        try:
            row, _ = self.delivery.enqueue_retry_waiting_answer(
                task, command_mail_id=mail_id, open_issues=open_issues
            )
        except Exception as exc:
            self.logger.exception("retry_waiting_answer_failed", exc, task_id=task_id)
            return
        self._dispatch_if_pending(row, actor="intake")

    def _mail_retry(self, task_id: str) -> dict[str, Any]:
        """Owner-initiated retry: same version, same inputs, new failure episode."""

        self.tasks.reset_retry_budget(task_id)
        return self.process_task(task_id, actor="mail-owner")

    def _answer_status_query(self, task_id: str, mail_id: str) -> None:
        """Read-only status answer; never recalculates or changes the task."""

        task = self.tasks.require(task_id)
        if task.get("deleted_at") is not None:
            # A trashed task's status query is answered without revealing the
            # task's prior state or any other task; the owner is told it can be
            # recovered by asking maintenance.
            body = (
                f"{self._marker(task)} 任务状态查询\n\n"
                "该任务已从列表删除并进入回收站，未发送任何新的结果或通知。\n"
                "如需恢复处理，请联系维护人员从回收站恢复后再操作。\n"
            )
            mail = self.mail_store.get(mail_id)
            recipient = normalize_email(str(mail.get("sender") or "")) if mail else None
            if not recipient:
                return
            try:
                row, _ = self.delivery.enqueue_guidance(
                    recipient=recipient,
                    recipient_name=mail.get("sender_name") if mail else None,
                    subject=f"Re: {str(mail.get('subject') or '任务状态查询') if mail else '任务状态查询'}",
                    body=body,
                    in_reply_to=str(mail.get("message_id") or "") if mail else None,
                    references=[str(mail["message_id"])] if mail and mail.get("message_id") else [],
                    dedupe_key=f"status_deleted:{task_id}:{mail_id}",
                )
                self._dispatch_if_pending(row, actor="intake")
            except Exception as exc:
                self.logger.exception(
                    "status_query_deleted_failed", exc, task_id=task_id
                )
            return
        open_issues = [
            issue for issue in self.tasks.issues(task_id) if issue["status"] == "open"
        ]
        try:
            row, _ = self.delivery.enqueue_status_report(
                task, query_mail_id=mail_id, open_issues=open_issues
            )
        except Exception as exc:
            self.logger.exception("status_report_failed", exc, task_id=task_id)
            return
        self._dispatch_if_pending(row, actor="intake")

    def _send_guidance_notice(self, result: IngestResult) -> None:
        task = self.tasks.require(str(result.task_id))
        try:
            row, _ = self.delivery.enqueue_notice(
                task,
                body=str(result.notice),
                dedupe_suffix=str(result.notice_suffix or "guidance"),
            )
        except Exception as exc:
            self.logger.exception(
                "guidance_notice_failed", exc, task_id=result.task_id
            )
            return
        self._dispatch_if_pending(row, actor="intake")

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
        self.last_poll_error = "; ".join(report.errors) or None
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
        self._require_active(task, "处理")
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
            self.tasks.reset_retry_budget(task_id)
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
            self.tasks.reset_retry_budget(task_id)
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
            # Unreadable/missing saved inputs do not heal by waiting; stop
            # instead of burning the retry budget, and tell the owner once.
            return self._record_processing_failure(
                task_id,
                error=f"{type(exc).__name__}: {exc}",
                stage="processing",
                actor=actor,
                attempt_id=attempt_id,
                code="request_build_failed",
                recoverable=False,
            )

        try:
            response_raw = self.engine.process_job(request)
        except CoreUnavailable as exc:
            self.logger.error(
                "core_unavailable", task_id=task_id, version=version, error=str(exc)
            )
            return self._record_processing_failure(
                task_id,
                error=str(exc),
                stage="processing",
                actor=actor,
                attempt_id=attempt_id,
                code="core_unavailable",
                recoverable=True,
            )
        except Exception as exc:
            self.logger.exception(
                "core_processing_failed",
                exc,
                task_id=task_id,
                version=version,
                attempt_id=attempt_id,
            )
            return self._record_processing_failure(
                task_id,
                error=f"{type(exc).__name__}: {exc}",
                stage="processing",
                actor=actor,
                attempt_id=attempt_id,
                code="core_processing_error",
                recoverable=True,
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
            return self._record_processing_failure(
                task_id,
                error=f"invalid core response: {exc}",
                stage="validation",
                actor=actor,
                attempt_id=attempt_id,
                code="invalid_core_response",
                recoverable=True,
            )

        self.tasks.set_kind(task_id, str(response["kind"]), actor=actor)
        task = self.tasks.require(task_id)
        layout = response.get("layout") or {}
        self.tasks.update_result(
            task_id,
            version,
            content_hash=response.get("content_hash"),
            business_identity=response.get("business_identity"),
            template_hash=None,
            output_dir=str(output_dir),
            status=state.PROCESSING,
            record_count=len(response["records"]),
            layout=layout or None,
        )
        if layout:
            fingerprint = str(layout.get("fingerprint") or "")
            confirmed = bool(
                fingerprint
                and self.tasks.find_confirmed_layout(str(response["kind"]), fingerprint)
            )
            # 仅记录结构指纹、采用工作表与规则ID；不记录正文或秘密。
            self.tasks.add_event(
                task_id,
                stage="validation",
                code="layout_seen",
                actor=actor,
                detail={
                    "kind": response["kind"],
                    "fingerprint": fingerprint,
                    "confirmed_layout": confirmed,
                    "sheets": layout.get("sheets") or [],
                    "checks": [c.get("rule") for c in layout.get("checks", []) if isinstance(c, dict)],
                },
            )
            self.logger.info(
                "task_layout_seen",
                task_id=task_id,
                version=version,
                kind=response["kind"],
                fingerprint=fingerprint,
                confirmed_layout=confirmed,
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
            self.tasks.reset_retry_budget(task_id)
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
            self.tasks.reset_retry_budget(task_id)
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
            self.tasks.reset_retry_budget(task_id)
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
            self.tasks.reset_retry_budget(task_id)
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
                "如需重新处理，请由原发起人直接回复本邮件，新增正文仅写：确认重新处理。\n"
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
            if candidates and not reprocess_override:
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
                        "请由原发起人回复本邮件，新增正文仅写：确认重新处理。"
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

        self.tasks.reset_retry_budget(task_id)
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
        layout_report = self._layout_report(fresh, response, request)
        try:
            delivery_row, _ = self.delivery.enqueue_delivery(
                fresh,
                record_count=len(response["records"]),
                artifacts=artifacts,
                content_hash=content_hash,
                layout_report=layout_report,
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

    def _record_processing_failure(
        self,
        task_id: str,
        *,
        error: str,
        stage: str,
        actor: str,
        attempt_id: str,
        code: str,
        recoverable: bool,
    ) -> dict[str, Any]:
        """Persist a technical failure, schedule bounded retry and notify once.

        Recoverable failures consume the automatic retry budget with backoff;
        once it is exhausted the task becomes FAILED. The owner receives one
        persisted notice per failure episode (an explicit user/admin retry
        starts a new episode via ``reset_retry_budget``). Non-recoverable
        failures (for example unreadable saved inputs) stop immediately.
        """

        status = state.RETRY_PENDING if recoverable else state.FAILED
        task = self.tasks.set_retry_state(
            task_id,
            status=status,
            error=error,
            stage=stage,
            actor=actor,
            attempt_id=attempt_id,
            code=code,
            count_attempt=recoverable,
        )
        if task["status"] == state.FAILED and not int(task.get("failure_notified") or 0):
            try:
                notice, _ = self.delivery.enqueue_failure_notice(
                    task,
                    episode=int(task.get("failure_seq") or 0) + 1,
                    exhausted=recoverable,
                )
                self.tasks.mark_failure_notified(task_id)
                self._dispatch_if_pending(notice, actor=actor)
                self.logger.warning(
                    "task_failure_notified",
                    task_id=task_id,
                    version=int(task["version"]),
                    code=code,
                )
            except Exception as exc:
                self.logger.exception(
                    "failure_notice_failed", exc, task_id=task_id
                )
        return self.tasks.require(task_id)

    # ------------------------------------------------------------------
    # processing helpers
    # ------------------------------------------------------------------
    def _version_dir(self, task_id: str, version: int) -> Path:
        return (self.config.tasks_dir / task_id / f"v{version}").resolve()

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
        # 表头别名随请求快照持久化：同版本重试不依赖当前配置状态；
        # 别名是解析配置而非秘密，进入版本快照便于追溯。
        if self.config.core.header_aliases:
            request["header_aliases"] = self.config.core.header_aliases
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
        self._require_active(task, "重试")
        if task["status"] not in {state.RETRY_PENDING, state.FAILED}:
            raise StateTransitionError("Only RETRY_PENDING or FAILED tasks can be retried")
        self.tasks.reset_retry_budget(task_id)
        return self.process_task(task_id, actor=actor)

    # ------------------------------------------------------------------
    # 表格布局确认（显式人工动作；基准为已确认布局）
    # ------------------------------------------------------------------
    def _layout_report(self, task: dict[str, Any], response: dict,
                       request: dict | None) -> dict | None:
        """汇总回复正文用的布局信息：采用 sheet、结构变化、已执行核对。

        首次使用（未确认）的布局给出人工重点核对建议；已确认布局不重复
        告警。结构变化与实际修正分别列出。不替代暂停：阻塞问题仍走追问。
        """
        layout = (response or {}).get("layout") or {}
        if not layout:
            return None
        kind = str(response.get("kind") or task.get("kind") or "")
        fingerprint = str(layout.get("fingerprint") or "")
        confirmed = bool(
            fingerprint and self.tasks.find_confirmed_layout(kind, fingerprint)
        )
        report: dict[str, Any] = {
            "kind": kind,
            "fingerprint": fingerprint,
            "confirmed": confirmed,
            "first_use": not confirmed,
            "sheets": layout.get("sheets") or [],
            "structure_changes": [],
            "checks": [c for c in (layout.get("checks") or []) if isinstance(c, dict)],
        }
        baseline_row = self.tasks.latest_confirmed_layout(kind)
        if baseline_row and not confirmed:
            try:
                baseline = json.loads(baseline_row["structure_json"])
            except (TypeError, json.JSONDecodeError):
                baseline = {}
            report["structure_changes"] = _diff_layout_structure(
                baseline, layout)
        corrections = []
        for item in (request or {}).get("overrides") or []:
            key = item.get("record_key")
            field = item.get("field")
            if key and field:
                corrections.append(f"{key}.{field}={item.get('value')}")
        # 自动窄规范化（如规格型号 1|| → 1|）：与实际值、来源一并列出，
        # 不得只报告按回复执行的修正（自动修正在交付正文同样可见）。
        automatic = [c for c in (layout.get("corrections") or [])
                     if isinstance(c, dict) and c.get("summary")]
        report["corrections"] = (
            [str(c["summary"]) for c in automatic] + corrections
        )
        report["corrections_auto"] = bool(automatic)
        report["table_columns"] = _table_column_lines(layout)
        return report

    def confirm_layout(self, task_id: str, *, actor: str,
                       version: int | None = None) -> dict[str, Any]:
        """把任务某版本采用的布局显式确认为该类业务的基准布局。"""
        task = self.tasks.require(task_id)
        version = int(version or task["version"])
        layout = self.tasks.layout(task_id, version)
        if not layout:
            raise ValidationError(
                f"Task {task_id} v{version} has no stored layout to confirm")
        kind = str(layout.get("kind") or task.get("kind") or "")
        fingerprint = str(layout.get("fingerprint") or "")
        if not kind or not fingerprint:
            raise ValidationError("Stored layout is missing kind/fingerprint")
        row = self.tasks.confirm_layout(
            kind, fingerprint,
            {k: layout.get(k) for k in
             ("sheets", "structure", "parts_invoice", "packing_total",
              "machine_table", "weight_table", "checks")},
            actor=actor,
        )
        self.tasks.add_event(
            task_id,
            stage="admin",
            code="layout_confirmed",
            actor=actor,
            detail={"kind": kind, "fingerprint": fingerprint, "version": version},
        )
        return {"kind": kind, "fingerprint": fingerprint,
                "confirmed_at": row["confirmed_at"], "version": version}

    def reprocess_task(
        self, task_id: str, *, actor: str = "admin", regenerate_only: bool = False
    ) -> dict[str, Any]:
        task = self.tasks.require(task_id)
        self._require_active(task, "重新处理")
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
        self._require_active(task, "确认重新处理")
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
        self._require_active(task, "交付")
        if task["status"] != state.READY_TO_SEND:
            raise StateTransitionError(
                "Only READY_TO_SEND tasks can be delivered; reprocess or resolve first"
            )
        version = int(task["version"])
        artifacts = self.tasks.artifacts(task_id, version)
        if not artifacts:
            raise ValidationError("The current task version has no output artifacts")
        version_row = self.tasks.get_version(task_id, version) or {}
        stored_layout = self.tasks.layout(task_id, version)
        layout_report = None
        if stored_layout:
            layout_report = self._layout_report(
                task, {"kind": task.get("kind"), "layout": stored_layout}, None)
        row, _ = self.delivery.enqueue_delivery(
            task,
            record_count=int(version_row.get("record_count") or 0),
            artifacts=artifacts,
            content_hash=task.get("content_hash"),
            layout_report=layout_report,
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

    # ------------------------------------------------------------------
    # task lifecycle (archive / recycle bin) and destructive maintenance
    # ------------------------------------------------------------------
    def archive_task(self, task_id: str, *, actor: str = "admin", reason: str | None = None) -> dict[str, Any]:
        return self.tasks.archive(task_id, actor=actor, reason=reason)

    def unarchive_task(self, task_id: str, *, actor: str = "admin") -> dict[str, Any]:
        return self.tasks.unarchive(task_id, actor=actor)

    def delete_task(self, task_id: str, *, actor: str = "admin", reason: str | None = None) -> dict[str, Any]:
        return self.tasks.delete_to_trash(task_id, actor=actor, reason=reason)

    def restore_task(self, task_id: str, *, actor: str = "admin") -> dict[str, Any]:
        return self.tasks.restore_from_trash(task_id, actor=actor)

    def batch_archive(self, task_ids: list[str], *, actor: str = "admin", reason: str | None = None) -> dict[str, Any]:
        return self.tasks.batch_archive(task_ids, actor=actor, reason=reason)

    def batch_unarchive(self, task_ids: list[str], *, actor: str = "admin") -> dict[str, Any]:
        return self.tasks.batch_unarchive(task_ids, actor=actor)

    def batch_delete(self, task_ids: list[str], *, actor: str = "admin", reason: str | None = None) -> dict[str, Any]:
        return self.tasks.batch_delete(task_ids, actor=actor, reason=reason)

    def batch_restore(self, task_ids: list[str], *, actor: str = "admin") -> dict[str, Any]:
        return self.tasks.batch_restore(task_ids, actor=actor)

    def purge_task_files_preview(self, task_id: str) -> dict[str, Any]:
        """Preview what 'permanently purge task files' would remove.

        Only trashed tasks in a terminal status (COMPLETED/CANCELLED/FAILED/
        WAITING_INPUT/READY_TO_SEND) are eligible. SENT outbox rows with
        unconfirmed delivery (SEND_UNKNOWN) are excluded — the operator must
        resolve those first. Returns the file paths and sizes that would go,
        plus what is deliberately kept (dedupe hashes, send summaries, audit).
        """

        task = self.tasks.require(task_id)
        if task.get("deleted_at") is None:
            raise ValidationError("只有已删除到回收站的任务才能永久清理文件")
        if str(task["status"]) in {
            state.PROCESSING,
            state.VALIDATING,
            state.SEND_UNKNOWN,
            state.RECEIVED,
            state.RETRY_PENDING,
        }:
            raise ValidationError(
                "该任务尚未到达终态，不能永久清理文件；请等待处理结束或先核对发送结果"
            )
        uncertain = self.db.fetchone(
            "SELECT 1 FROM outbox WHERE task_id = ? AND status = 'send_unknown' LIMIT 1",
            (task_id,),
        )
        if uncertain is not None:
            raise ValidationError("该任务有发送结果不确定的邮件，请先核对后再清理")
        directory = (self.config.tasks_dir / task_id).resolve()
        files: list[dict[str, Any]] = []
        if directory.is_dir() and is_within(directory, self.config.tasks_dir.resolve()):
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    files.append({"path": str(path), "size": path.stat().st_size})
        return {
            "task_id": task_id,
            "status": task["status"],
            "file_count": len(files),
            "files": files,
            "kept": [
                "任务记录与状态、业务去重指纹(content_hash/business_identity)",
                "outbox 发送摘要与发送记录",
                "task_events 审计与字段来源(field_provenance)",
            ],
            "note": "文件清理后无法恢复原始附件；数据库摘要保留以便去重与审计。",
        }

    def purge_task_files(self, task_id: str, *, actor: str = "admin") -> dict[str, Any]:
        """Permanently delete runtime files for a trashed terminal task.

        Database rows (task, versions, outbox, events, provenance, issues) are
        kept so dedupe and send summaries still work; only runtime files under
        the task directory are removed. The original e-mail is never deleted
        here — it may be referenced by other versions or tasks.
        """

        preview = self.purge_task_files_preview(task_id)
        directory = (self.config.tasks_dir / task_id).resolve()
        removed = 0
        if directory.is_dir() and is_within(directory, self.config.tasks_dir.resolve()):
            shutil.rmtree(directory)
            removed = preview["file_count"]
        self.db.insert(
            """
            INSERT INTO task_events (
                task_id, version, stage, code, detail_json, actor, created_at
            ) VALUES (?, 0, 'cleanup', 'task_files_purged', ?, ?, ?)
            """,
            (
                task_id,
                json.dumps({"files_removed": removed}, ensure_ascii=False),
                actor,
                utc_now(),
            ),
        )
        self.logger.info("task_files_purged", task_id=task_id, actor=actor, files_removed=removed)
        return {"task_id": task_id, "files_removed": removed}

    # ------------------------------------------------------------------
    # test-data clear (identity-proven, never on a production runtime)
    # ------------------------------------------------------------------
    def test_identity(self) -> str:
        """The allowlist identity persisted at first initialization.

        ``test``/``production``. Absent means the runtime predates the marker
        and cannot be reliably identified, so test-clear must refuse.
        """

        from .runtime_meta import get_meta

        return get_meta(self.db, "environment") or "unknown"

    def record_environment_identity(self) -> None:
        """Record a durable environment identity WITHOUT trusting mode alone.

        Rules (restart-stable, never overwritten unsafely):

        * Effective mode ``production`` -> identity ``production`` and a
          permanent ``seen_production`` marker. Once seen, the DB can never be
          treated as a clearable test runtime, even if the operator later
          switches the mode back to ``test``.
        * Otherwise (mode ``test``) and no identity recorded yet:
          - if the database already holds tasks or mail (for example an old
            production DB being upgraded), record ``unknown`` — never ``test``;
          - only a genuinely empty runtime with mode ``test`` is recorded as
            ``test``.

        A prior ``production``/``unknown`` identity is never downgraded to
        ``test``. Combined with the "stop the poller / no in-progress / no
        unknown outbox" checks, this ensures test-clear cannot touch a
        production or ambiguous runtime.
        """

        from .runtime_meta import get_meta, set_meta, set_meta_if_absent

        effective_mode = self.settings.current().allowlist.mode
        if effective_mode == "production":
            set_meta_if_absent(self.db, "environment", "production")
            set_meta(self.db, "seen_production", "1")
            return
        # mode == test (or anything non-production)
        existing = get_meta(self.db, "environment")
        if existing is not None:
            return
        has_data = int(self.db.scalar("SELECT COUNT(*) FROM tasks") or 0) > 0 or int(
            self.db.scalar("SELECT COUNT(*) FROM mail_messages") or 0
        ) > 0
        identity = "unknown" if has_data else "test"
        set_meta_if_absent(self.db, "environment", identity)
        # Bind the identity to the canonical runtime directory so a test DB
        # copied into another (for example production) runtime cannot be
        # cleared there. Recorded once with the identity.
        try:
            set_meta_if_absent(
                self.db,
                "environment_runtime_dir",
                str(self.config.paths.runtime_dir.resolve()),
            )
        except OSError:
            pass

    def _clear_blockers(self) -> list[str]:
        blockers: list[str] = []
        active = self.db.scalar(
            """
            SELECT COUNT(*) FROM tasks
             WHERE deleted_at IS NULL AND status IN (?, ?, ?, ?)
            """,
            (
                state.PROCESSING,
                state.VALIDATING,
                state.SEND_UNKNOWN,
                state.RETRY_PENDING,
            ),
        )
        if int(active or 0):
            blockers.append(f"存在 {int(active)} 个处理中/待重试任务")
        inflight = self.db.scalar(
            "SELECT COUNT(*) FROM outbox WHERE status IN ('sending', 'send_unknown')"
        )
        if int(inflight or 0):
            blockers.append(f"存在 {int(inflight)} 封发送中/发送结果不确定的邮件（含无任务邮件）")
        return blockers

    def clear_test_data_preview(self, *, poller_running: bool = False) -> dict[str, Any]:
        from .runtime_meta import get_meta

        identity = self.test_identity()
        current_mode = self.settings.current().allowlist.mode
        if current_mode == "production":
            # Real-time safety net: if the live mode is production, record the
            # permanent marker now (covers direct/edge mode changes that bypassed
            # the audited settings path) so a later switch back to test cannot
            # make the runtime clearable.
            from .runtime_meta import set_meta

            set_meta(self.db, "seen_production", "1")
        seen_production = get_meta(self.db, "seen_production")
        recorded_dir = get_meta(self.db, "environment_runtime_dir")
        try:
            dir_ok = recorded_dir is None or recorded_dir == str(
                self.config.paths.runtime_dir.resolve()
            )
        except OSError:
            dir_ok = False
        # Identity must be a durable ``test`` marker bound to this runtime
        # directory, the effective mode must be test, and the runtime must never
        # have run in production mode.
        identity_ok = (
            identity == "test"
            and current_mode == "test"
            and seen_production is None
            and dir_ok
        )
        blockers = self._clear_blockers()
        if poller_running:
            blockers.append("邮箱轮询仍在运行，请先停止轮询")
        return {
            "eligible": identity_ok,
            "identity": identity,
            "current_mode": current_mode,
            "seen_production": seen_production is not None,
            "runtime_dir_ok": dir_ok,
            "in_progress_tasks": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM tasks WHERE deleted_at IS NULL "
                    "AND status IN (?, ?, ?, ?)",
                    (
                        state.PROCESSING,
                        state.VALIDATING,
                        state.SEND_UNKNOWN,
                        state.RETRY_PENDING,
                    ),
                )
                or 0
            ),
            "blockers": blockers,
            "runtime_dir": str(self.config.paths.runtime_dir),
            "removes": [
                "tasks / task_versions / task_inputs / task_overrides",
                "task_issues / field_provenance / task_artifacts / task_events",
                "outbox 邮件记录（含无任务邮件）",
                "mail_messages 与 runtime/mail 下按年月子目录保存的全部原始邮件",
                "mailbox_cursors 游标（清理后下次运行将重新建立首轮水位）",
                "runtime/tasks 任务目录文件",
            ],
            "keeps": [
                "countries / country_aliases / machine_materials / machine_aliases 参考资料与别名",
                "config_versions 审计与 runtime_meta 身份",
                "backups / diagnostics / schema_migrations",
            ],
        }

    def _remove_tree_children(self, root: Path) -> tuple[int, list[str]]:
        """Remove every child under ``root`` recursively; return (count, errors).

        File removal is checked: errors are returned so the caller can refuse to
        report success. Nothing outside ``root`` (verified via is_within) is
        touched.
        """

        removed = 0
        errors: list[str] = []
        if not root.is_dir():
            return 0, []
        root_resolved = root.resolve()
        for child in root_resolved.iterdir():
            if not is_within(child, root_resolved):
                errors.append(f"拒绝越界路径：{child}")
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except OSError as exc:
                errors.append(f"{child}: {exc}")
        return removed, errors

    def clear_test_data(
        self,
        *,
        actor: str = "admin",
        confirm: bool = False,
        poller_running: bool = False,
    ) -> dict[str, Any]:
        """Clear test task data after proving the runtime is a test identity.

        Requires a durable ``test`` identity (never a production/unknown one),
        effective mode ``test``, never-seen-production, explicit confirmation, a
        fresh backup, no in-progress tasks, no sending/unknown outbox, and the
        poller stopped. Files are removed first and any file-removal failure
        aborts before the database is touched, so a successful return means the
        files really were cleared. Reference data and configuration are kept.
        """

        preview = self.clear_test_data_preview(poller_running=poller_running)
        if not preview["eligible"]:
            raise ValidationError(
                "无法可靠判定当前运行目录为测试身份（初始化身份="
                f"{preview['identity']}，当前模式={preview['current_mode']}，"
                f"曾用于生产={preview['seen_production']}），拒绝清空以保护生产数据"
            )
        if not confirm:
            raise ValidationError("清空测试数据需要显式二次确认")
        if preview["blockers"]:
            raise ValidationError("清空受阻：" + "；".join(preview["blockers"]))

        safety = self.maintenance.backup("pre-test-clear")
        # Remove files FIRST; if any removal fails, abort before deleting rows so
        # we never claim success while originals remain (or vice versa).
        tasks_removed, task_errors = self._remove_tree_children(self.config.tasks_dir)
        mail_removed, mail_errors = self._remove_tree_children(self.config.mail_dir)
        errors = task_errors + mail_errors
        if errors:
            raise ValidationError(
                "文件清理失败，数据库未改动：" + "；".join(errors[:5])
            )
        with self.db.transaction() as conn:
            for table in (
                "task_artifacts",
                "field_provenance",
                "task_issues",
                "task_overrides",
                "task_inputs",
                "task_events",
                "outbox",
                "task_versions",
                "tasks",
                "mail_messages",
                "mailbox_cursors",
            ):
                conn.execute(f"DELETE FROM {table}")
        self.logger.warning(
            "test_data_cleared",
            actor=actor,
            safety_backup=str(safety),
            task_paths_removed=tasks_removed,
            mail_paths_removed=mail_removed,
        )
        return {
            "safety_backup": str(safety),
            "task_paths_removed": tasks_removed,
            "mail_paths_removed": mail_removed,
            "note": "首轮水位将在下次扫描时重新建立；历史补扫策略不变。",
        }

    # ------------------------------------------------------------------
    # machine reference import: preview then confirm (web flow)
    # ------------------------------------------------------------------
    def preview_machine_reference(self, path: str, *, actor: str = "admin") -> dict[str, Any]:
        importer = self._reference_importer or load_import_machine_reference()
        rows = importer(path)
        if not isinstance(rows, list):
            raise ValidationError("import_machine_reference must return a list")
        filename = Path(path).name
        return self.references.preview_machine_import(
            rows, filename=filename, source_path=path, actor=actor
        )

    def confirm_machine_reference(self, token: str, *, actor: str = "admin") -> dict[str, Any]:
        return self.references.confirm_machine_import(token, actor=actor)

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
                    if str(attachment.suffix).lower() not in {"xlsx", "xls", "xlsm"}:
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
                            "mime_type": (
                                "application/vnd.ms-excel"
                                if str(attachment.suffix).lower() == "xls"
                                else XLSX_MIME
                            ),
                            "source_type": "offline_attachment",
                        }
                    )
                continue
            if suffix not in {"xlsx", "xls", "xlsm"}:
                raise ValidationError(f"Unsupported offline source type: {path.name}")
            expanded.append(
                {
                    "data": path.read_bytes(),
                    "filename": safe_filename(path.name),
                    "mime_type": (
                        "application/vnd.ms-excel" if suffix == "xls" else XLSX_MIME
                    ),
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