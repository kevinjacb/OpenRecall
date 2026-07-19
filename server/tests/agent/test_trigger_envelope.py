"""Tests for the Trigger envelope on PlannerContext (P3).

The Trigger union replaces the previous ``trigger_text: str`` field
on :class:`PlannerContext`. The unification rule: the planner's
read path is source-agnostic; the only difference between
:class:`UserRequest` and :class:`Proactive` is what called
:meth:`Planner.plan` and what the trigger text is. The proactive
ISSUE_COMMAND prohibition is added in a separate commit.
"""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sense_server.contracts.types import (
    PlannerContext,
    Proactive,
    Trigger,
    UserRequest,
)

# Tagged unions in pydantic v2 validate via TypeAdapter, not the
# BaseModel's model_validate (Trigger is an Annotated[Union[...]],
# not a BaseModel).
_trigger_adapter = TypeAdapter(Trigger)


def test_user_request_is_constructible() -> None:
    u = UserRequest(request_id="r1", text="hello")
    assert u.request_id == "r1"
    assert u.text == "hello"
    assert u.kind == "user_request"


def test_proactive_is_constructible() -> None:
    p = Proactive(request_id="r2", event_id="s1:7", transcript="")
    assert p.request_id == "r2"
    assert p.event_id == "s1:7"
    assert p.transcript == ""


def test_trigger_discriminator_picks_user_request() -> None:
    raw = {"kind": "user_request", "request_id": "r1", "text": "hi"}
    parsed = _trigger_adapter.validate_python(raw)
    assert isinstance(parsed, UserRequest)
    assert parsed.text == "hi"


def test_trigger_discriminator_picks_proactive() -> None:
    raw = {"kind": "proactive", "request_id": "r2", "event_id": "s:1", "transcript": "x"}
    parsed = _trigger_adapter.validate_python(raw)
    assert isinstance(parsed, Proactive)
    assert parsed.event_id == "s:1"


def test_trigger_discriminator_rejects_unknown_kind() -> None:
    raw = {"kind": "other", "request_id": "r", "text": ""}
    with pytest.raises(ValidationError):
        _trigger_adapter.validate_python(raw)


def test_planner_context_carries_user_request() -> None:
    ctx = PlannerContext(
        request_id="r1",
        trigger=UserRequest(request_id="r1", text="hi"),
        session_id="s1",
    )
    assert isinstance(ctx.trigger, UserRequest)
    assert ctx.trigger.text == "hi"


def test_planner_context_carries_proactive() -> None:
    ctx = PlannerContext(
        request_id="r2",
        trigger=Proactive(request_id="r2", event_id="s:1", transcript="x"),
        session_id="s1",
    )
    assert isinstance(ctx.trigger, Proactive)
    assert ctx.trigger.event_id == "s:1"
