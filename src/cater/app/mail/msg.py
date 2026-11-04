"""Offline Outlook ``.msg`` parsing.

The plan requires offline ``.msg`` support without Outlook GUI automation. We
use the dedicated ``extract-msg`` library only in this adapter; the rest of the
application keeps working with the neutral :class:`ParsedMail` shape.

``extract-msg`` is an optional dependency: importing this module when it is not
installed raises a clear error instead of breaking the whole application.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from ..util import normalize_email, safe_filename
from .message import ParsedAttachment, ParsedMail, html_to_text


class MsgParseError(RuntimeError):
    pass


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _filename(attachment: object) -> str | None:
    for attribute in ("longFilename", "shortFilename", "name", "displayName"):
        value = getattr(attachment, attribute, None)
        if value:
            return str(value)
    return None


def _attachment_bytes(attachment: object) -> bytes:
    data = getattr(attachment, "data", None)
    if isinstance(data, bytes):
        return data
    if callable(getattr(attachment, "getData", None)):
        value = attachment.getData()
        return value if isinstance(value, bytes) else bytes(value)
    raise MsgParseError("Attachment has no readable data")


def parse_msg(path: str | Path, *, max_attachment_bytes: int | None = None) -> ParsedMail:
    """Read a ``.msg`` file into the application's transport-neutral shape."""

    source = Path(path)
    try:
        import extract_msg
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise MsgParseError(
            "extract-msg is required for .msg files; install project dependencies"
        ) from exc

    try:
        message = extract_msg.Message(str(source))
    except Exception as exc:
        raise MsgParseError(f"Cannot read .msg file {source.name}: {exc}") from exc

    try:
        sender_value = message.sender or ""
        # Sender may be "Display Name <mail@example.com>" or just an address.
        sender = ""
        sender_name = None
        if "<" in sender_value and ">" in sender_value:
            sender_name = sender_value.split("<", 1)[0].strip().strip('"') or None
            sender = normalize_email(sender_value.split("<", 1)[1].split(">", 1)[0])
        else:
            sender = normalize_email(sender_value)
        subject = message.subject or ""
        message_id_value = getattr(message, "messageId", None)
        message_id = str(message_id_value).strip("<>") if message_id_value else None
        html_body = message.htmlBody or ""
        if isinstance(html_body, bytes):
            html_body = html_body.decode("utf-8", errors="replace")
        text_body = message.body or ""
        if isinstance(text_body, bytes):
            text_body = text_body.decode("utf-8", errors="replace")

        attachments: list[ParsedAttachment] = []
        warnings: list[str] = []
        for attachment in message.attachments or []:
            name = safe_filename(_filename(attachment) or "attachment.bin")
            try:
                data = _attachment_bytes(attachment)
            except Exception as exc:
                warnings.append(f"attachment_unreadable:{name}")
                continue
            if max_attachment_bytes is not None and len(data) > max_attachment_bytes:
                warnings.append(f"attachment_too_large:{name}")
                continue
            attachments.append(
                ParsedAttachment(
                    filename=name,
                    content_type=_guess_type(name),
                    data=data,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )

        headers = {"From": sender_value, "Subject": subject}
        return ParsedMail(
            sender=sender,
            sender_name=sender_name,
            subject=subject,
            message_id=message_id,
            in_reply_to=[],
            references=[],
            date=_iso(getattr(message, "date", None)),
            html_body=html_body,
            text_body=text_body or html_to_text(html_body),
            attachments=attachments,
            headers=headers,
            parse_warnings=warnings,
        )
    finally:
        close = getattr(message, "close", None)
        if callable(close):
            close()


def _guess_type(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"