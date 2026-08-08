"""Phase 2 — durable segment titles (spec §2.2).

Both backends run one suite: the in-memory twin is what the HTTP tests use,
so a divergence would mean green tests and a broken server.
"""
from __future__ import annotations

import pytest

from opensapien_server.sessions.segment_meta import (
    InMemorySegmentMetaStore,
    SqliteSegmentMetaStore,
)


@pytest.fixture(params=["memory", "sqlite"])
def meta(request, tmp_path):
    if request.param == "memory":
        return InMemorySegmentMetaStore()
    return SqliteSegmentMetaStore(tmp_path / "segment_meta.db")


def test_unknown_segment_has_no_meta(meta):
    assert meta.get("s1:0") is None


def test_set_and_read_an_llm_title(meta):
    assert meta.set_title("s1:0", "Studio standup", source="llm") is True

    row = meta.get("s1:0")
    assert row.title == "Studio standup"
    assert row.title_source == "llm"
    assert row.updated_at is not None


def test_a_user_title_overwrites_an_llm_title(meta):
    meta.set_title("s1:0", "Auto name", source="llm")

    assert meta.set_title("s1:0", "My name", source="user") is True

    row = meta.get("s1:0")
    assert row.title == "My name"
    assert row.title_source == "user"


def test_an_llm_title_never_overwrites_a_user_title(meta):
    """Losing someone's own name for a recording to a background job is the
    kind of small betrayal that makes a feature untrusted."""
    meta.set_title("s1:0", "My name", source="user")

    assert meta.set_title("s1:0", "Auto name", source="llm") is False

    assert meta.get("s1:0").title == "My name"


def test_an_llm_title_can_replace_another_llm_title(meta):
    meta.set_title("s1:0", "First guess", source="llm")

    assert meta.set_title("s1:0", "Better guess", source="llm") is True

    assert meta.get("s1:0").title == "Better guess"


def test_titles_for_batches_a_page(meta):
    meta.set_title("s1:0", "One", source="llm")
    meta.set_title("s1:9", "Two", source="user")

    got = meta.titles_for(["s1:0", "s1:9", "s1:99"])

    assert got == {"s1:0": "One", "s1:9": "Two"}


def test_titles_for_an_empty_page(meta):
    assert meta.titles_for([]) == {}


def test_delete_is_idempotent(meta):
    meta.set_title("s1:0", "One", source="llm")

    assert meta.delete("s1:0") is True
    assert meta.delete("s1:0") is False
    assert meta.get("s1:0") is None
