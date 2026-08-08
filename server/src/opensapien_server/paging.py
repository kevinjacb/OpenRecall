"""The one keyset-cursor format, shared by every paged list endpoint.

A cursor is an opaque base64url-encoded JSON ``{"before": iso, "last_id": id}``
— the ``(timestamp, id)`` anchor of the last row on the page just served. The
next page resumes strictly after it, which is why paging stays correct while
rows are being inserted underneath it (an offset would skip or repeat rows).

The format started life private to :mod:`opensapien_server.sessions.index`.
It lives here because ``/segments`` and ``/memory`` page the same way, and
three copies of a cursor codec is three chances for one of them to drift into
a subtly different padding or tiebreak rule.

Opacity is the contract: clients echo the string back and never parse it. The
trailing ``=`` padding is stripped because ``=`` is URL-special per RFC 3986,
so a client that puts the cursor in a query string without escaping it does
not hand us a truncated cursor.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime


def encode_cursor(*, before: datetime, last_id: str) -> str:
    """Encode a ``(timestamp, id)`` keyset anchor as an opaque cursor."""
    payload = json.dumps({"before": before.isoformat(), "last_id": last_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a cursor into its ``(timestamp, id)`` anchor.

    Raises :class:`ValueError` on anything malformed. Callers turn that into
    a 400 rather than silently serving page one — a cursor bug that quietly
    restarts pagination is a client that loops forever.
    """
    try:
        # Re-pad to a multiple of 4: Python's decoder is strict about padding
        # and the encoder strips it.
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        before = datetime.fromisoformat(payload["before"])
        last_id = str(payload["last_id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"bad cursor: {e!s}") from e
    return before, last_id
