"""GET / PUT /settings (spec §4.1, §4.2).

Greenfield surface, so snake_case pydantic DTOs with ``ErrorEnvelopeDTO`` for
errors (spec ground rule 3).

A ``PUT`` that changes ``capture.audio_enabled`` triggers reconciliation
immediately rather than waiting for the next ``hello``: if the device happens
to be connected, the toggle should take effect now, and if it isn't, the
reconciler is a no-op and the next ``hello`` picks it up.
"""
from __future__ import annotations

from aiohttp import web

from ...settings.model import SettingsDocument, SettingsPatch


def add_routes(app: web.Application) -> None:
    app.router.add_get("/settings", get_settings)
    app.router.add_put("/settings", put_settings)


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"schema_version": "v1", "code": code, "message": message}, status=status,
    )


def _store(app: web.Application):
    return app.get("sense_settings_store")


async def get_settings(request: web.Request) -> web.Response:
    store = _store(request.app)
    if store is None:
        return _error("not_found", "settings are not configured", 404)
    return web.json_response(store.get().model_dump(mode="json"))


async def put_settings(request: web.Request) -> web.Response:
    """Merge a full or partial document and persist it.

    ``extra="forbid"`` on the patch models means a misspelled key is a 400
    rather than a silently-ignored change — the failure mode worth avoiding
    is a user believing they turned something off.
    """
    store = _store(request.app)
    if store is None:
        return _error("not_found", "settings are not configured", 404)

    try:
        body = await request.json()
    except Exception:
        return _error("bad_request", "body must be JSON", 400)
    try:
        patch = SettingsPatch.model_validate(body)
    except Exception as exc:
        return _error("bad_request", f"invalid settings: {exc}", 400)

    current: SettingsDocument = store.get()
    merged = patch.merge_onto(current)
    store.put(merged)

    if merged.capture.audio_enabled != current.capture.audio_enabled:
        reconciler = request.app.get("sense_reconciler")
        if reconciler is not None:
            try:
                reconciler.reconcile()
            except Exception:
                # The setting is saved either way; convergence is the
                # reconciler's ongoing job, not this request's.
                request.app.logger.exception("reconcile_on_put_failed")

    return web.json_response(merged.model_dump(mode="json"))
