"""§D command lifecycle states.

A command moves through the states:

    PENDING → VALIDATED → ISSUED → DELIVERED → EXECUTING
                                                       │
                                                       ├── COMPLETED  (success)
                                                       ├── FAILED     (executor error)
                                                       ├── CANCELLED  (operator abort)
                                                       └── TIMED_OUT  (deadline)

The state machine is forward-only. Backwards transitions are invalid
(once a command is ISSUED, it can't go back to PENDING). The four
terminal states have no outgoing transitions.

String values are part of the audit-log wire format and must remain
stable. Renaming a value breaks every persisted audit entry; the
``test_status_string_value_is_stable`` test enforces this.
"""
from __future__ import annotations

from enum import Enum


class CommandStatus(str, Enum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    ISSUED = "ISSUED"
    DELIVERED = "DELIVERED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


# Canonical forward order. The state machine validates transitions
# by checking the source's index + 1 == the target's index in this
# list. Terminal states are excluded from this order (they have no
# outgoing transitions).
STATUS_ORDER: tuple[CommandStatus, ...] = (
    CommandStatus.PENDING,
    CommandStatus.VALIDATED,
    CommandStatus.ISSUED,
    CommandStatus.DELIVERED,
    CommandStatus.EXECUTING,
)


# Cancellation and timeout are reachable from any non-terminal state.
# This is the "abort" path — the operator changes their mind, or the
# device hits the deadline.
_ABORT_TRANSITIONS: frozenset[CommandStatus] = frozenset(
    {CommandStatus.CANCELLED, CommandStatus.TIMED_OUT}
)


def terminal_statuses() -> frozenset[CommandStatus]:
    """The four terminal states. A command that has reached one of
    these cannot transition anywhere else (no going from COMPLETED
    back to EXECUTING, no FAILED → CANCELLED, etc.)."""
    return frozenset(
        {CommandStatus.COMPLETED, CommandStatus.FAILED,
         CommandStatus.CANCELLED, CommandStatus.TIMED_OUT}
    )


def is_valid_transition(frm: CommandStatus, to: CommandStatus) -> bool:
    """Return True if the transition ``frm -> to`` is allowed.

    Forward path: each step in :data:`STATUS_ORDER` is valid.
    Abort path: from any non-terminal state to CANCELLED or TIMED_OUT.
    Same-state: NOT valid (idempotent transitions are a no-op at the
    caller; the state machine refuses them so the audit log doesn't
    see no-op events).
    """
    if frm == to:
        return False
    if frm in terminal_statuses():
        return False
    if to in terminal_statuses():
        if to in _ABORT_TRANSITIONS:
            # CANCELLED / TIMED_OUT reachable from any non-terminal.
            return True
        # COMPLETED / FAILED: only reachable from EXECUTING.
        return frm is CommandStatus.EXECUTING and to in (
            CommandStatus.COMPLETED, CommandStatus.FAILED,
        )
    # Non-terminal -> non-terminal: must be the next step in
    # STATUS_ORDER.
    if frm not in STATUS_ORDER or to not in STATUS_ORDER:
        return False
    return STATUS_ORDER.index(to) == STATUS_ORDER.index(frm) + 1
