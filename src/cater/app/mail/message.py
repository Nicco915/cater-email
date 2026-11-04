"""MIME parsing shared by real and simulated mail transports.

Business extraction stays in the core. This module only extracts transport
metadata, body text/HTML and attachments, and classifies automated mail.
"""

from __future__ import annotations

import email
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from ..util import normalize_email, safe_filename

_REFERENCE_RE = re.compile(r"<([^<>\s]+)>")
_AUTO_SUBJECT_PATTERNS = (
    re.compile(r"^\s*(automatic reply|auto[- ]?reply|out of office|out-of-office)\b", re.I),
    re.compile(r"^\s*(自动回复|自動回覆|自动答复)"),
    re.compile(r"^\s*(undeliverable|undelivered mail|mail delivery failed|delivery status notification)\b", re.I),
    re.compile(r"(退信|邮件投递失败|无法投递)"),
)


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    data: bytes
    size: int
    sha256: str
    is_inline: bool = False

    @property
    def suffix(self) -> str:
        return self.filename.rsplit(".", 1)[-1].lower() if "." in self.filename else ""


@dataclass
class ParsedMail:
    sender: str
    sender_name: str | None
    subject: str
    message_id: str | None
    in_reply_to: list[str]
    references: list[str]
    date: str | None
    html_body: str
    text_body: str
    attachments: list[ParsedAttachment]
    headers: dict[str, str] = field(default_factory=dict)
    auto_submitted: str | None = None
    is_auto: bool = False
    is_bounce: bool = False
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def body_text(self) -> str:
        if self.text_body.strip():
            return self.text_body
        return html_to_text(self.html_body)


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "table",
    }
    CELL_TAGS = {"td", "th"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in self.CELL_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts)
        value = value.replace("\xa0", " ")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed HTML must not crash intake; use a crude fallback.
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def _header(message: Message, name: str) -> str:
    value = message.get(name)
    if value is None:
        return ""
    return str(value).strip()


def _message_ids(value: str) -> list[str]:
    if not value:
        return []
    found = _REFERENCE_RE.findall(value)
    if found:
        return [item.strip() for item in found if item.strip()]
    # Some clients omit angle brackets in References.
    return [item.strip() for item in value.split() if "@" in item and item.strip()]


def _payload_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw.encode(part.get_content_charset() or "utf-8", errors="replace")
            # bytes returned below
        return b""
    return payload


def _decode_text(part: Message) -> str:
    data = _payload_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")


def parse_mail(raw: bytes, *, max_attachment_bytes: int | None = None) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    from_header = _header(message, "From")
    addresses = getaddresses([from_header])
    sender = normalize_email(addresses[0][1]) if addresses and addresses[0][1] else ""
    sender_name = (addresses[0][0].strip() or None) if addresses else None
    subject = _header(message, "Subject")
    message_id = _header(message, "Message-ID").strip("<>") or None
    in_reply_to = _message_ids(_header(message, "In-Reply-To"))
    references = _message_ids(_header(message, "References"))
    date_header = _header(message, "Date")
    date_iso: str | None = None
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            date_iso = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            date_iso = None

    html_parts: list[str] = []
    text_parts: list[str] = []
    attachments: list[ParsedAttachment] = []
    warnings: list[str] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        is_attachment = disposition == "attachment" or bool(filename)
        if is_attachment and filename:
            data = _payload_bytes(part)
            name = safe_filename(str(filename), fallback="attachment.bin")
            if max_attachment_bytes is not None and len(data) > max_attachment_bytes:
                warnings.append(f"attachment_too_large:{name}")
            attachments.append(
                ParsedAttachment(
                    filename=name,
                    content_type=content_type,
                    data=data,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    is_inline=disposition == "inline",
                )
            )
            continue
        if content_type == "text/html":
            html_parts.append(_decode_text(part))
        elif content_type == "text/plain":
            text_parts.append(_decode_text(part))

    auto_submitted = _header(message, "Auto-Submitted") or None
    is_auto = bool(auto_submitted and auto_submitted.casefold() != "no")
    if not is_auto:
        if _header(message, "X-Autoreply") or _header(message, "X-Autorespond"):
            is_auto = True
        precedence = _header(message, "Precedence").casefold()
        if precedence in {"bulk", "junk", "auto_reply", "list"}:
            is_auto = True
        if any(pattern.search(subject) for pattern in _AUTO_SUBJECT_PATTERNS):
            is_auto = True

    is_bounce = False
    if sender.startswith(("mailer-daemon@", "postmaster@")):
        is_bounce = True
    lowered_subject = subject.casefold()
    if any(
        marker in lowered_subject
        for marker in ("undeliverable", "undelivered mail", "delivery status notification", "退信", "投递失败")
    ):
        is_bounce = True
    if (
        message.get_content_type().lower() == "multipart/report"
        and "delivery-status" in _header(message, "Content-Type").casefold()
    ):
        is_bounce = True

    headers = {
        name: _header(message, name)
        for name in (
            "From",
            "To",
            "Cc",
            "Subject",
            "Message-ID",
            "In-Reply-To",
            "References",
            "Auto-Submitted",
            "Date",
        )
        if _header(message, name)
    }
    return ParsedMail(
        sender=sender,
        sender_name=sender_name,
        subject=subject,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        date=date_iso,
        html_body="\n".join(html_parts),
        text_body="\n".join(text_parts),
        attachments=attachments,
        headers=headers,
        auto_submitted=auto_submitted,
        is_auto=is_auto,
        is_bounce=is_bounce,
        parse_warnings=warnings,
    )


@dataclass(frozen=True)
class OutboundAttachment:
    path: str
    filename: str
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class OutboundMessage:
    recipient: str
    subject: str
    body: str
    message_id: str
    task_id: str | None  # None for taskless guidance mail
    purpose: str
    attachments: tuple[OutboundAttachment, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class SendReceipt:
    provider_id: str | None
    response: str
    accepted_at: str