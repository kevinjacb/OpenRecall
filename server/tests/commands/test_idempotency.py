"""Tests for command idempotency.

The dispatcher dedups by ``idempotency_key``: two calls with the
same key produce a single command (same command_id, re-issuing
returns the original signed command). The ``command_id`` field on
the Command is per-issue; the ``idempotency_key`` is the user-facing
identifier the Planner uses to dedup retries.

This is the binding contract for "the user asks the same thing twice,
we issue once":

  1. issue(c1)               -> command_id="c1"
  2. issue(c1_again)         -> same command_id="c1" (no duplicate)
  3. issue(c2) with new key  -> command_id="c2" (new issue)

A retry across gateway restarts (or after the dispatcher is
re-instantiated) MUST be dedup-safe: the dispatcher persists the
idempotency_key → command_id mapping in the command record (Phase 7).
The Phase 4 in-memory implementation lays the API ground; the
durable layer comes next.
"""
from __future__ import annotations

import pytest

from opensapien_server.commands.dispatcher import CommandDispatcher
from opensapien_server.commands.model import Command
from opensapien_server.commands.signing import CommandSigner


T0 = __import__("datetime").datetime(2026, 6, 30, 12, 0, 0, tzinfo=__import__("datetime").timezone.utc)


def make():
    clock = type("C", (), {"__call__": lambda self: T0})()
    signer = CommandSigner.generate()
    return CommandDispatcher(signer, clock=clock), signer, clock


def a_command(command_id: str, idempotency_key: str = "ik-1", ttl_s: int = 30) -> Command:
    return Command(
        command_id=command_id,
        session_id="s1",
        type="capture_photo",
        params={},
        issued_at=T0,
        expires_at=T0 + __import__("datetime").timedelta(seconds=ttl_s),
        idempotency_key=idempotency_key,
    )


def test_same_idempotency_key_dedups_to_first_command_id():
    """Two issues with the same key produce the same command_id.

    This is the binding contract: the second issue is a no-op.
    The caller gets the FIRST signed command back.
    """
    disp, signer, _ = make()
    s1 = disp.issue(a_command("c1", idempotency_key="user-photo-1"))
    s2 = disp.issue(a_command("c2", idempotency_key="user-photo-1"))
    assert s1.command.command_id == "c1"  # first wins; the c2 issue is deduped
    assert s2.command.command_id == "c1"  # deduped to the first signed command
    # Only one command is tracked; pending() returns one.
    assert len(disp.pending()) == 1


def test_different_idempotency_keys_produce_different_command_ids():
    """Two issues with different keys produce different command_ids."""
    disp, _, _ = make()
    s1 = disp.issue(a_command("c1", idempotency_key="user-photo-1"))
    s2 = disp.issue(a_command("c2", idempotency_key="user-photo-2"))
    assert s1.command.command_id == "c1"
    assert s2.command.command_id == "c2"
    assert len(disp.pending()) == 2


def test_legacy_command_id_dedup_still_works():
    """Backward compat: commands WITHOUT an idempotency_key
    dedup by command_id (the original behavior)."""
    c = Command(
        command_id="c1",
        session_id="s1",
        type="capture_photo",
        params={},
        issued_at=T0,
        expires_at=T0 + __import__("datetime").timedelta(seconds=30),
        # no idempotency_key
    )
    disp, _, _ = make()
    disp.issue(c)
    same_again = Command(
        command_id="c1",  # same id
        session_id="s1",
        type="capture_photo",
        params={},
        issued_at=T0,
        expires_at=T0 + __import__("datetime").timedelta(seconds=30),
    )
    s2 = disp.issue(same_again)
    # Without idempotency_key, dedup falls back to command_id.
    assert s2.command.command_id == "c1"
    assert len(disp.pending()) == 1


def test_idempotency_key_persists_through_ack():
    """A command dispatched with idempotency_key=X, when acked, is
    removed from pending. A subsequent issue with the same key
    produces a NEW command (because the original completed)."""
    disp, _, _ = make()
    s1 = disp.issue(a_command("c1", idempotency_key="user-1"))
    disp.ack("c1")
    s2 = disp.issue(a_command("c2", idempotency_key="user-1"))
    # Same idempotency_key, but the original is acked — so this is a
    # NEW issue, not a dedup hit. The dedup is only for unacked /
    # unexpired commands.
    assert s2.command.command_id == "c2"
