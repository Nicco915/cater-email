"""Mail transport protocols and the IMAP/SMTP implementation.

Adapters never parse business data. The IMAP scan is read-only (``BODY.PEEK[]``
and read-only SELECT), so polling never changes the user's read state.
"""

from __future__ import annotations

import imaplib
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Protocol

from ..config import CredentialProvider, MailConfig
from ..errors import MailAuthError, MailTransportError, SendRejectedError, SendUnknownError
from ..util import utc_now
from .message import OutboundAttachment, OutboundMessage, SendReceipt


@dataclass(frozen=True)
class RawMessage:
    account: str
    folder: str
    uid_validity: int | None
    uid: int
    raw: bytes
    size: int


@dataclass(frozen=True)
class ScanResult:
    messages: list[RawMessage]
    uid_validity: int | None
    last_uid: int
    uidvalidity_changed: bool = False
    first_scan: bool = False
    skipped_oversized: tuple[dict[str, int], ...] = ()


class MailGateway(Protocol):
    def scan(
        self,
        account: str,
        folder: str,
        *,
        last_uid: int | None,
        uid_validity: int | None,
        include_history: bool = False,
    ) -> ScanResult: ...

    def send(self, message: OutboundMessage, *, from_address: str) -> SendReceipt: ...

    def find_sent(
        self, account: str, sent_folder: str, message_id: str
    ) -> bool: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------


def encode_imap_mailbox(name: str) -> str:
    """Encode a folder name as IMAP modified UTF-7 when needed."""

    if all(ord(char) < 128 for char in name):
        return name.replace('"', '\\"')
    result: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        encoded = "".join(pending).encode("utf-16-be")
        import base64

        text = base64.b64encode(encoded).decode("ascii").rstrip("=").replace("/", ",")
        result.append(f"&{text}-")
        pending.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E and char != "&":
            flush()
            result.append(char)
        else:
            pending.append(char)
    flush()
    return "".join(result).replace('"', '\\"')


# ---------------------------------------------------------------------------
# RFC 2971 client identification (163-style dedicated mailboxes)
# ---------------------------------------------------------------------------

# Static, truthful client metadata only: never the account, credentials or
# any personal/business data.
IMAP_ID_METADATA = (("name", "CATER"), ("version", "1.0"))


def _advertises_id(capabilities) -> bool:
    """Capabilities may be pre-split atoms, full capability lines or None."""
    if not capabilities:
        return False
    for item in capabilities:
        if isinstance(item, (bytes, bytearray)):
            atoms = bytes(item).split()
        elif isinstance(item, str):
            atoms = item.split()
        else:
            continue
        for atom in atoms:
            if isinstance(atom, bytes):
                if atom.split(b"=", 1)[0].upper() == b"ID":
                    return True
            elif atom.split("=", 1)[0].upper() == "ID":
                return True
    return False


def _bounded_server_text(data, limit: int = 160) -> str:
    """Server diagnostic text: decoded, control chars stripped, length bounded.

    Used so operators can distinguish e.g. an 'Unsafe Login' policy rejection
    from a nonexistent folder, without dumping protocol logs or raw mail.
    """
    if data is None:
        return ""
    if isinstance(data, (bytes, bytearray)):
        chunks = [bytes(data)]
    elif isinstance(data, str):
        chunks = [data.encode("utf-8", "replace")]
    else:
        chunks = [bytes(item) for item in data if isinstance(item, (bytes, bytearray))]
    if not chunks:
        return ""
    text = b" ".join(chunks).decode("utf-8", "replace")
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return " ".join(text.split())[:limit]


def _send_rfc2971_id(connection: imaplib.IMAP4):
    """Send the RFC 2971 ID command without mutating imaplib's command table.

    CPython's imaplib looks up commands in the module-global ``Commands``
    dict inside ``_command``, so registering ``ID`` on the instance (or the
    class) would not take effect; the command line is therefore sent
    directly through the connection's tag machinery.
    """
    pairs = " ".join(f'"{key}" "{value}"' for key, value in IMAP_ID_METADATA)
    line = f"ID ({pairs})".encode("ascii")
    tag = connection._new_tag()
    connection.send(tag + b" " + line + imaplib.CRLF)
    return connection._command_complete("ID", tag)


class ImapGateway:
    def __init__(
        self,
        config: MailConfig,
        credentials: CredentialProvider,
        *,
        timeout: int = 30,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.timeout = timeout

    def _connection(self) -> imaplib.IMAP4:
        if not self.config.imap_host:
            raise MailTransportError("mail.imap_host is not configured")
        context = ssl.create_default_context()
        try:
            if self.config.imap_ssl:
                return imaplib.IMAP4_SSL(
                    self.config.imap_host, self.config.imap_port, timeout=self.timeout, ssl_context=context
                )
            connection = imaplib.IMAP4(
                self.config.imap_host, self.config.imap_port, timeout=self.timeout
            )
            if self.config.imap_starttls:
                connection.starttls(context)
            return connection
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise MailTransportError(f"IMAP connection failed: {exc}") from exc

    def _post_login_handshake(self, connection: imaplib.IMAP4) -> None:
        """RFC 2971 ID handshake after LOGIN, before any folder access.

        163-style dedicated mailboxes reject SELECT/EXAMINE ('Unsafe Login')
        unless the client identifies itself via ID. Servers that never
        advertise ID are untouched (Feishu etc.). A server that advertises
        ID but rejects the command is a hard failure — never silently
        proceed, because folder access would fail with a misleading error.
        """
        if connection.state != "AUTH":
            return
        advertised = _advertises_id(getattr(connection, "capabilities", None))
        if not advertised:
            # 登录后能力集可能变化：部分服务器仅在 AUTH 态通告 ID。
            try:
                status, data = connection.capability()
            except imaplib.IMAP4.error:
                status, data = "NO", None
            if status == "OK":
                advertised = _advertises_id(data)
        if not advertised:
            return
        try:
            status, data = _send_rfc2971_id(connection)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailTransportError(
                "IMAP server requires the RFC 2971 ID handshake, but the ID "
                f"command failed: {_bounded_server_text(str(exc)) or exc}"
            ) from exc
        if status != "OK":
            detail = _bounded_server_text(data)
            raise MailTransportError(
                "IMAP server requires the RFC 2971 ID handshake, but rejected "
                f"the ID command{': ' + detail if detail else ''}"
            )

    def _login(self, connection: imaplib.IMAP4, account: str) -> None:
        """Authenticate exactly once per connection, then run the ID handshake."""
        try:
            connection.login(account, self.credentials.imap_password(account) or "")
        except imaplib.IMAP4.error as exc:
            raise MailAuthError(f"IMAP authentication failed: {exc}") from exc
        self._post_login_handshake(connection)

    def _select(self, connection: imaplib.IMAP4, folder: str) -> tuple[int | None, int]:
        try:
            status, data = connection.select(f'"{encode_imap_mailbox(folder)}"', readonly=True)
        except imaplib.IMAP4.error as exc:
            raise MailTransportError(
                f"Cannot open mail folder {folder!r}: "
                f"{_bounded_server_text(str(exc)) or exc}"
            ) from exc
        if status != "OK":
            # 保留服务器诊断文本（限长去控制符），使 “Unsafe Login” 策略拒绝
            # 与“文件夹不存在”可区分；不做协议级 debug 记录。
            detail = _bounded_server_text(data)
            raise MailTransportError(
                f"Cannot open mail folder {folder!r}{': ' + detail if detail else ''}"
            )
        uidvalidity: int | None = None
        # imaplib 把 tagged OK 里的 [UIDVALIDITY n] 收进 untagged_responses；
        # select() 返回的 data 只是 EXISTS 计数，从中解析不到。
        for item in (connection.untagged_responses.get("UIDVALIDITY") or []):
            if isinstance(item, bytes) and item.strip().isdigit():
                uidvalidity = int(item.strip())
                break
        if uidvalidity is None:
            for item in data or []:
                if isinstance(item, bytes):
                    match = re.search(rb"\[UIDVALIDITY\s+(\d+)\]", item)
                    if match:
                        uidvalidity = int(match.group(1))
                        break
        try:
            status, result = connection.status(
                f'"{encode_imap_mailbox(folder)}"', "(UIDNEXT MESSAGES)"
            )
        except imaplib.IMAP4.error:
            result = []
        # UIDNEXT is a safe watermark for "start from now" because it is the
        # next UID, not an already delivered message.
        return uidvalidity, 0

    def _search_max(self, connection: imaplib.IMAP4) -> int:
        status, data = connection.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return 0
        uids = [int(item) for item in bytes(data[0]).split() if item.isdigit()]
        return max(uids, default=0)

    def _fetch_one(self, connection: imaplib.IMAP4, uid: int) -> bytes | None:
        status, data = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise MailTransportError(f"IMAP fetch failed for UID {uid}")
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    def scan(
        self,
        account: str,
        folder: str,
        *,
        last_uid: int | None,
        uid_validity: int | None,
        include_history: bool = False,
    ) -> ScanResult:
        connection = self._connection()
        try:
            self._login(connection, account)
            selected_validity, _ = self._select(connection, folder)
            changed = (
                uid_validity is not None
                and selected_validity is not None
                and uid_validity != selected_validity
            )
            if last_uid is None and not include_history:
                watermark = self._search_max(connection)
                return ScanResult(
                    messages=[],
                    uid_validity=selected_validity,
                    last_uid=watermark,
                    first_scan=True,
                )
            start = 1 if (last_uid is None or changed) else last_uid + 1
            status, data = connection.uid("SEARCH", None, f"UID {start}:*")
            if status != "OK":
                raise MailTransportError("IMAP UID search failed")
            found = [int(item) for item in bytes(data[0] if data else b"").split() if item.isdigit()]
            found = sorted(uid for uid in found if uid >= start)
            messages: list[RawMessage] = []
            skipped: list[dict[str, int]] = []
            highest = last_uid or 0
            for uid in found:
                try:
                    size_status, size_data = connection.uid("FETCH", str(uid), "(RFC822.SIZE)")
                    size = 0
                    if size_status == "OK" and size_data:
                        match = re.search(rb"RFC822\.SIZE\s+(\d+)", bytes(size_data[0]))
                        if match:
                            size = int(match.group(1))
                    if size and size > self.config.max_message_bytes:
                        skipped.append({"uid": uid, "size": size})
                        highest = max(highest, uid)
                        continue
                    raw = self._fetch_one(connection, uid)
                    if raw is None:
                        skipped.append({"uid": uid, "size": size})
                        highest = max(highest, uid)
                        continue
                    messages.append(
                        RawMessage(
                            account=account,
                            folder=folder,
                            uid_validity=selected_validity,
                            uid=uid,
                            raw=raw,
                            size=len(raw),
                        )
                    )
                    highest = max(highest, uid)
                except imaplib.IMAP4.error as exc:
                    raise MailTransportError(f"IMAP fetch failed: {exc}") from exc
            return ScanResult(
                messages=messages,
                uid_validity=selected_validity,
                last_uid=highest,
                uidvalidity_changed=changed,
                skipped_oversized=tuple(skipped),
            )
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailTransportError(f"IMAP scan failed: {exc}") from exc
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    def find_sent(self, account: str, sent_folder: str, message_id: str) -> bool:
        connection = self._connection()
        try:
            self._login(connection, account)
            status, data = connection.select(
                f'"{encode_imap_mailbox(sent_folder)}"', readonly=True
            )
            if status != "OK":
                detail = _bounded_server_text(data)
                raise MailTransportError(
                    f"Sent folder is unavailable: {sent_folder}"
                    f"{': ' + detail if detail else ''}"
                )
            header = message_id.strip()
            if not header.startswith("<"):
                header = f"<{header}>"
            status, data = connection.uid("SEARCH", None, "HEADER", "Message-ID", header)
            if status != "OK":
                detail = _bounded_server_text(data)
                raise MailTransportError(
                    "Sent-mail search failed"
                    f"{': ' + detail if detail else ''}"
                )
            return bool(data and data[0] and bytes(data[0]).strip())
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------


def _guess_content_type(filename: str) -> str:
    import mimetypes

    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def build_outbound_email(message: OutboundMessage, *, from_address: str) -> EmailMessage:
    email_message = EmailMessage()
    email_message["From"] = from_address
    email_message["To"] = message.recipient
    email_message["Subject"] = message.subject
    email_message["Message-ID"] = (
        message.message_id if message.message_id.startswith("<") else f"<{message.message_id}>"
    )
    email_message["Date"] = format_datetime(datetime.now(timezone.utc))
    email_message["Auto-Submitted"] = "auto-generated"
    if message.task_id:
        email_message["X-CATER-Task"] = message.task_id
    email_message["X-CATER-Purpose"] = message.purpose
    if message.in_reply_to:
        email_message["In-Reply-To"] = (
            message.in_reply_to
            if message.in_reply_to.startswith("<")
            else f"<{message.in_reply_to}>"
        )
    if message.references:
        email_message["References"] = " ".join(
            item if item.startswith("<") else f"<{item}>" for item in message.references
        )
    email_message.set_content(message.body)
    for attachment in message.attachments:
        path = Path(attachment.path)
        data = path.read_bytes()
        email_message.add_attachment(
            data,
            maintype=(attachment.content_type or _guess_content_type(attachment.filename)).split("/")[0],
            subtype=(attachment.content_type or _guess_content_type(attachment.filename)).split("/")[-1],
            filename=attachment.filename,
        )
    return email_message


class SmtpGateway:
    def __init__(
        self,
        config: MailConfig,
        credentials: CredentialProvider,
        *,
        timeout: int | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.timeout = timeout or config.smtp_timeout_seconds

    def send(self, message: OutboundMessage, *, from_address: str) -> SendReceipt:
        if not self.config.smtp_host:
            raise MailTransportError("mail.smtp_host is not configured")
        mime = build_outbound_email(message, from_address=from_address)
        context = ssl.create_default_context()
        client: smtplib.SMTP | None = None
        sent_started = False
        try:
            if self.config.smtp_ssl:
                client = smtplib.SMTP_SSL(
                    self.config.smtp_host,
                    self.config.smtp_port,
                    timeout=self.timeout,
                    context=context,
                )
            else:
                client = smtplib.SMTP(
                    self.config.smtp_host, self.config.smtp_port, timeout=self.timeout
                )
                if self.config.smtp_starttls:
                    client.starttls(context=context)
            try:
                client.login(from_address, self.credentials.smtp_password(from_address) or "")
            except smtplib.SMTPAuthenticationError as exc:
                raise MailAuthError("SMTP authentication failed") from exc
            sent_started = True
            refused = client.sendmail(
                from_address, [message.recipient], mime.as_bytes()
            )
            if refused:
                raise SendRejectedError(f"SMTP rejected recipient: {message.recipient}")
            return SendReceipt(
                provider_id=None,
                response="accepted",
                accepted_at=utc_now(),
            )
        except (SendRejectedError, MailAuthError):
            raise
        except smtplib.SMTPRecipientsRefused as exc:
            raise SendRejectedError("SMTP rejected the recipient") from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise MailAuthError("SMTP authentication failed") from exc
        except (smtplib.SMTPConnectError, smtplib.SMTPHeloError, OSError) as exc:
            if sent_started:
                raise SendUnknownError(
                    "SMTP connection was lost after the message data was submitted"
                ) from exc
            raise MailTransportError(f"SMTP connection failed: {exc}") from exc
        except (smtplib.SMTPException, TimeoutError, ConnectionError) as exc:
            if sent_started:
                raise SendUnknownError("SMTP result is uncertain") from exc
            raise MailTransportError(f"SMTP send failed: {exc}") from exc
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass

    def scan(self, *args, **kwargs):  # pragma: no cover - protocol completeness
        raise NotImplementedError("SMTP gateway does not scan")

    def find_sent(self, account: str, sent_folder: str, message_id: str) -> bool:
        return ImapGateway(self.config, self.credentials).find_sent(
            account, sent_folder, message_id
        )

    def close(self) -> None:
        return None


class CompositeGateway:
    """IMAP for intake and sent-mail checks, SMTP for delivery."""

    def __init__(self, config: MailConfig, credentials: CredentialProvider) -> None:
        self.imap = ImapGateway(config, credentials)
        self.smtp = SmtpGateway(config, credentials)

    def scan(self, *args, **kwargs) -> ScanResult:
        return self.imap.scan(*args, **kwargs)

    def send(self, message: OutboundMessage, *, from_address: str) -> SendReceipt:
        return self.smtp.send(message, from_address=from_address)

    def find_sent(self, account: str, sent_folder: str, message_id: str) -> bool:
        return self.imap.find_sent(account, sent_folder, message_id)

    def close(self) -> None:
        self.imap.close()
        self.smtp.close()