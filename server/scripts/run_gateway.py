#!/usr/bin/env python3
"""Run the Sense gateway WebSocket server (BLE-relayed audio + control plane).

This is the live entry point the Android relay connects to (XIAO -> Android -> Mac).
It wires the real Opus decoder + MLX-whisper transcriber per session, so it needs
the heavy extras installed on the Mac:

    pip install -e '.[mlx,opus]'   # also: brew install opus
    python scripts/run_gateway.py --port 8765 --window-ms 5000

Protocol (one connection == one session):
  * text frame:  §E JSON control, e.g. {"type":"hello","session_id":"...","start_seq":0}
  * binary frame: a §C.6 audio packet
  * server replies (text JSON): ack / request_chunks / transcript
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sense_server.commands.dispatcher import CommandDispatcher
from sense_server.commands.signing import load_or_create_signer
from sense_server.events.store import SqliteEventStore
from sense_server.gateway.adapter import build_pipeline_factory, serve


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--window-ms", type=int, default=5000, help="transcription window")
    ap.add_argument("--model", default=None, help="override the MLX-whisper model repo")
    ap.add_argument("--db", default="data/events.db", help="durable capture-event store path")
    ap.add_argument("--key-file", default="data/server_ed25519.key",
                    help="server command-signing key (created on first run)")
    args = ap.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = SqliteEventStore(args.db)
    signer = load_or_create_signer(args.key_file)
    dispatcher = CommandDispatcher(signer)
    factory = build_pipeline_factory(window_ms=args.window_ms, model=args.model)

    print(f"gateway listening on ws://{args.host}:{args.port}  "
          f"(window={args.window_ms} ms, events -> {args.db})")
    print(f"server command public key (provision this on the device): "
          f"{signer.public_key_bytes.hex()}")
    try:
        asyncio.run(
            serve(factory, host=args.host, port=args.port,
                  event_store=store, dispatcher=dispatcher)
        )
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
