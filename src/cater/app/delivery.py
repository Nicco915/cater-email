"""Question/delivery composition, reliable dispatch and uncertain-send recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import state
from .config import AppConfig
from .db import Database
from .errors import (
    MailAuthError,
    MailTransportError,
    SendNotAllowedError,
    SendRejectedError,
    SendUnknownError,
    StateTransitionError,
)
from .logging import JsonlLogger
from .mail.gateway import MailGateway
from .mail.message import OutboundAttachment, OutboundMessage, SendReceipt
from .mail_store import MailStore
from .outbox import (
    DRY_RUN,
    FAILED,
    PENDING,
    SEND_UNKNOWN,
    SENDING,
    SENT,
    OutboxRepository,
)
from .settings import SettingsService
from .tasks import TaskRepository
from .util import EMAIL_RE, is_within, normalize_email, sha256_text, utc_now

KIND_LABELS = {"parts": "配件", "machine": "整机", "auto": "自动识别"}
_QUIET_SUBJECT_RE = None


def _clean_subject(value: str, limit: int = 180) -> str:
    text = " ".join((value or "").replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


def task_marker(task_id: str, version: int) -> str:
    return f"[CATER {task_id} v{version}]"


def outbound_subject(original_subject: str, task_id: str, version: int) -> str:
    original = _clean_subject(original_subject) or "任务"
    marker = task_marker(task_id, version)
    # Do not nest a previously generated marker.
    import re

    original = re.sub(r"\[CATER\s+task_[a-z0-9]+\s+v\d+\]\s*", "", original, flags=re.I)
    return _clean_subject(f"{marker} {original}")


class DeliveryService:
    def __init__(
        self,
        db: Database,
        config: AppConfig,
        settings: SettingsService,
        logger: JsonlLogger,
        tasks: TaskRepository,
        outbox: OutboxRepository,
        mail_store: MailStore,
        gateway: MailGateway,
    ) -> None:
        self.db = db
        self.config = config
        self.settings = settings
        self.logger = logger
        self.tasks = tasks
        self.outbox = outbox
        self.mail_store = mail_store
        self.gateway = gateway

    # ------------------------------------------------------------------
    # message construction helpers
    # ------------------------------------------------------------------
    def _original_subject(self, task: dict[str, Any]) -> str:
        origin = self.mail_store.get(str(task["origin_mail_id"])) if task.get("origin_mail_id") else None
        return str(origin.get("subject") or "") if origin else ""

    def _thread_headers(self, task: dict[str, Any]) -> tuple[str | None, list[str]]:
        mails = [item for item in self.mail_store.for_task(str(task["id"])) if item.get("message_id")]
        references: list[str] = []
        for mail in mails:
            message_id = str(mail["message_id"]).strip().strip("<>")
            if message_id and message_id not in references:
                references.append(message_id)
        if not references and task.get("origin_mail_id"):
            origin = self.mail_store.get(str(task["origin_mail_id"]))
            if origin and origin.get("message_id"):
                references.append(str(origin["message_id"]).strip("<>"))
        in_reply_to = references[-1] if references else None
        return in_reply_to, references

    def _message_id(self, task_id: str, purpose: str, dedupe_key: str) -> str:
        account = self.config.mail.account or "cater.local"
        domain = account.rsplit("@", 1)[-1] if "@" in account else "cater.local"
        token = sha256_text(dedupe_key)[:24]
        return f"cater.{task_id}.{purpose}.{token}@{domain}"

    def _live_allowed(self, recipient: str) -> bool:
        settings = self.settings.current()
        return settings.sending.live_allowed(settings.allowlist) and settings.allowlist.contains(
            recipient
        )

    def _recipient(self, task: dict[str, Any]) -> str:
        recipient = normalize_email(str(task["owner"]))
        if not EMAIL_RE.match(recipient):
            raise ValueError(f"Invalid task owner address: {recipient!r}")
        return recipient

    # ------------------------------------------------------------------
    # questions
    # ------------------------------------------------------------------
    def question_body(self, task: dict[str, Any], issues: list[dict[str, Any]]) -> str:
        lines = ["您好：", "", "这票资料还需要您确认以下内容，暂未发送结果文件。", ""]
        examples: list[str] = []
        labels = {
            "dealer_code": "经销商代码", "dealer_order": "客户订单号", "order": "SO订单号",
            "material": "机型", "model": "机型", "serial_number": "机器序列号",
            "total_sales_price": "机器销售价格", "net_weight": "净重", "qty": "数量",
            "part_code": "商品料号", "coo": "原产国", "destination": "目的国",
            "unit_price": "单价", "hs_code": "商品编码",
        }
        for issue in issues:
            code = str(issue.get("code") or "")
            record = issue.get("record_key")
            field = str(issue.get("field") or "")
            prefix = f"编号 {record}：" if record else ""
            if code == "DESTINATION_MISSING":
                lines.append("- 请告诉我们这票货物的目的国。")
                examples.append("目的国：法国")
            elif code == "DESTINATION_UNKNOWN" or (code in {"UNKNOWN_COUNTRY", "COUNTRY_UNKNOWN", "COUNTRY_MAPPING_MISSING"} and field == "destination"):
                lines.append("- 国家名称暂时无法识别，请提供标准国家名称。")
                examples.append("目的国：法国")
            elif code == "REPROCESS_CONFIRMATION_REQUIRED":
                lines.append("- 这票货物之前处理过，本次资料有变化，请确认是否重新处理。")
                examples.append("确认重新处理")
            elif code == "MISSING_FIELD" or code.startswith("MISSING_"):
                fields = [item.strip() for item in field.split(",") if item.strip()]
                names = "、".join(labels.get(item, "业务信息") for item in fields) or "业务信息"
                lines.append(f"- {prefix}缺少{names}，请补充。")
                if record and fields:
                    for name in fields:
                        value = "4" if name == "qty" else "请填写正确值"
                        examples.append(f"修正：{record}.{name}={value}")
                else:
                    lines.append("  请联系处理人员核对该项资料，再按提示补充。")
            elif code == "FIELD_CONFLICT":
                message = str(issue.get("message") or "")
                lines.append(f"- {prefix}{labels.get(field, '资料')}在邮件与附件中不一致，请确认正确值。")
                # Preserve useful business values without dumping internal source objects.
                if "不一致：" in message:
                    lines.append("  两处填写分别为：" + message.split("不一致：", 1)[1])
                if record and field:
                    examples.append(f"修正：{record}.{field}=请填写正确值")
            elif "MODEL" in code or "MACHINE" in code or "SPECIAL_RULE" in code:
                lines.append(f"- {prefix}机型或特殊申报资料尚未确认，请联系资料维护人员核对。")
            elif "ATTACHMENT" in code or "WORKBOOK" in code or "SHEET" in code or "UNSUPPORTED" in code:
                lines.append("- 附件或工作表无法使用，请回复正确、完整且未加密的 Excel 文件。")
            elif "MAPPING" in code or "COUNTRY" in code:
                lines.append(f"- {prefix}国家对应关系需要确认，请联系资料维护人员核对国家代码。")
            else:
                lines.append(f"- {prefix}资料暂时无法通过检查，请联系处理人员核对本票资料。")
        examples = list(dict.fromkeys(examples))
        if examples:
            lines.extend(["", "请直接回复本邮件，按下面格式填写实际内容：", ""])
            lines.extend(examples)
            if any("请填写正确值" in item for item in examples):
                lines.append("（保留等号前的内容，把等号后的文字换成正确值。）")
            elif examples == ["目的国：法国"]:
                lines.append("（把“法国”换成这票货物实际的目的国。）")
        lines.extend(["", "请保留邮件主题。收到补充后，我们会继续检查并处理。",
                      "", f"查询编号：{task['id']}"])
        return "\n".join(lines)

    def enqueue_question(
        self, task: dict[str, Any], issues: Iterable[dict[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        issue_list = [issue for issue in issues if issue.get("blocking", True)]
        if not issue_list:
            issue_list = list(issues)
        digest_payload = [
            {
                "code": issue.get("code"),
                "record_key": issue.get("record_key"),
                "field": issue.get("field"),
                "message": issue.get("message"),
                "required_action": issue.get("required_action"),
            }
            for issue in issue_list
        ]
        digest = sha256_text(json.dumps(digest_payload, ensure_ascii=False, sort_keys=True))
        dedupe_key = f"question:{task['id']}:{digest}"
        recipient = self._recipient(task)
        in_reply_to, references = self._thread_headers(task)
        body = self.question_body(task, issue_list)
        task_version = int(task["version"])
        subject = outbound_subject(self._original_subject(task), str(task["id"]), task_version)
        dry_run = not self._live_allowed(recipient)
        row, created = self.outbox.enqueue(
            dedupe_key=dedupe_key,
            task_id=str(task["id"]),
            version=task_version,
            purpose="question",
            recipient=recipient,
            recipient_name=task.get("owner_name"),
            subject=subject,
            body=body,
            attachments=[],
            in_reply_to=in_reply_to,
            references=references,
            message_id=self._message_id(str(task["id"]), "question", dedupe_key),
            dry_run=dry_run,
        )
        if created:
            self.tasks.add_event(
                str(task["id"]),
                stage="delivery",
                code="question_enqueued",
                detail={"outbox_id": row["id"], "dry_run": dry_run, "issues": len(issue_list)},
            )
            self.logger.info(
                "question_enqueued",
                task_id=task["id"],
                outbox_id=row["id"],
                dry_run=dry_run,
                issue_codes=[issue.get("code") for issue in issue_list],
            )
        return row, created

    # ------------------------------------------------------------------
    # notices
    # ------------------------------------------------------------------
    def enqueue_notice(
        self,
        task: dict[str, Any],
        *,
        body: str,
        dedupe_suffix: str,
    ) -> tuple[dict[str, Any], bool]:
        recipient = self._recipient(task)
        in_reply_to, references = self._thread_headers(task)
        dedupe_key = f"notice:{task['id']}:{sha256_text(dedupe_suffix)[:32]}"
        subject = outbound_subject(self._original_subject(task), str(task["id"]), int(task["version"]))
        dry_run = not self._live_allowed(recipient)
        row, created = self.outbox.enqueue(
            dedupe_key=dedupe_key,
            task_id=str(task["id"]),
            version=int(task["version"]),
            purpose="notice",
            recipient=recipient,
            recipient_name=task.get("owner_name"),
            subject=subject,
            body=body,
            attachments=[],
            in_reply_to=in_reply_to,
            references=references,
            message_id=self._message_id(str(task["id"]), "notice", dedupe_key),
            dry_run=dry_run,
        )
        if created:
            self.tasks.add_event(
                str(task["id"]),
                stage="delivery",
                code="notice_enqueued",
                detail={"outbox_id": row["id"], "dry_run": dry_run},
            )
        return row, created

    # ------------------------------------------------------------------
    # delivery
    # ------------------------------------------------------------------
    def delivery_body(
        self,
        task: dict[str, Any],
        *,
        record_count: int,
        artifacts: list[dict[str, Any]],
    ) -> str:
        marker = task_marker(str(task["id"]), int(task["version"]))
        lines = [
            f"{marker} 处理完成",
            "",
            "您好：",
            "",
            "本地处理程序已生成以下业务文件，请查收附件。",
            f"任务编号：{task['id']}",
            f"业务类型：{KIND_LABELS.get(str(task['kind']), str(task['kind']))}",
            f"目的国：{task.get('destination_name') or '未提供'}",
            f"明细数量：{record_count}",
            "附件：",
        ]
        for artifact in artifacts:
            lines.append(f"- {artifact['filename']}")
        lines.extend(
            [
                "",
                "说明：本邮件仅报告本地文件生成结果，不声称文件已上传任何业务系统。",
                "如数据有误，请使用规范格式回复（附任务编号）。",
                "",
            ]
        )
        return "\n".join(lines)

    def enqueue_delivery(
        self,
        task: dict[str, Any],
        *,
        record_count: int,
        artifacts: list[dict[str, Any]],
        content_hash: str | None,
    ) -> tuple[dict[str, Any], bool]:
        if not artifacts:
            raise ValueError("A delivery requires at least one artifact")
        task_root = (self.config.tasks_dir / str(task["id"])).resolve()
        attachment_payload: list[dict[str, str]] = []
        for artifact in artifacts:
            path = Path(str(artifact["path"])).resolve()
            if not is_within(path, task_root) or not path.is_file():
                raise ValueError(f"Artifact is outside the task directory: {artifact['filename']}")
            attachment_payload.append(
                {
                    "path": str(path),
                    "filename": str(artifact["filename"]),
                    "content_type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                }
            )
        dedupe_key = (
            f"delivery:{task['id']}:v{task['version']}:{content_hash}"
            if content_hash
            else f"delivery:{task['id']}:v{task['version']}"
        )
        recipient = self._recipient(task)
        in_reply_to, references = self._thread_headers(task)
        subject = outbound_subject(self._original_subject(task), str(task["id"]), int(task["version"]))
        body = self.delivery_body(task, record_count=record_count, artifacts=artifacts)
        dry_run = not self._live_allowed(recipient)
        row, created = self.outbox.enqueue(
            dedupe_key=dedupe_key,
            task_id=str(task["id"]),
            version=int(task["version"]),
            purpose="delivery",
            recipient=recipient,
            recipient_name=task.get("owner_name"),
            subject=subject,
            body=body,
            attachments=attachment_payload,
            in_reply_to=in_reply_to,
            references=references,
            message_id=self._message_id(str(task["id"]), "delivery", dedupe_key),
            dry_run=dry_run,
        )
        if created:
            self.tasks.add_event(
                str(task["id"]),
                stage="delivery",
                code="delivery_enqueued",
                detail={
                    "outbox_id": row["id"],
                    "dry_run": dry_run,
                    "artifacts": [artifact["filename"] for artifact in artifacts],
                },
            )
            self.logger.info(
                "delivery_enqueued",
                task_id=task["id"],
                outbox_id=row["id"],
                dry_run=dry_run,
                content_hash=content_hash,
            )
        return row, created

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    def dispatch(self, outbox_id: str, *, actor: str = "system") -> dict[str, Any]:
        row = self.outbox.get(outbox_id)
        if row is None:
            raise KeyError(outbox_id)
        if row["status"] == DRY_RUN:
            raise SendNotAllowedError(
                "Outbox message is in dry-run status; use the explicit release action"
            )
        if row["status"] == SENDING:
            raise StateTransitionError(
                "Outbox message is already being sent; do not duplicate the attempt"
            )
        if row["status"] != PENDING:
            return row
        recipient = normalize_email(str(row["recipient"]))
        if not self._live_allowed(recipient):
            raise SendNotAllowedError(
                "Live sending requires sending.enabled=true, dry_run=false and the recipient "
                "in the active allowlist"
            )
        if not self.config.mail.account:
            raise SendNotAllowedError("mail.account is not configured")
        claimed = self.outbox.mark_sending(outbox_id)
        self.tasks.add_event(
            str(claimed["task_id"]),
            stage="delivery",
            code="send_attempt",
            actor=actor,
            detail={"outbox_id": outbox_id, "attempt": claimed["attempts"]},
        )
        attachments = [
            OutboundAttachment(
                path=str(item["path"]),
                filename=str(item["filename"]),
                content_type=str(item.get("content_type") or "application/octet-stream"),
            )
            for item in claimed.get("attachments", [])
        ]
        message = OutboundMessage(
            recipient=recipient,
            subject=str(claimed["subject"]),
            body=str(claimed["body"]),
            message_id=str(claimed["message_id"]),
            task_id=str(claimed["task_id"]),
            purpose=str(claimed["purpose"]),
            attachments=tuple(attachments),
            in_reply_to=claimed.get("in_reply_to"),
            references=tuple(str(claimed.get("references_header") or "").split()),
        )
        try:
            receipt = self.gateway.send(message, from_address=self.config.mail.account)
        except SendUnknownError as exc:
            unknown = self.outbox.mark_unknown(outbox_id, str(exc))
            self.logger.warning(
                "send_unknown",
                outbox_id=outbox_id,
                task_id=unknown["task_id"],
                attempt=unknown["attempts"],
                error=str(exc),
            )
            self._on_unknown(unknown)
            # Best-effort sent-folder check; a negative result does not license
            # a resend. Only the admin confirmation path can do that.
            try:
                self.recheck_unknown(outbox_id, actor="system")
            except MailTransportError:
                pass
            return self.outbox.get(outbox_id) or unknown
        except (SendRejectedError, MailAuthError) as exc:
            failed = self.outbox.mark_failed(outbox_id, str(exc))
            self.tasks.add_event(
                str(failed["task_id"]),
                stage="delivery",
                code="send_permanent_failure",
                actor=actor,
                detail={"outbox_id": outbox_id, "error": str(exc)},
            )
            self.logger.error(
                "send_permanent_failure",
                outbox_id=outbox_id,
                task_id=failed["task_id"],
                error=str(exc),
            )
            return failed
        except MailTransportError as exc:
            attempts = int(claimed["attempts"])
            if attempts >= 3:
                failed = self.outbox.mark_failed(outbox_id, str(exc))
                self.logger.error(
                    "send_retry_exhausted",
                    outbox_id=outbox_id,
                    task_id=failed["task_id"],
                    attempts=attempts,
                    error=str(exc),
                )
                return failed
            delay = (30, 120, 600)[min(attempts - 1, 2)]
            retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            pending = self.outbox.mark_pending(
                outbox_id, error=str(exc), next_attempt_at=retry_at
            )
            self.tasks.add_event(
                str(pending["task_id"]),
                stage="delivery",
                code="send_retry_scheduled",
                actor=actor,
                detail={"outbox_id": outbox_id, "delay_seconds": delay, "error": str(exc)},
            )
            self.logger.warning(
                "send_retry_scheduled",
                outbox_id=outbox_id,
                task_id=pending["task_id"],
                delay_seconds=delay,
                error=str(exc),
            )
            return pending

        sent = self.outbox.mark_sent(
            outbox_id, response=receipt.response, provider_message_id=receipt.provider_id
        )
        self._on_sent(sent, actor=actor)
        return self.outbox.get(outbox_id) or sent

    def _on_sent(self, outbox_row: dict[str, Any], *, actor: str) -> None:
        task_id = str(outbox_row["task_id"])
        task = self.tasks.require(task_id)
        self.tasks.add_event(
            task_id,
            stage="delivery",
            code=f"{outbox_row['purpose']}_sent",
            actor=actor,
            detail={"outbox_id": outbox_row["id"], "message_id": outbox_row["message_id"]},
        )
        if outbox_row["purpose"] == "delivery" and task["status"] in {
            state.READY_TO_SEND,
            state.SEND_UNKNOWN,
        }:
            try:
                self.tasks.transition(
                    task_id,
                    state.COMPLETED,
                    stage="delivery",
                    actor=actor,
                    code="delivery_accepted",
                    detail={"outbox_id": outbox_row["id"]},
                )
            except Exception:
                self.logger.exception(
                    "task_complete_after_send_failed",
                    Exception("task status update failed after SMTP acceptance"),
                    task_id=task_id,
                    outbox_id=outbox_row["id"],
                )

    def _on_unknown(self, outbox_row: dict[str, Any]) -> None:
        if outbox_row["purpose"] != "delivery":
            return
        task = self.tasks.require(str(outbox_row["task_id"]))
        if task["status"] == state.READY_TO_SEND:
            try:
                self.tasks.transition(
                    str(task["id"]),
                    state.SEND_UNKNOWN,
                    stage="delivery",
                    code="send_unknown",
                    detail={"outbox_id": outbox_row["id"]},
                )
            except Exception:
                self.logger.exception(
                    "task_send_unknown_transition_failed",
                    Exception("task status update failed"),
                    task_id=task["id"],
                )

    def recheck_unknown(self, outbox_id: str, *, actor: str = "system") -> bool:
        row = self.outbox.get(outbox_id)
        if row is None:
            raise KeyError(outbox_id)
        if row["status"] != SEND_UNKNOWN:
            return row["status"] == SENT
        found = self.gateway.find_sent(
            self.config.mail.account, self.config.mail.sent_folder, str(row["message_id"])
        )
        if found:
            sent = self.outbox.mark_sent(
                outbox_id, response="found in Sent folder after uncertain send"
            )
            self._on_sent(sent, actor=actor)
            self.logger.info(
                "send_unknown_confirmed_by_sent_folder",
                outbox_id=outbox_id,
                task_id=sent["task_id"],
            )
            return True
        self.logger.warning(
            "send_unknown_not_found_in_sent",
            outbox_id=outbox_id,
            task_id=row["task_id"],
            required_action="manual confirmation required; do not resend automatically",
        )
        return False

    def confirm_unknown_sent(self, outbox_id: str, *, actor: str) -> dict[str, Any]:
        row = self.outbox.get(outbox_id)
        if row is None:
            raise KeyError(outbox_id)
        if row["status"] != SEND_UNKNOWN:
            raise ValueError("Only SEND_UNKNOWN messages can be confirmed as sent")
        row = self.outbox.mark_sent(
            outbox_id, response="administrator confirmed message exists in Sent folder"
        )
        self._on_sent(row, actor=actor)
        return self.outbox.get(outbox_id) or row

    def confirm_unknown_not_sent(self, outbox_id: str, *, actor: str) -> dict[str, Any]:
        row = self.outbox.get(outbox_id)
        if row is None:
            raise KeyError(outbox_id)
        if row["status"] != SEND_UNKNOWN:
            raise ValueError("Only SEND_UNKNOWN messages can be confirmed as not sent")
        pending = self.outbox.mark_pending(outbox_id, error=None)
        task = self.tasks.require(str(row["task_id"]))
        if task["status"] == state.SEND_UNKNOWN and row["purpose"] == "delivery":
            self.tasks.transition(
                str(task["id"]),
                state.READY_TO_SEND,
                stage="delivery",
                actor=actor,
                code="send_unknown_confirmed_not_sent",
                detail={"outbox_id": outbox_id},
            )
        self.tasks.add_event(
            str(row["task_id"]),
            stage="delivery",
            code="unknown_confirmed_not_sent",
            actor=actor,
            detail={"outbox_id": outbox_id},
        )
        return self.outbox.get(outbox_id) or pending

    def release_dry_run(self, outbox_id: str, *, actor: str) -> dict[str, Any]:
        row = self.outbox.get(outbox_id)
        if row is None:
            raise KeyError(outbox_id)
        recipient = normalize_email(str(row["recipient"]))
        if not self._live_allowed(recipient):
            raise SendNotAllowedError(
                "Cannot release a dry-run message while sending is disabled or the recipient "
                "is outside the active allowlist"
            )
        self.outbox.mark_pending(outbox_id)
        self.tasks.add_event(
            str(row["task_id"]),
            stage="delivery",
            code="dry_run_released",
            actor=actor,
            detail={"outbox_id": outbox_id},
        )
        return self.dispatch(outbox_id, actor=actor)

    def retry_failed(self, outbox_id: str, *, actor: str) -> dict[str, Any]:
        row = self.outbox.get(outbox_id)
        if row is None:
            raise KeyError(outbox_id)
        if row["status"] != FAILED:
            raise ValueError("Only failed outbox messages can be retried explicitly")
        self.outbox.mark_pending(outbox_id)
        self.tasks.add_event(
            str(row["task_id"]),
            stage="delivery",
            code="outbox_retry_requested",
            actor=actor,
            detail={"outbox_id": outbox_id},
        )
        try:
            return self.dispatch(outbox_id, actor=actor)
        except SendNotAllowedError:
            # The message is now pending and will be attempted when live
            # sending is explicitly enabled for its recipient.
            return self.outbox.get(outbox_id) or row

    def send_due(self, *, limit: int = 20) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for row in self.outbox.list_due(limit=limit):
            try:
                results.append(self.dispatch(str(row["id"])))
            except SendNotAllowedError:
                break
            except Exception as exc:
                self.logger.exception(
                    "outbox_dispatch_error", exc, outbox_id=row["id"], task_id=row["task_id"]
                )
                results.append(row)
        return results