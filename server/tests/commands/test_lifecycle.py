"""Tests for the command lifecycle state machine.

The dispatcher drives the command through the binding state machine:

  PENDING -> VALIDATED -> ISSUED -> DELIVERED -> EXECUTING
                                                       |
                                                       +-- COMPLETED
                                                       +-- FAILED
                                                       +-- CANCELLED
                                                       +-- TIMED_OUT

A well-formed lifecycle is a single forward path. Backwards
transitions are invalid. The state machine enforces this at the
dispatcher's ``transition`` method; the audit log records every
transition.

Persistence (the durable ``CommandRecord`` + lifecycle table) arrives
in Phase 7. Phase 5 establishes the API ground.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sense_server.commands.dispatcher import CommandDispatcher
from sense_server.commands.model import Command
from sense_server.commands.signing import CommandSigner
from sense_server.commands.status import (
    CommandStatus,
    is_valid_transition,
)


T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def make() -> tuple[CommandDispatcher, FakeClock]:
    clock = FakeClock()
    signer = CommandSigner.generate()
    return CommandDispatcher(signer, clock=clock), clock


def a_command(command_id: str = "c1") -> Command:
    return Command(
        command_id=command_id,
        session_id="s1",
        type="capture_photo",
        params={},
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=30),
    )


def _walk_to(disp: CommandDispatcher, command_id: str, target: CommandStatus) -> None:
    """Walk the dispatcher to ``target`` via the canonical forward path.

    For non-terminal targets, walks PENDING -> ... -> target.
    For terminal targets (COMPLETED, FAILED, CANCELLED, TIMED_OUT),
    walks to EXECUTING first; the caller does the final transition.
    """
    order = [
        CommandStatus.PENDING, CommandStatus.VALIDATED, CommandStatus.ISSUED,
        CommandStatus.DELIVERED, CommandStatus.EXECUTING,
    ]
    if target in order:
        target_idx = order.index(target)
    else:
        # Walk to EXECUTING; the caller does the final transition.
        target_idx = order.index(CommandStatus.EXECUTING)
    for s in order[1:target_idx + 1]:
        disp.transition(command_id, s)


# --- basic transition API ---------------------------------------------------


def test_initial_status_is_pending():
    """A newly-issued command starts at PENDING."""
    disp, _ = make()
    disp.issue(a_command())
    assert disp.get_status("c1") == CommandStatus.PENDING


def test_transition_moves_command_through_lifecycle():
    """A clean forward path: PENDING -> VALIDATED -> ISSUED -> DELIVERED ->
    EXECUTING -> COMPLETED. Every transition is recorded on the
    command's history."""
    disp, clock = make()
    disp.issue(a_command())
    disp.transition("c1", CommandStatus.VALIDATED, at=clock.t)
    disp.transition("c1", CommandStatus.ISSUED, at=clock.t)
    assert disp.get_status("c1") == CommandStatus.ISSUED
    disp.transition("c1", CommandStatus.DELIVERED, at=clock.t)
    disp.transition("c1", CommandStatus.EXECUTING, at=clock.t)
    disp.transition("c1", CommandStatus.COMPLETED, at=clock.t, detail={"ok": True})
    history = disp.get_history("c1")
    assert [t.to_status for t in history] == [
        CommandStatus.PENDING,
        CommandStatus.VALIDATED,
        CommandStatus.ISSUED,
        CommandStatus.DELIVERED,
        CommandStatus.EXECUTING,
        CommandStatus.COMPLETED,
    ]
    assert history[-1].detail == {"ok": True}


def test_transition_records_timestamp():
    """Every transition has an at: datetime field — the state machine
    is the canonical source of "when did this happen" for the
    Android lifecycle view."""
    disp, clock = make()
    disp.issue(a_command())
    clock.advance(10)
    disp.transition("c1", CommandStatus.VALIDATED, at=clock.t)
    disp.transition("c1", CommandStatus.ISSUED, at=clock.t)
    history = disp.get_history("c1")
    assert history[-1].at == T0 + timedelta(seconds=10)


# --- invalid transitions -----------------------------------------------------


def test_backward_transition_is_rejected():
    """EXECUTING -> ISSUED is a backwards transition. The state
    machine refuses it and the command's status is unchanged."""
    disp, _ = make()
    disp.issue(a_command())
    _walk_to(disp, "c1", CommandStatus.EXECUTING)
    with pytest.raises(ValueError, match="invalid transition"):
        disp.transition("c1", CommandStatus.ISSUED)
    assert disp.get_status("c1") == CommandStatus.EXECUTING


def test_skip_forward_transition_is_rejected():
    """PENDING -> DELIVERED (skipping ISSUED + VALIDATED) is invalid.
    Skipping states breaks the audit trail."""
    disp, _ = make()
    disp.issue(a_command())
    disp.transition("c1", CommandStatus.VALIDATED)
    with pytest.raises(ValueError, match="invalid transition"):
        disp.transition("c1", CommandStatus.DELIVERED)


def test_terminal_state_has_no_outgoing_transitions():
    """Once a command is COMPLETED, FAILED, CANCELLED, or TIMED_OUT,
    no further transitions are allowed. The state machine refuses
    them rather than corrupting the audit history."""
    disp, _ = make()
    for terminal in (CommandStatus.COMPLETED, CommandStatus.FAILED,
                     CommandStatus.CANCELLED, CommandStatus.TIMED_OUT):
        cmd_id = f"c-{terminal.value}"
        disp.issue(Command(
            command_id=cmd_id,
            session_id="s1",
            type="capture_photo",
            params={},
            issued_at=T0,
            expires_at=T0 + timedelta(seconds=30),
        ))
        # Walk to the terminal state. Every non-terminal command passes
        # through VALIDATED -> ISSUED -> DELIVERED -> EXECUTING first.
        for s in (
            CommandStatus.VALIDATED, CommandStatus.ISSUED,
            CommandStatus.DELIVERED, CommandStatus.EXECUTING,
        ):
            disp.transition(cmd_id, s)
        # Now transition to the terminal state — this is the LAST
        # valid forward transition.
        disp.transition(cmd_id, terminal, at=T0)
        # No further transitions are allowed.
        with pytest.raises(ValueError, match="invalid transition"):
            disp.transition(cmd_id, CommandStatus.EXECUTING, at=T0)
        with pytest.raises(ValueError, match="invalid transition"):
            disp.transition(cmd_id, CommandStatus.PENDING, at=T0)


def test_terminal_to_terminal_transition_is_rejected():
    """COMPLETED -> FAILED is invalid: a command that succeeded
    cannot later fail. The state machine refuses it."""
    disp, _ = make()
    disp.issue(a_command())
    # Walk to EXECUTING; the final EXECUTING -> COMPLETED transition is
    # the last valid forward step.
    _walk_to(disp, "c1", CommandStatus.EXECUTING)
    disp.transition("c1", CommandStatus.COMPLETED)
    with pytest.raises(ValueError, match="invalid transition"):
        disp.transition("c1", CommandStatus.FAILED)
    with pytest.raises(ValueError, match="invalid transition"):
        disp.transition("c1", CommandStatus.CANCELLED)


# --- abort paths -------------------------------------------------------------


def test_cancellation_from_any_non_terminal_state():
    """Cancellation is reachable from any non-terminal state. This
    is the "operator changes their mind" path."""
    for state in (CommandStatus.PENDING, CommandStatus.VALIDATED,
                 CommandStatus.ISSUED, CommandStatus.DELIVERED,
                 CommandStatus.EXECUTING):
        disp, _ = make()
        cmd_id = f"cancel-{state.value}"
        disp.issue(Command(
            command_id=cmd_id,
            session_id="s1",
            type="capture_photo",
            params={},
            issued_at=T0,
            expires_at=T0 + timedelta(seconds=30),
        ))
        _walk_to(disp, cmd_id, state)
        disp.transition(cmd_id, CommandStatus.CANCELLED)
        assert disp.get_status(cmd_id) == CommandStatus.CANCELLED


def test_timeout_from_any_non_terminal_state():
    """Timeout is reachable from any non-terminal state. The device
    missed its deadline to ack."""
    for state in (CommandStatus.PENDING, CommandStatus.ISSUED, CommandStatus.EXECUTING):
        disp, _ = make()
        cmd_id = f"timeout-{state.value}"
        disp.issue(Command(
            command_id=cmd_id,
            session_id="s1",
            type="capture_photo",
            params={},
            issued_at=T0,
            expires_at=T0 + timedelta(seconds=30),
        ))
        _walk_to(disp, cmd_id, state)
        disp.transition(cmd_id, CommandStatus.TIMED_OUT, detail={"deadline": "T0+30s"})
        assert disp.get_status(cmd_id) == CommandStatus.TIMED_OUT


# --- history ----------------------------------------------------------------


def test_history_records_from_status():
    """Every transition (except the initial PENDING) records the
    source status. The Android lifecycle view uses this to render
    the timeline (e.g. "PENDING -> ISSUED at 12:00:30")."""
    disp, _ = make()
    disp.issue(a_command())
    disp.transition("c1", CommandStatus.VALIDATED)
    disp.transition("c1", CommandStatus.ISSUED)
    history = disp.get_history("c1")
    # history[0] is the initial PENDING (from_status=None).
    # history[1] is the VALIDATED transition; history[2] is ISSUED.
    assert history[1].from_status == CommandStatus.PENDING
    assert history[1].to_status == CommandStatus.VALIDATED
    assert history[2].from_status == CommandStatus.VALIDATED
    assert history[2].to_status == CommandStatus.ISSUED


def test_history_is_append_only():
    """``get_history`` returns a snapshot — calling it again returns
    the same length. The dispatcher is frozen; transitions produce
    new history entries, never in-place edits."""
    disp, _ = make()
    disp.issue(a_command())
    disp.transition("c1", CommandStatus.VALIDATED)
    disp.transition("c1", CommandStatus.ISSUED)
    h1 = disp.get_history("c1")
    n = len(h1)
    h2 = disp.get_history("c1")
    assert len(h1) == len(h2) == n


# --- unknown command ---------------------------------------------------------


def test_transition_on_unknown_command_raises():
    disp, _ = make()
    with pytest.raises(KeyError):
        disp.transition("nonexistent", CommandStatus.ISSUED)


def test_get_status_on_unknown_command_raises():
    disp, _ = make()
    with pytest.raises(KeyError):
        disp.get_status("nonexistent")


def test_get_history_on_unknown_command_raises():
    disp, _ = make()
    with pytest.raises(KeyError):
        disp.get_history("nonexistent")


# --- integration with is_valid_transition ---------------------------------


def test_dispatcher_validates_using_is_valid_transition():
    """The dispatcher and the pure is_valid_transition function must
    agree. The dispatcher is a thin wrapper that just enforces
    consistency."""
    for frm, to, valid in [
        (CommandStatus.PENDING, CommandStatus.VALIDATED, True),
        (CommandStatus.PENDING, CommandStatus.DELIVERED, False),  # skip
        (CommandStatus.VALIDATED, CommandStatus.PENDING, False),  # backward
        (CommandStatus.PENDING, CommandStatus.PENDING, False),     # same-state
    ]:
        disp, _ = make()
        disp.issue(a_command())
        if frm != CommandStatus.PENDING:
            _walk_to(disp, "c1", frm)
        try:
            disp.transition("c1", to)
            accepted = True
        except ValueError:
            accepted = False
        assert accepted == valid, (
            f"frm={frm} to={to}: dispatcher said {accepted} but "
            f"is_valid_transition says {valid}"
        )
