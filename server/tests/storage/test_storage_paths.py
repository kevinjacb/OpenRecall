import pytest

from openrecall_server.storage_paths import (
    SIBLING_DB_NAMES,
    InvalidDatabasePath,
    sibling_db,
    validate_events_db_path,
)


def test_siblings_derive_beside_the_event_store():
    assert sibling_db("/data/events.db", "atoms.db") == "/data/atoms.db"
    assert sibling_db("/data/events.db", "speakers.db") == "/data/speakers.db"


def test_a_prefixed_basename_still_derives_a_matching_prefix():
    """The original str.replace semantics allowed this and tooling relies on it."""
    assert sibling_db("/tmp/run7-events.db", "commands.db") == "/tmp/run7-commands.db"


def test_every_sibling_gets_a_distinct_path():
    """The bug this module exists to prevent: with a bad basename every
    str.replace was a no-op, so all eight stores opened ONE file and SQLite
    silently created every table side by side in it."""
    paths = {sibling_db("/data/events.db", n) for n in SIBLING_DB_NAMES}
    assert len(paths) == len(SIBLING_DB_NAMES)
    assert "/data/events.db" not in paths


def test_a_basename_without_events_db_is_refused():
    with pytest.raises(InvalidDatabasePath) as exc:
        sibling_db("/data/sense.db", "atoms.db")
    assert "events.db" in str(exc.value)


def test_events_db_elsewhere_in_the_path_is_not_enough():
    """A parent directory named events.db must not satisfy the check — the
    replacement operates on the basename, so only the basename counts."""
    with pytest.raises(InvalidDatabasePath):
        validate_events_db_path("/srv/events.db/capture.sqlite")


def test_an_unknown_sibling_name_is_refused():
    with pytest.raises(InvalidDatabasePath):
        sibling_db("/data/events.db", "not_a_real_store.db")


def test_the_sibling_list_matches_what_the_gateway_actually_opens():
    """A sibling added to run_gateway.py but not here would be derived by a
    raw .replace again, reintroducing the aliasing bug for that one store."""
    src = open("scripts/run_gateway.py").read()
    for name in SIBLING_DB_NAMES:
        assert name in src, f"{name} is declared here but never opened by the gateway"
    assert 'args.db.replace(' not in src, (
        "run_gateway.py still derives a database path by raw string replacement; "
        "use storage_paths.sibling_db so the bad-basename case cannot recur")
