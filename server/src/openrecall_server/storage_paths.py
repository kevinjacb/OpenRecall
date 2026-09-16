"""Derive the sibling database paths from the event-store path.

The gateway takes one ``--db`` flag and derives seven more paths from it. That
was done inline with ``args.db.replace("events.db", "commands.db")`` — string
replacement, not path joining — which has a sharp edge: if the path does not
contain ``events.db``, every replacement is a **no-op**, so all eight stores
open the same file. Nothing errors. The tables have different names, so SQLite
happily creates them side by side in one database and the corruption is silent
until someone looks.

It is one flag typo away (``--db /data/sense.db``), and a container entrypoint
that templates the path makes it easier still. So the derivation lives here,
in one function that validates first, and the callers no longer say ``.replace``.
"""
from __future__ import annotations

from pathlib import Path

#: The canonical basename. The event store is the anchor all siblings hang off.
EVENTS_DB_NAME = "events.db"

#: Every sibling the gateway derives. Kept here so the set is visible in one
#: place — it is also the list a backup has to cover.
SIBLING_DB_NAMES = (
    "commands.db",
    "segment_meta.db",
    "settings.db",
    "atoms.db",
    "reminders.db",
    "memory_index.db",
    "speakers.db",
)


class InvalidDatabasePath(ValueError):
    """The --db path cannot be used to derive the sibling databases."""


def validate_events_db_path(db: str | Path) -> Path:
    """Return ``db`` as a Path, or raise if siblings cannot be derived from it.

    Accepts any basename *containing* ``events.db`` (so ``test-events.db`` is
    fine and derives ``test-commands.db``), because that is what the original
    ``str.replace`` semantics allowed and some tooling relies on it.
    """
    path = Path(db)
    if EVENTS_DB_NAME not in path.name:
        raise InvalidDatabasePath(
            f"--db must be named {EVENTS_DB_NAME} (got {path.name!r}). The "
            + "/".join(SIBLING_DB_NAMES)
            + f" databases are derived from it by replacing {EVENTS_DB_NAME!r} "
            "in the path; any other name silently collapses all eight onto one "
            f"file. Pass e.g. --db /data/{EVENTS_DB_NAME}."
        )
    return path


def sibling_db(db: str | Path, name: str) -> str:
    """Path to the ``name`` database beside the event store at ``db``.

    Validates first, so a bad ``--db`` fails here rather than silently aliasing.
    """
    if name not in SIBLING_DB_NAMES:
        raise InvalidDatabasePath(
            f"{name!r} is not a known sibling database; expected one of "
            f"{', '.join(SIBLING_DB_NAMES)}"
        )
    path = validate_events_db_path(db)
    return str(path.with_name(path.name.replace(EVENTS_DB_NAME, name)))
