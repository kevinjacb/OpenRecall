#!/usr/bin/env python3
"""Run the Sense gateway WebSocket server + HTTP control API.

This is the live entry point the Android relay connects to (XIAO -> Android -> Mac).
It wires the real Opus decoder + MLX-whisper transcriber per session, so it needs
the heavy extras installed on the Mac:

    pip install -e '.[mlx,opus]'   # also: brew install opus
    python scripts/run_gateway.py --port 8765 --window-ms 5000

Two servers share one event loop:

  * WebSocket gateway (``--port``, default 8765) — the BLE-relayed audio + control
    plane. §E text frames carry control JSON; §C.6 binary frames carry audio.
  * HTTP control API (``--http-port``, default 8766) — operator/phone-facing
    endpoints (``/health``, ``/provisioning/pubkey``), guarded by a bearer token.

On startup it prints, in order: the HTTP control URL, the WS URL, the bearer token
(copy to the phone), and the server command-signing public key (provision on device).

Protocol (one WS connection == one session):
  * text frame:  §E JSON control, e.g. {"type":"hello","session_id":"...","start_seq":0}
  * binary frame: a §C.6 audio packet
  * server replies (text JSON): ack / request_chunks / transcript
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import aiohttp.web

from sense_server.auth import load_or_create_token
from sense_server.commands.dispatcher import CommandDispatcher
from sense_server.commands.signing import load_or_create_signer
from sense_server.events.store import SqliteEventStore
from sense_server.gateway.adapter import build_pipeline_factory, serve
from sense_server.http.app import build_app
from sense_server.sessions.index import SessionIndex
from sense_server.sessions.lifecycle import SessionLifecycle


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--window-ms", type=int, default=5000, help="transcription window")
    ap.add_argument("--model", default=None, help="override the MLX-whisper model repo")
    ap.add_argument("--db", default="data/events.db", help="durable capture-event store path")
    ap.add_argument("--key-file", default="data/server_ed25519.key",
                    help="server command-signing key (created on first run)")
    ap.add_argument("--token-file", default="data/server_token",
                    help="bearer token file for the HTTP control API + WS auth (created on first run)")
    ap.add_argument("--http-port", type=int, default=8766,
                    help="HTTP control API port (operator/phone-facing)")
    args = ap.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = SqliteEventStore(args.db)
    signer = load_or_create_signer(args.key_file)
    dispatcher = CommandDispatcher(signer)
    factory = build_pipeline_factory(window_ms=args.window_ms, model=args.model)

    # Phase 3 dependencies: the index backs `/sessions`, the lifecycle backs
    # `/status`'s active_sessions counter. Both are process-wide and in-memory;
    # a restart rebuilds the index from the durable store on demand.
    session_index = SessionIndex()
    session_lifecycle = SessionLifecycle()

    token = load_or_create_token(args.token_file)
    app = build_app(
        token=token,
        get_pubkey=lambda: signer.public_key_bytes,
        event_store=store,
        session_index=session_index,
        session_lifecycle=session_lifecycle,
    )

    async def main_loop() -> None:
        http_runner = aiohttp.web.AppRunner(app)
        try:
            await http_runner.setup()
            site = aiohttp.web.TCPSite(http_runner, args.host, args.http_port)
            await site.start()
            print(f"http control API on http://{args.host}:{args.http_port}")
            print(f"gateway listening on ws://{args.host}:{args.port}  "
                  f"(window={args.window_ms} ms, events -> {args.db})")
            print(f"bearer token (copy to phone): {token}")
            print(f"server command public key (provision on device): "
                  f"{signer.public_key_bytes.hex()}")
            await serve(
                factory,
                host=args.host,
                port=args.port,
                event_store=store,
                dispatcher=dispatcher,
                token=token,
                session_index=session_index,
                session_lifecycle=session_lifecycle,
            )
        finally:
            await http_runner.cleanup()

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()