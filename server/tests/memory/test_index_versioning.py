"""Tests for the MemoryIndex name/version metadata (M12).

Every index impl advertises a stable ``name`` and ``version`` so the
Retriever can record provenance (``retrieval_strategy``, ``index_name``,
``index_version``) and operators can correlate a strange result with
the backend that produced it.
"""
from __future__ import annotations

import pytest

from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.index import InMemoryMemoryIndex, MemoryIndex, SqliteMemoryIndex


def test_in_memory_index_name_version():
    idx = InMemoryMemoryIndex()
    assert idx.name == "in_memory"
    assert idx.version == "v1"


def test_sqlite_index_name_version():
    idx = SqliteMemoryIndex(":memory:")
    assert idx.name == "sqlite"
    assert idx.version == "v1"


def test_in_memory_index_satisfies_protocol():
    assert isinstance(InMemoryMemoryIndex(), MemoryIndex)


def test_sqlite_index_satisfies_protocol():
    assert isinstance(SqliteMemoryIndex(":memory:"), MemoryIndex)
