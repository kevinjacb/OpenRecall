"""Tests for the §E WebSocket control-plane envelope.

Control messages cross the trust boundary (the Android relay is untrusted), so
inbound parsing is strict: unknown types and unexpected fields are rejected rather
than silently accepted. Audio travels as binary §C.6 packets on separate frames;
these JSON messages are only the control/signalling plane.
"""

import pytest
from pydantic import ValidationError

from sense_server.protocol.messages import (
    Ack,
    Bye,
    Hello,
    RequestChunks,
    TranscriptMsg,
    parse_control,
)


def test_parses_hello_with_default_start_seq():
    msg = parse_control('{"type": "hello", "session_id": "s1"}')
    assert isinstance(msg, Hello)
    assert msg.session_id == "s1"
    assert msg.start_seq == 0


def test_parses_hello_with_explicit_start_seq():
    msg = parse_control('{"type": "hello", "session_id": "s1", "start_seq": 42}')
    assert isinstance(msg, Hello)
    assert msg.start_seq == 42


def test_parses_bye():
    msg = parse_control('{"type": "bye", "session_id": "s1"}')
    assert isinstance(msg, Bye)
    assert msg.session_id == "s1"


def test_rejects_unknown_message_type():
    with pytest.raises(ValidationError):
        parse_control('{"type": "definitely_not_a_message", "session_id": "s1"}')


def test_rejects_unexpected_fields_strictly():
    with pytest.raises(ValidationError):
        parse_control('{"type": "hello", "session_id": "s1", "evil": true}')


def test_outbound_messages_serialize_with_their_type_tag():
    import json

    assert json.loads(Ack(session_id="s1", next_seq=7).model_dump_json()) == {
        "type": "ack",
        "session_id": "s1",
        "next_seq": 7,
    }
    assert json.loads(
        RequestChunks(session_id="s1", start=3, end=6).model_dump_json()
    ) == {"type": "request_chunks", "session_id": "s1", "start": 3, "end": 6}
    assert json.loads(
        TranscriptMsg(session_id="s1", text="hello world", duration_ms=5000).model_dump_json()
    ) == {
        "type": "transcript",
        "session_id": "s1",
        "text": "hello world",
        "duration_ms": 5000,
    }
