"""Tests for the CommandStatus enum and its lifecycle ordering.

A command moves through the states:

    PENDING → VALIDATED → ISSUED → DELIVERED → EXECUTING
                                                       │
                                                       ├── COMPLETED  (success)
                                                       ├── FAILED     (executor error)
                                                       ├── CANCELLED  (operator abort)
                                                       └── TIMED_OUT  (deadline)

A well-formed lifecycle is a single forward path. Backwards
transitions are invalid (no going from EXECUTING back to ISSUED). The
state machine enforces this at the dispatcher's ``transition`` method
(Phase 5) and the audit log records every transition.
"""
from __future__ import annotations

import pytest

from opensapien_server.commands.status import (
    CommandStatus,
    STATUS_ORDER,
    is_valid_transition,
    terminal_statuses,
)


def test_status_enum_has_all_eight_values():
    # Binding: 8 statuses (4 happy-path + 4 terminal failure modes).
    # Tests fail if a status is added or removed — that's a contract
    # change that ripples through the audit log, dispatch, and Android
    # command-lifecycle UI.
    assert {s.value for s in CommandStatus} == {
        "PENDING",
        "VALIDATED",
        "ISSUED",
        "DELIVERED",
        "EXECUTING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
    }


def test_status_order_starts_at_pending_and_ends_at_terminal():
    # The canonical lifecycle order. The state machine uses this to
    # validate transitions.
    assert STATUS_ORDER[0] is CommandStatus.PENDING
    assert terminal_statuses() == {
        CommandStatus.COMPLETED,
        CommandStatus.FAILED,
        CommandStatus.CANCELLED,
        CommandStatus.TIMED_OUT,
    }


def test_terminal_statuses_have_no_outgoing_transitions():
    """A command that has COMPLETED cannot go anywhere; same for
    FAILED, CANCELLED, TIMED_OUT. This is the load-bearing invariant
    of the lifecycle — once we're done, we're done."""
    for status in terminal_statuses():
        for other in CommandStatus:
            assert not is_valid_transition(status, other), (
                f"{status} -> {other} should be invalid (terminal)"
            )


def test_valid_forward_transitions():
    """The canonical forward path. Each transition in the order
    is valid."""
    for i in range(len(STATUS_ORDER) - 1):
        frm = STATUS_ORDER[i]
        to = STATUS_ORDER[i + 1]
        assert is_valid_transition(frm, to), f"{frm} -> {to} should be valid"


def test_invalid_skip_forward_transition():
    """PENDING → ISSUED (skipping VALIDATED) is invalid. Skipping
    states breaks the audit trail."""
    assert not is_valid_transition(CommandStatus.PENDING, CommandStatus.ISSUED)
    assert not is_valid_transition(CommandStatus.VALIDATED, CommandStatus.DELIVERED)
    assert not is_valid_transition(CommandStatus.ISSUED, CommandStatus.EXECUTING)


def test_invalid_backwards_transition():
    assert not is_valid_transition(CommandStatus.ISSUED, CommandStatus.PENDING)
    assert not is_valid_transition(CommandStatus.EXECUTING, CommandStatus.ISSUED)
    assert not is_valid_transition(CommandStatus.COMPLETED, CommandStatus.EXECUTING)


def test_valid_transitions_to_terminal_from_executing():
    """EXECUTING is the last non-terminal state. From here, the
    command either completes, fails, gets cancelled, or times out.
    All four are valid."""
    for terminal in terminal_statuses():
        assert is_valid_transition(CommandStatus.EXECUTING, terminal), (
            f"EXECUTING -> {terminal} should be valid"
        )


def test_valid_cancellation_paths():
    """Cancellation is valid from any non-terminal state."""
    for state in (CommandStatus.PENDING, CommandStatus.VALIDATED,
                 CommandStatus.ISSUED, CommandStatus.DELIVERED,
                 CommandStatus.EXECUTING):
        assert is_valid_transition(state, CommandStatus.CANCELLED), (
            f"{state} -> CANCELLED should be valid"
        )


def test_valid_timeout_paths():
    """Timeout is valid from any non-terminal state (the device has
    no power to ack within the deadline)."""
    for state in (CommandStatus.PENDING, CommandStatus.VALIDATED,
                 CommandStatus.ISSUED, CommandStatus.DELIVERED,
                 CommandStatus.EXECUTING):
        assert is_valid_transition(state, CommandStatus.TIMED_OUT), (
            f"{state} -> TIMED_OUT should be valid"
        )


def test_terminal_states_cannot_transition_to_each_other():
    """COMPLETED -> FAILED is invalid: a command that succeeded
    cannot later fail. Same for any other terminal pair."""
    for a in terminal_statuses():
        for b in terminal_statuses():
            if a == b:
                continue
            assert not is_valid_transition(a, b), (
                f"{a} -> {b} should be invalid (terminal to terminal)"
            )


def test_status_string_value_is_stable():
    """The string value of each status is part of the audit-log wire
    format. Renaming a value would break persisted audit entries
    forever. This test fails if anyone reorders or renames a value.
    """
    assert CommandStatus.PENDING.value == "PENDING"
    assert CommandStatus.VALIDATED.value == "VALIDATED"
    assert CommandStatus.ISSUED.value == "ISSUED"
    assert CommandStatus.DELIVERED.value == "DELIVERED"
    assert CommandStatus.EXECUTING.value == "EXECUTING"
    assert CommandStatus.COMPLETED.value == "COMPLETED"
    assert CommandStatus.FAILED.value == "FAILED"
    assert CommandStatus.CANCELLED.value == "CANCELLED"
    assert CommandStatus.TIMED_OUT.value == "TIMED_OUT"


def test_status_string_round_trips():
    """CommandStatus(value) must succeed for every defined value."""
    for s in CommandStatus:
        assert CommandStatus(s.value) is s
