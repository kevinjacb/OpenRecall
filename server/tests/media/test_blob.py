"""Tests for the content-addressed media blob store.

Media (photos, video) is content-addressed by sha256 and never inlined into the
event/atom stream — events and atoms reference the hash. Content addressing makes
storage idempotent (identical bytes store once) and tamper-evident.
"""

import hashlib

import pytest

from sense_server.media.blob import FilesystemBlobStore, InMemoryBlobStore


@pytest.fixture(params=["memory", "fs"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryBlobStore()
    return FilesystemBlobStore(tmp_path / "blobs")


def test_put_returns_the_sha256_and_get_round_trips(store):
    data = b"\xff\xd8\xff\xe0 jpeg-ish bytes"

    digest = store.put(data)

    assert digest == hashlib.sha256(data).hexdigest()
    assert store.has(digest) is True
    assert store.get(digest) == data


def test_put_is_idempotent_and_content_addressed(store):
    data = b"same bytes"
    assert store.put(data) == store.put(data)  # identical content -> identical hash


def test_get_missing_blob_raises(store):
    with pytest.raises(KeyError):
        store.get("0" * 64)


def test_has_is_false_for_unknown(store):
    assert store.has("deadbeef") is False


def test_filesystem_blobs_persist_across_reopen(tmp_path):
    path = tmp_path / "blobs"
    digest = FilesystemBlobStore(path).put(b"durable image")

    assert FilesystemBlobStore(path).get(digest) == b"durable image"
