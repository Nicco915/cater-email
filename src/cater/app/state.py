"""Task status values and the explicit transition table."""

from __future__ import annotations

RECEIVED = "RECEIVED"
VALIDATING = "VALIDATING"
PROCESSING = "PROCESSING"
READY_TO_SEND = "READY_TO_SEND"
COMPLETED = "COMPLETED"
WAITING_INPUT = "WAITING_INPUT"
RETRY_PENDING = "RETRY_PENDING"
FAILED = "FAILED"
SEND_UNKNOWN = "SEND_UNKNOWN"
CANCELLED = "CANCELLED"

ALL_STATUSES = frozenset(
    {
        RECEIVED,
        VALIDATING,
        PROCESSING,
        READY_TO_SEND,
        COMPLETED,
        WAITING_INPUT,
        RETRY_PENDING,
        FAILED,
        SEND_UNKNOWN,
        CANCELLED,
    }
)

# Deliberately explicit: no generic set_status escape hatch.
TRANSITIONS: dict[str, frozenset[str]] = {
    # RETRY_PENDING covers a crash between task creation and the first processing
# attempt; startup recovery moves such rows there for normal retry handling.
    RECEIVED: frozenset({VALIDATING, RETRY_PENDING, FAILED, CANCELLED}),
    VALIDATING: frozenset({PROCESSING, WAITING_INPUT, RETRY_PENDING, FAILED, CANCELLED}),
    PROCESSING: frozenset(
        {READY_TO_SEND, WAITING_INPUT, RETRY_PENDING, FAILED, COMPLETED, CANCELLED}
    ),
    READY_TO_SEND: frozenset(
        {COMPLETED, SEND_UNKNOWN, FAILED, CANCELLED, PROCESSING}
    ),
    WAITING_INPUT: frozenset({PROCESSING, FAILED, CANCELLED}),
    RETRY_PENDING: frozenset({PROCESSING, FAILED, CANCELLED}),
    FAILED: frozenset({PROCESSING, CANCELLED}),
    SEND_UNKNOWN: frozenset({COMPLETED, READY_TO_SEND, FAILED}),
    # COMPLETED -> PROCESSING is possible only after an explicit, audited
    # "reprocess" admin action that first creates a new task version.
    COMPLETED: frozenset({PROCESSING}),
    CANCELLED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def require_transition(current: str, target: str) -> None:
    from .errors import StateTransitionError

    if not can_transition(current, target):
        raise StateTransitionError(f"Cannot move task from {current} to {target}")


TERMINAL = frozenset({COMPLETED, CANCELLED})
ACTIVE = frozenset(
    {RECEIVED, VALIDATING, PROCESSING, READY_TO_SEND, WAITING_INPUT, RETRY_PENDING, SEND_UNKNOWN}
)