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
STATUS_LABELS = {
    state.RECEIVED: "已收到，等待检查",
    state.VALIDATING: "资料检查中",
    state.PROCESSING: "正在处理",
    state.READY_TO_SEND: "结果文件已生成，等待发送",
    state.COMPLETED: "已完成",
    state.WAITING_INPUT: "等待您补充资料",
    state.RETRY_PENDING: "遇到技术故障，正在按间隔自动重试",
    state.FAILED: "技术故障，自动重试已用完，等待处理",
    state.SEND_UNKNOWN: "发送结果待人工确认",
    state.CANCELLED: "已取消",
}
FIELD_LABELS = {
    "dealer_code": "经销商代码", "dealer_order": "客户订单号", "order": "SO订单号",
    "material": "机型", "model": "机型", "serial_number": "机器序列号",
    "total_sales_price": "机器销售价格", "net_weight": "净重", "qty": "数量",
    "part_code": "商品料号", "coo": "原产国", "destination": "目的国",
    "unit_price": "单价", "hs_code": "商品编码",
    "unit_weight": "单件重", "packages": "件数", "gross_weight": "毛重",
    "hs_code_titled": "表头HS", "description": "品名", "gross": "毛重",
}


def _humanize_detail(message: str) -> str:
    """把问题消息里的 Python 字面量换成可读中文（不泄露内部结构）。

    - ``['coo', 'qty']`` → ``原产国、数量``（字段走 FIELD_LABELS，其余按原文）；
    - ``[('qty', [6, 10])]`` → ``数量（第6、10列均命中，无法唯一确定）``；
    - ``行[17, 24]`` → ``第17、24``；``[]`` → ``无``。
    """
    import re
    text = str(message or "")
    if not text:
        return ""
    text = re.sub(r"\[\s*\]", "无", text)

    def _ambiguous(m: "re.Match[str]") -> str:
        field = m.group(1)
        cols = "、".join(n.strip() for n in m.group(2).split(",") if n.strip())
        return f"{FIELD_LABELS.get(field, field)}（第{cols}列均命中，无法唯一确定）"

    text = re.sub(r"\('([a-z_]+)',\s*\[([0-9,\s]+)\]\)", _ambiguous, text)

    def _quoted_list(m: "re.Match[str]") -> str:
        items = [i.strip() for i in re.findall(r"'([^']*)'", m.group(0))]
        return "、".join(
            FIELD_LABELS.get(item, item) if re.fullmatch(r"[a-z_]+", item) else item
            for item in items)

    text = re.sub(r"\[((?:\s*'[^']+'\s*,?)+)\]", _quoted_list, text)
    text = re.sub(r"\[([0-9][0-9,\s]*)\]",
                  lambda m: "第" + "、".join(
                      n.strip() for n in m.group(1).split(",") if n.strip()),
                  text)
    # 元组列表替换后残留的外层方括号（含中文内容）收尾去掉
    text = re.sub(r"\[([^\[\]]*[一-鿿][^\[\]]*)\]", r"\1", text)
    text = re.sub(
        r"\b(part_code|unit_price|net_weight|unit_weight|hs_code_titled|"
        r"hs_code|qty|coo|model|serial|packages|gross_weight)\b",
        lambda m: FIELD_LABELS.get(m.group(1), m.group(1)), text)
    return (text.replace("=无", "：无").replace("=None", "为空")
            .replace("行第", "第").replace("（ambiguous）", "（存在多候选）")
            .replace("（incomplete）", "（缺少必需列）")
            .replace("（second_header）", "（出现重复表头行）")
            .replace("（multiple_totals）", "（多张合计行）").strip())


def _source_location(issue: dict) -> str:
    """首个来源的工作表/单元格定位（面向用户的简短位置描述）。"""
    for src in issue.get("sources") or []:
        if not isinstance(src, dict):
            continue
        sheet, cell = src.get("sheet"), src.get("cell")
        if sheet and cell:
            return f"（{sheet} 表 {cell}）"
        if sheet:
            return f"（{sheet} 表）"
        if src.get("filename"):
            return f"（附件 {src['filename']}）"
    return ""


# 输入结构类问题（附件表头/表块/合计行/明细行）的可读引导：这些是
# 客户工作簿的结构问题，不是机型资料库问题，必须给出工作表/单元格
# 定位、实际缺失/多候选字段与可执行的补充动作，禁止笼统“联系处理人员”。
_STRUCTURE_GUIDANCE = {
    "INVALID_DETAIL_ROW": ("最终发票明细行的必填数值缺失或不是有效数字，该行未纳入输出",
                           "请补齐该行数量/单价后，将修正后的 Excel 附件直接回复本邮件"),
    "FINAL_SHEET_INVALID": ("最终发票表结构不完整或不唯一，不能自动替代或猜测",
                            "请补齐表头（料号/数量/单价/原产国/重量）与表头右侧三列"
                            "补充资料（商品编码/中文名/申报要素）后重发附件"),
    "AMBIGUOUS_FINAL_SHEET": ("存在多张合格的最终发票表，无法唯一选定",
                              "请仅保留一张最终业务表（删除或移出其余工作表）后重发附件"),
    "FINAL_SHEET_NOT_FOUND": ("未找到含补充资料列的最终调整表（如 CI-For Dealer (2)），"
                              "原表不能自动替代",
                              "请提供含补充资料（表头右侧三列）的最终调整表 Excel 附件"),
    "SUPPLEMENTARY_COLUMNS_INVALID": ("最终调整表右侧三列补充资料"
                                      "（商品编码/中文名/申报要素）不完整",
                                      "请确认三列依次填写（商品编码为 8–13 位数字）后重发附件"),
    "PACKING_TOTALS_INVALID": ("箱单的件数/毛重合计列无法唯一定位",
                               "请确认箱单表头只保留一份 PACKAGE 与 GROSS WEIGHT 列"
                               "后重发附件"),
    "MULTIPLE_TOTAL_ROWS": ("同一张表块内出现多张合计（TOTAL）行，无法确定以哪一行为准",
                            "请仅保留本票唯一一张 TOTAL 行（其余合计行移出该表块）后重发附件"),
    "MACHINE_TABLE_INVALID": ("机器信息表结构不完整、表头不唯一或存在多张表块",
                              "请只保留一张连续的机器信息表（表头含 MODEL/MSO# 等，"
                              "各表块用空行分隔）后重发附件"),
    "AMBIGUOUS_MACHINE_TABLE": ("多张工作表都存在机器信息表，无法唯一选定",
                                "请删除多余工作表或明确本票使用的机器信息表后重发附件"),
    "WEIGHT_TABLE_INVALID": ("箱单重量表结构不完整或表头不唯一",
                             "请补齐重量表表头（DESCRIPTION/QTY/NET WEIGHT 等）"
                             "并保证唯一后重发附件"),
    "AMBIGUOUS_WEIGHT_TABLE": ("多张工作表都存在箱单重量表，无法唯一选定",
                               "请删除多余工作表或明确本票使用的箱单重量表后重发附件"),
    "MISSING_SOURCE": ("未收到所需的 Excel 附件", None),
    "UNSUPPORTED_FILE": ("附件无法使用（不是有效的 xlsx/xls、文件已加密，"
                         "或不含所需工作表）", None),
    "WORKBOOK_DEPENDENCY": ("附件已收到，但运行环境缺少解析该 Excel 所需的组件"
                            "（如 xlrd）", None),
    "AMBIGUOUS_INPUT": ("多份内容不同的附件都符合本票要求，无法自动选定，"
                        "本次未采用任何一份（已保存附件不会被删除或改动）", None),
}
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
    def _describe_issues(
        self, issues: list[dict[str, Any]]
    ) -> tuple[list[str], list[str]]:
        """Translate issue codes into short Chinese lines and reply examples."""

        lines: list[str] = []
        examples: list[str] = []
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
            elif code in _STRUCTURE_GUIDANCE:
                head, default_action = _STRUCTURE_GUIDANCE[code]
                if record and code == "INVALID_DETAIL_ROW":
                    head = (f"编号 {record}：" + head.replace(
                        "最终发票明细行", "所在明细行"))
                lines.append(f"- {head}{_source_location(issue)}。")
                detail = _humanize_detail(issue.get("message"))
                if detail:
                    lines.append(f"  具体情况：{detail}")
                action = issue.get("required_action") or default_action
                if action:
                    lines.append(f"  {action}")
            elif code == "MISSING_FIELD" or code.startswith("MISSING_"):
                fields = [item.strip() for item in field.split(",") if item.strip()]
                names = "、".join(FIELD_LABELS.get(item, "业务信息") for item in fields) or "业务信息"
                lines.append(f"- {prefix}缺少{names}，请补充。")
                if record and fields:
                    for name in fields:
                        value = "4" if name == "qty" else "请填写正确值"
                        examples.append(f"修正：{record}.{name}={value}")
                elif issue.get("required_action"):
                    lines.append(f"  {issue['required_action']}")
                else:
                    lines.append("  请联系处理人员核对该项资料，再按提示补充。")
            elif code == "FIELD_CONFLICT":
                message = str(issue.get("message") or "")
                lines.append(f"- {prefix}{FIELD_LABELS.get(field, '资料')}在邮件与附件中不一致，请确认正确值。")
                # Preserve useful business values without dumping internal source objects.
                if "不一致：" in message:
                    lines.append("  两处填写分别为：" + message.split("不一致：", 1)[1])
                if record and field:
                    examples.append(f"修正：{record}.{field}=请填写正确值")
            elif "MODEL" in code or "MACHINE" in code or "SPECIAL_RULE" in code:
                lines.append(f"- {prefix}机型或特殊申报资料尚未确认，请联系资料维护人员核对。")
            elif code == "ATTACHMENT_NAME_MISMATCH":
                lines.append(
                    f"- 附件“{issue.get('message') or ''}”与已保存的资料文件名不一致。"
                    "为保证不误用文件，本次未替换。"
                )
                if issue.get("required_action"):
                    lines.append(f"  {issue['required_action']}")
            elif code == "ATTACHMENT_NAME_CONFLICT":
                lines.append(
                    f"- 同一邮件中多份名为“{issue.get('message') or ''}”的附件内容不一致，"
                    "本次未采用任何一份。"
                )
                lines.append("  请只保留一份正确文件后重新发送。")
            elif code == "ATTACHMENT_ROLE_UNKNOWN":
                lines.append(
                    f"- 收到多份不同名附件（{'、'.join(str(item).strip() for item in str(issue.get('message') or '').split('、'))}），"
                    "本任务还没有已保存的 Excel 附件，无法判别各自角色，本次未采用任何一份。"
                )
                if issue.get("required_action"):
                    lines.append(f"  {issue['required_action']}")
            elif "ATTACHMENT" in code or "WORKBOOK" in code or "SHEET" in code or "UNSUPPORTED" in code:
                lines.append("- 附件或工作表无法使用，请回复正确、完整且未加密的 Excel 文件。")
            elif "MAPPING" in code or "COUNTRY" in code:
                lines.append(f"- {prefix}国家对应关系需要确认，请联系资料维护人员核对国家代码。")
            else:
                lines.append(f"- {prefix}资料暂时无法通过检查，请联系处理人员核对本票资料。")
        return lines, examples

    def question_body(self, task: dict[str, Any], issues: list[dict[str, Any]]) -> str:
        lines = ["您好：", "", "这票资料还需要您确认以下内容，暂未发送结果文件。", ""]
        issue_lines, examples = self._describe_issues(issues)
        lines.extend(issue_lines)
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
    # status reports (read-only answers to owner "查询状态" mail commands)
    # ------------------------------------------------------------------
    def status_report_body(
        self, task: dict[str, Any], open_issues: list[dict[str, Any]]
    ) -> str:
        status = str(task["status"])
        label = STATUS_LABELS.get(status, status)
        lines = [
            "您好：",
            "",
            f"任务编号：{task['id']}（第 {int(task['version'])} 版）",
            f"当前状态：{label}",
            "",
        ]
        if open_issues:
            lines.append("当前需要您处理的问题：")
            issue_lines, examples = self._describe_issues(open_issues)
            lines.extend(issue_lines)
            examples = list(dict.fromkeys(examples))
            if examples:
                lines.extend(["", "请直接回复本邮件，按下面格式填写实际内容：", ""])
                lines.extend(examples)
            lines.extend(["", "补齐后我们会继续检查并处理，无需其他操作。"])
        else:
            if status in {state.RETRY_PENDING, state.FAILED}:
                lines.append(
                    "处理程序遇到技术故障，您已保存的资料不会丢失，无需重新发送邮件。"
                )
                lines.append("可直接回复本邮件，新增正文单独写：重试")
                lines.append("如多次重试仍不成功，请联系维护人员处理。")
            elif status == state.SEND_UNKNOWN:
                lines.append("结果邮件的发送情况需要维护人员人工核对，在此之前不会重复发送。")
                lines.append("如急需结果文件，请联系维护人员。")
            elif status in {state.READY_TO_SEND, state.COMPLETED}:
                lines.append("结果文件已生成并按当前发送设置处理，无需操作。")
                if status == state.COMPLETED:
                    lines.append("如需基于最新资料再次生成，请回复：确认重新处理")
            else:
                lines.append("任务正在按流程处理，无需操作；有新进展会再通知您。")
        lines.extend(
            [
                "",
                "说明：本邮件只是状态告知，不会改动您的资料或任务。",
                f"查询编号：{task['id']}",
            ]
        )
        return "\n".join(lines)

    def enqueue_status_report(
        self,
        task: dict[str, Any],
        *,
        query_mail_id: str,
        open_issues: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Persist a read-only status answer, deduplicated per query mail.

        The dedupe key combines a fingerprint of the querying mail with a
        digest of the visible task state: redelivering the same query mail
        while nothing changed reuses the existing answer instead of notifying
        again, while a new query mail (or a changed state) produces a fresh
        report.
        """

        query_mail = self.mail_store.get(query_mail_id)
        fingerprint = str(
            (query_mail or {}).get("raw_hash")
            or (query_mail or {}).get("message_id")
            or query_mail_id
        )
        state_digest = sha256_text(
            json.dumps(
                {
                    "status": str(task["status"]),
                    "version": int(task["version"]),
                    "issues": [
                        [issue.get("code"), issue.get("record_key"), issue.get("field")]
                        for issue in open_issues
                    ],
                    "retry_count": int(task.get("retry_count") or 0),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )[:24]
        dedupe_key = f"status:{task['id']}:{fingerprint[:32]}:{state_digest}"
        recipient = self._recipient(task)
        in_reply_to, references = self._thread_headers(task)
        body = self.status_report_body(task, open_issues)
        task_version = int(task["version"])
        subject = outbound_subject(
            self._original_subject(task), str(task["id"]), task_version
        )
        dry_run = not self._live_allowed(recipient)
        row, created = self.outbox.enqueue(
            dedupe_key=dedupe_key,
            task_id=str(task["id"]),
            version=task_version,
            purpose="status",
            recipient=recipient,
            recipient_name=task.get("owner_name"),
            subject=subject,
            body=body,
            attachments=[],
            in_reply_to=in_reply_to,
            references=references,
            message_id=self._message_id(str(task["id"]), "status", dedupe_key),
            dry_run=dry_run,
        )
        if created:
            self.tasks.add_event(
                str(task["id"]),
                stage="delivery",
                code="status_report_enqueued",
                detail={"outbox_id": row["id"], "dry_run": dry_run, "mail_id": query_mail_id},
            )
            self.logger.info(
                "status_report_enqueued",
                task_id=task["id"],
                outbox_id=row["id"],
                dry_run=dry_run,
                mail_id=query_mail_id,
            )
        return row, created

    # ------------------------------------------------------------------
    # failure notices (technical retry budget exhausted / non-recoverable)
    # ------------------------------------------------------------------
    def failure_notice_body(self, task: dict[str, Any], *, exhausted: bool) -> str:
        if exhausted:
            description = (
                "处理程序已连续多次尝试（含数次自动重试）仍遇到技术故障，"
                "自动重试已用完，本任务暂停处理。"
            )
            saved = "您通过邮件提供的资料已保存，无需重新发送原邮件。"
        else:
            description = (
                "处理程序在第一次尝试时即无法继续处理该任务，未进行自动重试，"
                "本任务暂停处理。"
            )
            saved = "请勿重复发送相同内容的邮件，避免产生重复任务。"
        return "\n".join(
            [
                "您好：",
                "",
                f"任务编号：{task['id']}（第 {int(task['version'])} 版）",
                description,
                "",
                saved,
                "可直接回复本邮件，新增正文单独写：重试",
                "如重试后仍不成功，请联系维护人员核对处理。",
                "",
                "说明：本邮件仅说明处理状态，不含技术细节。",
                f"查询编号：{task['id']}",
            ]
        )

    def enqueue_failure_notice(
        self, task: dict[str, Any], *, episode: int, exhausted: bool
    ) -> tuple[dict[str, Any], bool]:
        recipient = self._recipient(task)
        in_reply_to, references = self._thread_headers(task)
        # ``episode`` (the persisted failure_seq) makes the notice unique per
        # failure episode, so an explicit owner retry that fails again earns
        # exactly one new feedback without repeating old ones.
        dedupe_key = f"failure:{task['id']}:v{int(task['version'])}:{int(episode)}"
        subject = outbound_subject(
            self._original_subject(task), str(task["id"]), int(task["version"])
        )
        dry_run = not self._live_allowed(recipient)
        row, created = self.outbox.enqueue(
            dedupe_key=dedupe_key,
            task_id=str(task["id"]),
            version=int(task["version"]),
            purpose="notice",
            recipient=recipient,
            recipient_name=task.get("owner_name"),
            subject=subject,
            body=self.failure_notice_body(task, exhausted=exhausted),
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
                code="failure_notice_enqueued",
                detail={"outbox_id": row["id"], "dry_run": dry_run, "exhausted": exhausted},
            )
        return row, created

    # ------------------------------------------------------------------
    # taskless guidance (mails that cannot be linked to any task)
    # ------------------------------------------------------------------
    def enqueue_guidance(
        self,
        *,
        recipient: str,
        recipient_name: str | None,
        subject: str,
        body: str,
        in_reply_to: str | None,
        references: list[str],
        dedupe_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Persist generic guidance without a task, honouring send protections.

        Goes through the same outbox/dry-run/allowlist pipeline as task mail;
        it is never dispatched directly through the gateway.
        """

        dry_run = not self._live_allowed(recipient)
        account = self.config.mail.account or "cater.local"
        domain = account.rsplit("@", 1)[-1] if "@" in account else "cater.local"
        token = sha256_text(dedupe_key)[:24]
        row, created = self.outbox.enqueue(
            dedupe_key=dedupe_key,
            task_id=None,
            version=0,
            purpose="guidance",
            recipient=recipient,
            recipient_name=recipient_name,
            subject=_clean_subject(subject) or "操作指引",
            body=body,
            attachments=[],
            in_reply_to=in_reply_to,
            references=references,
            message_id=f"cater.guidance.{token}@{domain}",
            dry_run=dry_run,
        )
        if created:
            self.logger.info(
                "guidance_enqueued",
                outbox_id=row["id"],
                recipient=recipient,
                dry_run=dry_run,
                dedupe_key=dedupe_key,
            )
        return row, created

    # ------------------------------------------------------------------
    # retry-on-waiting answers (one per command mail, not deduped against
    # older questions about the same issues)
    # ------------------------------------------------------------------
    def retry_waiting_body(
        self, task: dict[str, Any], open_issues: list[dict[str, Any]]
    ) -> str:
        lines = [
            "您好：",
            "",
            f"任务编号：{task['id']}（第 {int(task['version'])} 版）",
            "任务正在等待补充资料，暂时不能重试；请先补齐以下内容，",
            "补齐后我们会继续检查并处理，无需再次发送“重试”。",
            "",
        ]
        issue_lines, examples = self._describe_issues(open_issues)
        lines.extend(issue_lines)
        examples = list(dict.fromkeys(examples))
        if examples:
            lines.extend(["", "请直接回复本邮件，按下面格式填写实际内容：", ""])
            lines.extend(examples)
        lines.extend(["", f"查询编号：{task['id']}"])
        return "\n".join(lines)

    def enqueue_retry_waiting_answer(
        self,
        task: dict[str, Any],
        *,
        command_mail_id: str,
        open_issues: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Answer a retry command for a waiting task, deduplicated per mail.

        Older question rows are deduped by issue digest and may never be
        resent; a new active command must still get its own concrete answer.
        """

        command_mail = self.mail_store.get(command_mail_id)
        fingerprint = str(
            (command_mail or {}).get("raw_hash")
            or (command_mail or {}).get("message_id")
            or command_mail_id
        )
        dedupe_key = f"retry-waiting:{task['id']}:{fingerprint[:40]}"
        recipient = self._recipient(task)
        in_reply_to, references = self._thread_headers(task)
        dry_run = not self._live_allowed(recipient)
        row, created = self.outbox.enqueue(
            dedupe_key=dedupe_key,
            task_id=str(task["id"]),
            version=int(task["version"]),
            purpose="notice",
            recipient=recipient,
            recipient_name=task.get("owner_name"),
            subject=outbound_subject(
                self._original_subject(task), str(task["id"]), int(task["version"])
            ),
            body=self.retry_waiting_body(task, open_issues),
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
                code="retry_waiting_answer_enqueued",
                detail={"outbox_id": row["id"], "mail_id": command_mail_id},
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
        layout_report: dict[str, Any] | None = None,
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
        lines.extend(self._layout_lines(layout_report))
        lines.extend(
            [
                "",
                "说明：本邮件仅报告本地文件生成结果，不声称文件已上传任何业务系统。",
                "如数据有误，请使用规范格式回复（附任务编号）。",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _layout_lines(layout_report: dict[str, Any] | None) -> list[str]:
        """布局与核对信息：采用 sheet、结构变化、已执行核对、人工建议。

        结构变化与实际修正分别列出；首次使用新布局重点提示，已确认
        布局不重复告警；普通成功保持简短。核对未执行不称通过。
        """
        if not layout_report:
            return []
        lines: list[str] = []
        sheets = layout_report.get("sheets") or []
        if sheets:
            lines.append(f"采用工作表：{'、'.join(str(s) for s in sheets)}")
        table_columns = layout_report.get("table_columns") or []
        if table_columns:
            lines.append("字段列位（本版采用）：")
            lines.extend(f"- {item}" for item in table_columns)
        changes = layout_report.get("structure_changes") or []
        if changes:
            lines.append("结构变化（相对已确认布局）：")
            lines.extend(f"- {item}" for item in changes)
        checks = layout_report.get("checks") or []
        if checks:
            lines.append("已执行核对：")
            for check in checks:
                if check.get("result") == "pass":
                    mark = "通过"
                elif check.get("result") == "skipped":
                    mark = "未执行（条件不足，跳过）"
                else:
                    mark = "未通过"
                record = check.get("record_key")
                prefix = f"{record} " if record else ""
                lines.append(f"- {prefix}{mark}：{check.get('detail', '')}")
        corrections = layout_report.get("corrections") or []
        if corrections:
            if layout_report.get("corrections_auto"):
                lines.append("实际修正（自动规范化与按您回复的修正项）：")
            else:
                lines.append("实际修正（按您回复的修正项执行）：")
            lines.extend(f"- {item}" for item in corrections)
        if layout_report.get("first_use"):
            lines.append(
                "人工重点核对建议：本次表格布局为首次使用（尚未确认），"
                "请重点核对附件中的数量、净重、单价与原始发票/箱单一致后再上传业务系统；"
                "确认无误后可联系维护人员确认该布局，以后不再重复提示。"
            )
        return lines

    def enqueue_delivery(
        self,
        task: dict[str, Any],
        *,
        record_count: int,
        artifacts: list[dict[str, Any]],
        content_hash: str | None,
        layout_report: dict[str, Any] | None = None,
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
        body = self.delivery_body(task, record_count=record_count, artifacts=artifacts,
                                  layout_report=layout_report)
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
        if claimed.get("task_id"):
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
            task_id=claimed["task_id"],
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
            if failed["task_id"]:
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
            if pending["task_id"]:
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
        task_id = outbox_row.get("task_id")
        if not task_id:
            return  # taskless guidance mail: no task event or transition
        task = self.tasks.require(str(task_id))
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
        if row["task_id"]:
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
        else:
            # Taskless guidance: queue it for a later explicit attempt; the
            # confirmation path never blindsides a resend on its own.
            self.logger.info(
                "taskless_unknown_confirmed_not_sent",
                outbox_id=outbox_id,
                message_id=row["message_id"],
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
        if row["task_id"]:
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
        if row["task_id"]:
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