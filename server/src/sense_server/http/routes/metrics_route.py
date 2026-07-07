"""GET /metrics route (N3.3) — Prometheus text format."""
from __future__ import annotations

from aiohttp import web


def add_routes(app: web.Application) -> None:
    app.router.add_get("/metrics", get_metrics)


async def get_metrics(request: web.Request) -> web.Response:
    metrics = request.app.get("sense_metrics")
    text = metrics.render() if metrics is not None else ""
    return web.Response(
        text=text,
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )
