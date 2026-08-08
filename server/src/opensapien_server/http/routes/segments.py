"""HTTP /segments routes — the app's primary surface (spec §2.3).

Segments, not sessions, are what the Home, Recordings and Recording Detail
pages address. See :mod:`opensapien_server.sessions.segments` for why the two
are different things. ``/sessions`` stays as-is for debugging and plumbing.

Wire convention is camelCase, mirroring ``/sessions`` — spec ground rule 3
says extend a surface in the convention it already uses, and these rows sit
next to session rows in the same client.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from ...sessions.segment_meta import TITLE_MAX_CHARS
from ...sessions.segments import Segment

if TYPE_CHECKING:
    from ...events.store import EventStore
    from ...sessions.segments import SegmentIndex

from .sessions import _audio_store, _event_to_wire, _speaker_registry

_LIMIT_DEFAULT = 20
_LIMIT_MAX = 100
_SEARCH_LIMIT = 100
_SNIPPET_CHARS = 120


def add_routes(app: web.Application) -> None:
    app.router.add_get("/segments", list_segments)
    app.router.add_get("/segments/{segment_id}", get_segment)
    app.router.add_get("/segments/{segment_id}/events", get_segment_events)
    app.router.add_get("/segments/{segment_id}/memory", get_segment_memory)
    app.router.add_patch("/segments/{segment_id}", patch_segment)
    app.router.add_get("/segments/{segment_id}/audio", get_segment_audio)
    app.router.add_get("/segments/{segment_id}/waveform", get_segment_waveform)
    app.router.add_delete("/segments/{segment_id}", delete_segment)


# ---- helpers ----------------------------------------------------------------


def _index(app: web.Application) -> "SegmentIndex":
    idx = app["sense_segment_index"]
    if idx is None:
        raise web.HTTPInternalServerError(
            text='{"error": "no_segment_index"}', content_type="application/json",
        )
    return idx


def _events(app: web.Application) -> "EventStore":
    store = app["sense_event_store"]
    if store is None:
        raise web.HTTPInternalServerError(
            text='{"error": "no_store"}', content_type="application/json",
        )
    return store


def _clamp_limit(raw: str | None) -> int:
    if not raw:
        return _LIMIT_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        return _LIMIT_DEFAULT
    return max(1, min(n, _LIMIT_MAX))


def _not_found() -> web.Response:
    return web.json_response({"error": "not_found"}, status=404)


def _memory_counts(app: web.Application, segments: list[Segment]) -> dict[str, int]:
    """"N memories" badge counts for a whole page in one pass (spec §2.5).

    Atoms carry ``session_id`` + ``start_ms``, so a segment's count is a range
    query. We fetch once per *distinct session* on the page and bucket in
    Python — one query per page rather than the per-row round trip the badge
    would otherwise cost.
    """
    atom_store = app.get("sense_atom_store")
    if atom_store is None or not segments:
        return {}
    counts: dict[str, int] = {s.id: 0 for s in segments}
    by_session: dict[str, list[Segment]] = {}
    for s in segments:
        by_session.setdefault(s.session_id, []).append(s)
    for session_id, session_segments in by_session.items():
        for atom in atom_store.atoms(session_id):
            for s in session_segments:
                if s.start_ms <= atom.start_ms < max(s.end_ms, s.start_ms + 1):
                    counts[s.id] += 1
                    break
    return counts


def _titles(app: web.Application, segments: list[Segment]) -> dict[str, str]:
    meta = app.get("sense_segment_meta")
    if meta is None or not segments:
        return {}
    return meta.titles_for([s.id for s in segments])


def _has_audio(app: web.Application, segment: Segment) -> bool:
    audio = app.get("sense_audio_store")
    if audio is None:
        return False
    return audio.has(segment.session_id)


def _to_wire(
    app: web.Application,
    s: Segment,
    *,
    title: str | None = None,
    memory_count: int = 0,
    match_snippet: str | None = None,
) -> dict:
    row = {
        "id": s.id,
        "sessionId": s.session_id,
        "title": title,
        "startedAt": s.started_at.isoformat(),
        "endedAt": s.ended_at.isoformat(),
        "durationMs": s.duration_ms(),
        "transcriptCount": s.transcript_count,
        "memoryCount": memory_count,
        "preview": s.preview or "",
        "hasAudio": _has_audio(app, s),
        "closed": s.closed,
    }
    if match_snippet is not None:
        row["matchSnippet"] = match_snippet
    return row


def _snippet(text: str, needle: str) -> str:
    """A window of ``text`` around the first match, for the search result row."""
    body = text or ""
    at = body.lower().find(needle.lower())
    if at < 0:
        return body[:_SNIPPET_CHARS]
    start = max(0, at - _SNIPPET_CHARS // 3)
    snippet = body[start:start + _SNIPPET_CHARS]
    return ("…" if start > 0 else "") + snippet


# ---- endpoints --------------------------------------------------------------


async def list_segments(request: web.Request) -> web.Response:
    """A page of segments, or search results when ``?q=`` is present."""
    query = (request.query.get("q") or "").strip()
    if query:
        return await _search_segments(request, query)

    idx = _index(request.app)
    try:
        segments, next_cursor = idx.list(
            limit=_clamp_limit(request.query.get("limit")),
            before=request.query.get("cursor"),
            session_id=request.query.get("session_id") or None,
        )
    except ValueError:
        return web.json_response({"error": "bad_cursor"}, status=400)

    titles = _titles(request.app, segments)
    counts = _memory_counts(request.app, segments)
    return web.json_response(
        {
            "segments": [
                _to_wire(
                    request.app, s,
                    title=titles.get(s.id),
                    memory_count=counts.get(s.id, 0),
                )
                for s in segments
            ],
            "nextCursor": next_cursor,
        }
    )


async def _search_segments(request: web.Request, query: str) -> web.Response:
    """Transcript search (spec §2.4).

    Event hits are mapped back to their segment and deduped to one row per
    segment — the user is looking for a recording to open, not for every
    line inside it. Search mode is unpaginated: a substring scan over a
    small table either finds a handful of rows or the query is too broad to
    page usefully.
    """
    idx = _index(request.app)
    hits = _events(request.app).search(query, limit=_SEARCH_LIMIT * 4)

    rows: list[dict] = []
    seen: set[str] = set()
    matched: list[Segment] = []
    snippets: dict[str, str] = {}
    for event in hits:
        segment = idx.segment_for_event(event.session_id, event.seq)
        if segment is None or segment.id in seen:
            continue
        seen.add(segment.id)
        matched.append(segment)
        snippets[segment.id] = _snippet(event.text, query)
        if len(matched) >= _SEARCH_LIMIT:
            break

    titles = _titles(request.app, matched)
    counts = _memory_counts(request.app, matched)
    rows = [
        _to_wire(
            request.app, s,
            title=titles.get(s.id),
            memory_count=counts.get(s.id, 0),
            match_snippet=snippets[s.id],
        )
        for s in matched
    ]
    return web.json_response({"segments": rows, "nextCursor": None})


async def get_segment(request: web.Request) -> web.Response:
    idx = _index(request.app)
    segment = idx.get(request.match_info["segment_id"])
    if segment is None:
        return _not_found()
    titles = _titles(request.app, [segment])
    counts = _memory_counts(request.app, [segment])
    registry = _speaker_registry(request.app)
    audio = _audio_store(request.app)
    events = _segment_events(request.app, segment)
    return web.json_response(
        {
            "summary": _to_wire(
                request.app, segment,
                title=titles.get(segment.id),
                memory_count=counts.get(segment.id, 0),
            ),
            "events": [_event_to_wire(e, registry, audio) for e in events],
        }
    )


def _segment_events(app: web.Application, segment: Segment) -> list:
    """The segment's slice of its session's event stream."""
    return [
        e
        for e in _events(app).events(segment.session_id)
        if segment.first_seq <= e.seq <= segment.last_seq
    ]


async def get_segment_events(request: web.Request) -> web.Response:
    idx = _index(request.app)
    segment = idx.get(request.match_info["segment_id"])
    if segment is None:
        return _not_found()
    registry = _speaker_registry(request.app)
    audio = _audio_store(request.app)
    return web.json_response(
        {
            "events": [
                _event_to_wire(e, registry, audio)
                for e in _segment_events(request.app, segment)
            ]
        }
    )


async def get_segment_memory(request: web.Request) -> web.Response:
    """The "Memories from this recording" chips — atoms whose ``start_ms``
    falls inside the segment's window."""
    idx = _index(request.app)
    segment = idx.get(request.match_info["segment_id"])
    if segment is None:
        return _not_found()
    atom_store = request.app.get("sense_atom_store")
    if atom_store is None:
        return web.json_response({"atoms": [], "returnedCount": 0})

    from .memory import _to_dto_from_store

    end = max(segment.end_ms, segment.start_ms + 1)
    atoms = [
        a for a in atom_store.atoms(segment.session_id)
        if segment.start_ms <= a.start_ms < end
    ]
    return web.json_response(
        {
            "atoms": [_to_dto_from_store(a).model_dump(mode="json") for a in atoms],
            "returnedCount": len(atoms),
        }
    )


async def patch_segment(request: web.Request) -> web.Response:
    """Rename a segment. Writes ``title_source="user"``, which permanently
    protects the title from the auto-titler."""
    idx = _index(request.app)
    segment = idx.get(request.match_info["segment_id"])
    if segment is None:
        return _not_found()
    meta = request.app.get("sense_segment_meta")
    if meta is None:
        return web.json_response({"error": "no_segment_meta"}, status=500)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)
    if not isinstance(body, dict) or "title" not in body:
        return web.json_response({"error": "bad_request"}, status=400)
    title = body["title"]
    if not isinstance(title, str):
        return web.json_response({"error": "bad_request"}, status=400)
    title = title.strip()
    if not 1 <= len(title) <= TITLE_MAX_CHARS:
        return web.json_response({"error": "bad_request"}, status=400)

    meta.set_title(segment.id, title, source="user")
    counts = _memory_counts(request.app, [segment])
    return web.json_response(
        _to_wire(
            request.app, segment,
            title=title,
            memory_count=counts.get(segment.id, 0),
        )
    )


# ---- audio plane (spec §3.2, §3.3) ------------------------------------------

WAVEFORM_BUCKET_MS = 500


async def get_segment_audio(request: web.Request) -> web.Response:
    """Serve the segment's audio as Ogg Opus.

    The container is built here, from the raw frame log, and cached
    (spec D4). ``FileResponse`` then gives Range requests, ``206`` and an
    ``ETag`` for free — which is what makes scrubbing work rather than
    forcing the client to download the whole recording to seek.

    An **open** segment is served live and never cached: it is still growing,
    so a cached copy would be wrong within seconds.
    """
    idx = _index(request.app)
    segment = idx.get(request.match_info["segment_id"])
    if segment is None:
        return _not_found()
    audio = request.app.get("sense_audio_store")
    if audio is None or not audio.has(segment.session_id):
        return _not_found()

    from ...media.ogg import mux_to_bytes

    end_ms = max(segment.end_ms, segment.start_ms)
    frames = audio.read_range(segment.session_id, segment.start_ms, end_ms)

    if not segment.closed:
        body = mux_to_bytes(frames)
        if not body:
            return _not_found()
        return web.Response(
            body=body,
            content_type="audio/ogg",
            headers={"Cache-Control": "no-store"},
        )

    cache = audio.log_path(segment.session_id).parent / "seg"
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{segment.id.replace(':', '_')}.ogg"
    if not cached.exists():
        body = mux_to_bytes(frames)
        if not body:
            return _not_found()
        # Write-then-rename: a request that dies mid-write must not leave a
        # truncated file that every later request happily serves.
        tmp = cached.with_suffix(".ogg.tmp")
        tmp.write_bytes(body)
        tmp.replace(cached)
    return web.FileResponse(cached, headers={"Content-Type": "audio/ogg"})


async def delete_segment(request: web.Request) -> web.Response:
    """Delete a recording and everything derived from it (spec §5.2).

    **Order matters.** SQLite rows first, files second, index entry last. The
    index is rebuilt from the event store on startup, so if a crash landed
    between steps, deleting the events first guarantees the rebuild cannot
    resurrect the segment. The opposite order would leave a user's deleted
    recording quietly back after a restart.

    Idempotent, and each step tolerates a missing target — a delete that was
    interrupted must be completable by simply retrying it.

    409 while the segment is still open: it is actively being written to, so
    a delete would race the ingest path. That refusal is only safe because of
    the Phase 0.1 fix — without it, a dropped session stayed "open" forever
    and its recording would have been permanently undeletable.
    """
    idx = _index(request.app)
    segment_id = request.match_info["segment_id"]
    segment = idx.get(segment_id)
    if segment is None:
        return _not_found()
    if not segment.closed:
        return web.json_response(
            {"error": "conflict", "message": "segment is still recording"},
            status=409,
        )

    end_ms = max(segment.end_ms, segment.start_ms + 1)

    # 1. Durable rows.
    _safe_delete(
        lambda: _events(request.app).delete_range(
            segment.session_id, segment.first_seq, segment.last_seq,
        ),
        "events",
    )
    atom_store = request.app.get("sense_atom_store")
    deleted_atoms: list[str] = []
    if atom_store is not None:
        deleted_atoms = _safe_delete(
            lambda: atom_store.delete_range(
                segment.session_id, segment.start_ms, end_ms,
            ),
            "atoms",
        ) or []
    memory_index = request.app.get("sense_memory_index")
    if memory_index is not None and deleted_atoms:
        # Cascaded, because an orphaned vector keeps surfacing a memory the
        # user was told was deleted.
        _safe_delete(lambda: memory_index.delete_atoms(deleted_atoms), "vectors")

    # 2. Files.
    audio = request.app.get("sense_audio_store")
    if audio is not None:
        _safe_delete(
            lambda: audio.erase_range(segment.session_id, segment.start_ms, end_ms),
            "audio",
        )
        cached = (
            audio.log_path(segment.session_id).parent
            / "seg" / f"{segment.id.replace(':', '_')}.ogg"
        )
        _safe_delete(lambda: cached.unlink(missing_ok=True), "audio_cache")
    meta = request.app.get("sense_segment_meta")
    if meta is not None:
        _safe_delete(lambda: meta.delete(segment.id), "meta")

    # 3. The index entry, last.
    idx.delete(segment.id)
    return web.Response(status=204)


def _safe_delete(fn, what: str):
    """Run one cascade step, logging rather than aborting on failure.

    A half-finished delete that stops at the first error is worse than one
    that removes everything it can: the user asked for this content to be
    gone, and a retry completes whatever was missed.
    """
    import logging

    try:
        return fn()
    except Exception:
        logging.getLogger(__name__).exception("segment_delete_step_failed step=%s", what)
        return None


async def get_segment_waveform(request: web.Request) -> web.Response:
    """The waveform behind the scrubber (spec §3.3).

    Peaks are a fixed-stride slice of the peak file, downsampled to 500 ms
    buckets. The client resamples to its 34 bars — the server does not bake
    in a display constant it cannot see.
    """
    idx = _index(request.app)
    segment = idx.get(request.match_info["segment_id"])
    if segment is None:
        return _not_found()
    audio = request.app.get("sense_audio_store")
    if audio is None or not audio.has(segment.session_id):
        return _not_found()

    from ...media.audio import FRAME_MS

    end_ms = max(segment.end_ms, segment.start_ms)
    per_slot = audio.peaks(segment.session_id, segment.start_ms, end_ms)
    if not per_slot:
        return _not_found()

    slots_per_bucket = WAVEFORM_BUCKET_MS // FRAME_MS
    buckets = [
        max(per_slot[i:i + slots_per_bucket]) / 255.0
        for i in range(0, len(per_slot), slots_per_bucket)
    ]
    return web.json_response(
        {
            "schemaVersion": "v1",
            "bucketMs": WAVEFORM_BUCKET_MS,
            "peaks": [round(p, 3) for p in buckets],
            "durationMs": len(per_slot) * FRAME_MS,
        }
    )
