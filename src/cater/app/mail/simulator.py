"""Fully offline mail gateway used by tests and local demonstrations."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import SendRejectedError, SendUnknownError
from ..util import utc_now
from .gateway import RawMessage, ScanResult
from .message import OutboundMessage, SendReceipt


@dataclass
class _StoredMessage:
    uid: int
    raw: bytes
    folder: str


@dataclass
class SimulatorMailGateway:
    """Deterministic in-memory IMAP + SMTP simulator.

    ``fail_mode`` controls the next and subsequent sends:

    * ``accept`` – accepted and visible in the simulated Sent folder
    * ``reject`` – SMTP rejects before acceptance
    * ``unknown_after_accept`` – accepted, but the client loses the response
    * ``unknown_before_accept`` – connection lost before acceptance

    Real network code is not involved in any mode.
    """

    account: str = "test@example.com"
    folder: str = "INBOX"
    sent_folder: str = "Sent"
    fail_mode: str = "accept"
    uid_validity: int = 1000
    _messages: list[_StoredMessage] = field(default_factory=list)
    _next_uid: int = 1
    sent: list[OutboundMessage] = field(default_factory=list)
    send_attempts: int = 0

    # ----- inbound -----------------------------------------------------
    def add_raw(self, raw: bytes, *, folder: str | None = None) -> int:
        uid = self._next_uid
        self._next_uid += 1
        self._messages.append(_StoredMessage(uid=uid, raw=raw, folder=folder or self.folder))
        return uid

    def add_message(self, raw: bytes, *, folder: str | None = None) -> int:
        return self.add_raw(raw, folder=folder)

    def change_uid_validity(self, value: int) -> None:
        self.uid_validity = value

    def scan(
        self,
        account: str,
        folder: str,
        *,
        last_uid: int | None,
        uid_validity: int | None,
        include_history: bool = False,
    ) -> ScanResult:
        del account  # single configured account in the simulator
        folder_messages = [item for item in self._messages if item.folder == folder]
        changed = uid_validity is not None and uid_validity != self.uid_validity
        if last_uid is None and not include_history:
            return ScanResult(
                messages=[],
                uid_validity=self.uid_validity,
                last_uid=max((item.uid for item in folder_messages), default=0),
                first_scan=True,
            )
        if last_uid is None or changed:
            selected = folder_messages
        else:
            selected = [item for item in folder_messages if item.uid > last_uid]
        messages = [
            RawMessage(
                account=self.account,
                folder=folder,
                uid_validity=self.uid_validity,
                uid=item.uid,
                raw=item.raw,
                size=len(item.raw),
            )
            for item in sorted(selected, key=lambda item: item.uid)
        ]
        highest = max(
            [last_uid or 0]
            + [item.uid for item in folder_messages]
        )
        return ScanResult(
            messages=messages,
            uid_validity=self.uid_validity,
            last_uid=highest,
            uidvalidity_changed=changed,
        )

    # ----- outbound ----------------------------------------------------
    def send(self, message: OutboundMessage, *, from_address: str) -> SendReceipt:
        del from_address
        self.send_attempts += 1
        if self.fail_mode == "reject":
            raise SendRejectedError("simulated SMTP rejection")
        if self.fail_mode == "unknown_before_accept":
            raise SendUnknownError("simulated connection loss before acceptance")
        self.sent.append(message)
        if self.fail_mode == "unknown_after_accept":
            raise SendUnknownError("simulated connection loss after acceptance")
        return SendReceipt(provider_id="simulated", response="accepted", accepted_at=utc_now())

    def find_sent(self, account: str, sent_folder: str, message_id: str) -> bool:
        del account, sent_folder
        normalized = message_id.strip().strip("<>")
        return any(
            item.message_id.strip().strip("<>") == normalized for item in self.sent
        )

    def close(self) -> None:
        return None

    def reset_sent(self) -> None:
        self.sent.clear()
        self.send_attempts = 0