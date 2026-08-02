"""§E WebSocket control-plane envelope.

The wearable's BLE link is relayed by the (untrusted) Android phone into a
WebSocket to the Mac. Audio rides as binary §C.6 packets on their own frames; these
JSON messages are the control/signalling plane only.

Inbound messages are parsed strictly (``extra="forbid"`` + a discriminated union on
``type``) because they cross the trust boundary. Outbound messages serialise with
their ``type`` tag for the client to dispatch on.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- Inbound (device/relay -> server) ----------------------------------------


class Hello(_Strict):
    """Open/resume a session; ``start_seq`` is the first chunk_seq to expect."""

    type: Literal["hello"] = "hello"
    session_id: str
    start_seq: int = 0


class Bye(_Strict):
    """Close a session; the server flushes any buffered audio."""

    type: Literal["bye"] = "bye"
    session_id: str


class CommandAck(_Strict):
    """Device acknowledges executing a signed §D command (relayed back)."""

    type: Literal["command_ack"] = "command_ack"
    session_id: str
    command_id: str


Inbound = Annotated[
    Union[Hello, Bye, CommandAck],
    Field(discriminator="type"),
]
_INBOUND = TypeAdapter(Inbound)


def parse_control(data: str | bytes) -> Inbound:
    """Strictly parse one inbound JSON control message into its typed model."""
    return _INBOUND.validate_json(data)


# ---- Outbound (server -> device/relay) ---------------------------------------


class Ack(_Strict):
    """Cursor ack: ``next_seq`` is the next contiguous chunk_seq the server wants."""

    type: Literal["ack"] = "ack"
    session_id: str
    next_seq: int


class RequestChunks(_Strict):
    """Backfill request for the contiguous head gap ``[start, end)``."""

    type: Literal["request_chunks"] = "request_chunks"
    session_id: str
    start: int
    end: int


class TranscriptMsg(_Strict):
    """A transcribed window pushed back to the client."""

    type: Literal["transcript"] = "transcript"
    session_id: str
    text: str
    duration_ms: int
    speaker: str | None = None
    speaker_confidence: float | None = None
    speaker_assignment: str | None = None
    speaker_name: str | None = None  # resolved display_name at emit time
    is_wearer: bool = False           # this hop's speaker is the wearer


class CommandMessage(_Strict):
    """A signed §D command for the device. ``payload``/``sig`` are the signed envelope
    (see :meth:`SignedCommand.to_wire`); the relay forwards them verbatim and the
    device verifies the signature before executing."""

    type: Literal["command"] = "command"
    session_id: str
    payload: str
    sig: str


class ProactiveMessage(_Strict):
    """A proactive answer from the server (P3).

    Carries the agent's answer text and the cited atom ids. The relay
    forwards it into the phone's ChatHistoryStore; the chat screen
    renders it as a new ``AGENT_PROACTIVE`` ChatMessage. The proactive
    trigger is server-initiated, never the user, so the user knows
    it's not from a question they asked.
    """

    type: Literal["proactive"] = "proactive"
    session_id: str
    request_id: str
    text: str
    atoms: tuple[str, ...] = Field(default_factory=tuple)
    propose: dict | None = None
