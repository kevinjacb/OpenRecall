"""Tests for the §D device command model and its canonical signing bytes.

The server signs a command's *canonical bytes* and the device verifies over the
same bytes, so canonicalisation must be deterministic and independent of dict
insertion order — otherwise a valid command could fail verification on the device.
"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sense_server.commands.model import Command

T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def cmd(**overrides) -> Command:
    base = dict(
        command_id="c1",
        session_id="s1",
        type="display_text",
        params={"text": "hello", "ttl": 5},
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=30),
    )
    base.update(overrides)
    return Command(**base)


def test_canonical_bytes_are_deterministic():
    assert cmd().canonical_bytes() == cmd().canonical_bytes()


def test_canonical_bytes_ignore_param_insertion_order():
    a = cmd(params={"text": "hello", "ttl": 5})
    b = cmd(params={"ttl": 5, "text": "hello"})  # same content, different order
    assert a.canonical_bytes() == b.canonical_bytes()


def test_canonical_bytes_change_when_any_field_changes():
    assert cmd().canonical_bytes() != cmd(command_id="c2").canonical_bytes()
    assert cmd().canonical_bytes() != cmd(params={"text": "bye"}).canonical_bytes()


def test_is_expired_is_relative_to_now():
    c = cmd()
    assert c.is_expired(T0 + timedelta(seconds=10)) is False
    assert c.is_expired(T0 + timedelta(seconds=30)) is True  # at/after expiry


def test_unknown_command_type_is_rejected():
    with pytest.raises(ValidationError):
        cmd(type="self_destruct")
