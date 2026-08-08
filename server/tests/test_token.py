# server/tests/test_token.py
import os
from pathlib import Path
from opensapien_server.auth import load_or_create_token


def test_load_or_create_token_generates_and_persists(tmp_path: Path):
    p = tmp_path / "tok"
    t1 = load_or_create_token(p)
    assert len(t1) == 64
    assert all(c in "0123456789abcdef" for c in t1)
    assert p.read_text().strip() == t1
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_load_or_create_token_is_stable(tmp_path: Path):
    p = tmp_path / "tok"
    t1 = load_or_create_token(p)
    t2 = load_or_create_token(p)
    assert t1 == t2


def test_load_or_create_token_is_random_across_paths(tmp_path: Path):
    a = load_or_create_token(tmp_path / "a")
    b = load_or_create_token(tmp_path / "b")
    assert a != b