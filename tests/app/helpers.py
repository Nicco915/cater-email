"""Shared offline helpers for the application test suite."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

from cater.app.config import (
    AllowlistConfig,
    AppConfig,
    LoggingConfig,
    MailConfig,
    PathsConfig,
    SendingConfig,
    WebConfig,
)
from cater.app.engine import CoreUnavailable
from cater.app.mail.gateway import RawMessage


@dataclass
class FakeEngine:
    """Deterministic stand-in implementing the public ``process_job`` contract."""

    mode: str = "ready"
    calls: list[dict[str, Any]] = field(default_factory=list)
    content_hash: str = "content-hash-ready"
    business_identity: str | None = None
    artifact_count: int = 1
    layout: dict[str, Any] | None = None
    records: list[dict[str, Any]] = field(
        default_factory=lambda: [{"record_key": "P1", "part_code": "P1", "qty": 1}]
    )

    def process_job(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(request)
        output_dir = Path(request["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        if self.mode == "core_unavailable":
            raise CoreUnavailable("core not installed in this test")
        if self.mode == "waiting":
            return {
                "status": "waiting_input",
                "kind": "parts",
                "records": [],
                "issues": [
                    {
                        "code": "MISSING_QTY",
                        "record_key": "P1",
                        "field": "qty",
                        "message": "缺少数量",
                        "sources": [],
                        "required_action": "补充 qty",
                    }
                ],
                "artifacts": [],
                "provenance": [],
                "business_identity": None,
                "content_hash": "hash-waiting",
            }
        if self.mode == "unsupported":
            return {
                "status": "unsupported",
                "kind": "unsupported",
                "records": [],
                "issues": [
                    {
                        "code": "UNSUPPORTED_BUSINESS",
                        "record_key": None,
                        "field": None,
                        "message": "无法识别业务类型",
                        "sources": [],
                        "required_action": "提供配件发票或整机资料",
                        "blocking": True,
                    }
                ],
                "artifacts": [],
                "provenance": [],
                "business_identity": None,
                "content_hash": "hash-unsupported",
            }

        artifacts = []
        for index in range(self.artifact_count):
            name = "result.xlsx" if index == 0 else f"result-{index + 1}.xlsx"
            payload = f"fake-xlsx-{index}".encode()
            path = output_dir / name
            path.write_bytes(payload)
            artifacts.append(
                {
                    "path": str(path),
                    "filename": name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "kind": "ledger",
                }
            )
        return {
            "status": "ready",
            "kind": "machine" if self.artifact_count > 1 else "parts",
            "records": self.records,
            "issues": [],
            "artifacts": artifacts,
            "provenance": [
                {
                    "record_key": "P1",
                    "field": "qty",
                    "value": 1,
                    "source_kind": "mail",
                    "source_ref": {},
                    "mail_id": None,
                }
            ],
            "business_identity": self.business_identity,
            "content_hash": self.content_hash,
            "layout": self.layout,
        }


def make_config(
    tmp_path: Path,
    *,
    dry_run: bool = True,
    enabled: bool = False,
    mode: str = "test",
    test_addresses: tuple[str, ...] = ("tester@example.com",),
    production_addresses: tuple[str, ...] = (),
    first_run_new_only: bool = True,
) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(runtime_dir=tmp_path / "runtime"),
        mail=MailConfig(
            account="bot@example.com",
            folder="INBOX",
            sent_folder="Sent",
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            first_run_new_only=first_run_new_only,
        ),
        allowlist=AllowlistConfig(
            mode=mode,
            test=test_addresses,
            production=production_addresses,
        ),
        sending=SendingConfig(dry_run=dry_run, enabled=enabled),
        web=WebConfig(host="127.0.0.1", port=8765),
        logging=LoggingConfig(),
    )


def make_mail(
    *,
    sender: str = "tester@example.com",
    sender_name: str | None = "Tester",
    subject: str = "配件发票",
    text: str = "目的国：法国",
    html: str | None = None,
    attachments: tuple[tuple[str, bytes], ...] = (),
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    auto_submitted: str | None = None,
    date: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    message["To"] = "bot@example.com"
    message["Subject"] = subject
    message["Date"] = date or formatdate(localtime=False)
    message["Message-ID"] = message_id or make_msgid(domain="example.com")
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    if auto_submitted:
        message["Auto-Submitted"] = auto_submitted
    message.set_content(text)
    if html is not None:
        message.add_alternative(html, subtype="html")
    for entry in attachments:
        filename, data = entry[0], entry[1]
        if len(entry) > 2 and entry[2]:
            maintype, subtype = entry[2].split("/", 1)
        else:
            lower = filename.lower()
            if lower.endswith(".xlsx"):
                maintype, subtype = (
                    "application",
                    "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            elif lower.endswith(".xls"):
                maintype, subtype = "application", "vnd.ms-excel"
            else:
                maintype, subtype = "application", "octet-stream"
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def raw_message(
    raw: bytes,
    *,
    uid: int = 1,
    account: str = "bot@example.com",
    folder: str = "INBOX",
    uid_validity: int = 1000,
) -> RawMessage:
    return RawMessage(
        account=account,
        folder=folder,
        uid_validity=uid_validity,
        uid=uid,
        raw=raw,
        size=len(raw),
    )