"""Application error types with stable codes for logs and task events."""

from __future__ import annotations


class CaterError(Exception):
    code = "CATER_ERROR"


class ConfigurationError(CaterError):
    code = "CONFIGURATION_ERROR"


class CredentialError(CaterError):
    code = "CREDENTIAL_ERROR"


class MailTransportError(CaterError):
    code = "MAIL_TRANSPORT_ERROR"


class MailAuthError(MailTransportError):
    code = "MAIL_AUTH_ERROR"


class SendRejectedError(CaterError):
    code = "SEND_REJECTED"


class SendUnknownError(CaterError):
    """The connection failed after a message may have been accepted."""

    code = "SEND_UNKNOWN"


class SendNotAllowedError(CaterError):
    code = "SEND_NOT_ALLOWED"


class PathSafetyError(CaterError):
    code = "UNSAFE_PATH"


class StateTransitionError(CaterError):
    code = "INVALID_STATE_TRANSITION"


class ValidationError(CaterError):
    code = "VALIDATION_ERROR"


class DuplicateMessage(CaterError):
    code = "DUPLICATE_MESSAGE"


class UnsupportedInput(CaterError):
    code = "UNSUPPORTED_INPUT"


class BatchDisallowedError(CaterError):
    """A batch lifecycle action had disallowed items; nothing was applied.

    Carries the per-item reasons so the UI can list them before the operator
    retries. The whole batch is rejected: there is never a partial apply.
    """

    code = "BATCH_DISALLOWED"

    def __init__(self, items: list[dict[str, str]]) -> None:
        self.items = items
        super().__init__(
            "部分任务不允许该操作，已全部撤销："
            + "; ".join(f"{i['task_id']} ({i['reason']})" for i in items)
        )