"""Mail transport, MIME parsing and simulator exports."""

from .gateway import (
    CompositeGateway,
    ImapGateway,
    MailGateway,
    RawMessage,
    ScanResult,
    SmtpGateway,
    build_outbound_email,
)
from .message import (
    OutboundAttachment,
    OutboundMessage,
    ParsedAttachment,
    ParsedMail,
    SendReceipt,
    html_to_text,
    parse_mail,
)
from .simulator import SimulatorMailGateway

__all__ = [
    "CompositeGateway",
    "ImapGateway",
    "MailGateway",
    "RawMessage",
    "ScanResult",
    "SmtpGateway",
    "build_outbound_email",
    "OutboundAttachment",
    "OutboundMessage",
    "ParsedAttachment",
    "ParsedMail",
    "SendReceipt",
    "html_to_text",
    "parse_mail",
    "SimulatorMailGateway",
]