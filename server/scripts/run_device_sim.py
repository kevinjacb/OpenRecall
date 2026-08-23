#!/usr/bin/env python3
"""Reference device simulator — drive the live gateway end to end from the Mac.

Stands in for the XIAO + Android relay: synthesises audio, Opus-encodes it (real
frames the server decodes), streams §C.6 packets to the gateway, prints the
transcripts that come back, and verifies + acks any signed §D command using the
server's public key.

    # terminal 1 — start the gateway; note the printed public key
    pip install -e '.[mlx,opus,llm]'   # plus: brew install opus
    python scripts/run_gateway.py

    # terminal 2 — run the simulator with that public key
    python scripts/run_device_sim.py --server-key <hex-from-gateway-startup> --seconds 6

To exercise command delivery, issue a command into the gateway's dispatcher (an
issuing API is future work); for audio + transcripts, --server-key may be any 64-hex
placeholder since no command will be delivered.
"""

from __future__ import annotations

import argparse
import asyncio
import math

from openrecall_server.sim.device import DeviceClient
from openrecall_server.sim.runner import run_session

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms


def synth_opus_frames(seconds: int) -> list[bytes]:
    """Synthesise speech-ish PCM and Opus-encode it into 20 ms frames."""
    from openrecall_server.sim.opus_encoder import OpusStreamEncoder

    encoder = OpusStreamEncoder()
    frames: list[bytes] = []
    total = seconds * SAMPLE_RATE
    phase = 0
    while phase < total:
        pcm = bytearray()
        for i in range(FRAME_SAMPLES):
            t = (phase + i) / SAMPLE_RATE
            sample = int(8000 * math.sin(2 * math.pi * 300 * t))
            sample = max(-32768, min(32767, sample))
            pcm += int(sample).to_bytes(2, "little", signed=True)
        frames.append(encoder.encode(bytes(pcm)))
        phase += FRAME_SAMPLES
    return frames


def _synthetic_clip() -> bytes:
    """A deterministic 4-scene MJPEG clip (5 red, 5 blue, 5 green, 5 red) for
    the P4b sim. Built with Pillow if available; else a stub of SOI-delimited
    bytes so the route still exercises split_mjpeg without the video extra."""
    try:
        from PIL import Image
        import io
        out = b""
        for color in (60, 200, 0, 60):
            for _ in range(5):
                buf = io.BytesIO()
                Image.new("L", (8, 8), color).save(buf, format="JPEG")
                out += buf.getvalue()
        return out
    except ImportError:
        # No Pillow in the dev env — 4 distinct SOI-delimited stubs.
        return b"".join(b"\xff\xd8" + bytes([c]) * 4 + b"\xff\xd9"
                        for c in (60, 200, 0, 60) for _ in range(5))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uri", default="ws://127.0.0.1:8765")
    ap.add_argument("--session", default="sim-1")
    ap.add_argument("--server-key", required=True, help="server command public key (hex)")
    ap.add_argument("--seconds", type=int, default=6, help="seconds of audio to stream")
    ap.add_argument("--frames-per-packet", type=int, default=50, help="20 ms frames per §C.6 packet")
    ap.add_argument("--idle-timeout", type=float, default=30.0,
                    help="seconds to wait for server messages before finishing")
    ap.add_argument("--token", default=None,
                    help="bearer token to send on the WS handshake (gateway auth)")
    ap.add_argument("--battery", type=float, default=0.85,
                    help="battery charge 0.0-1.0 reported in the opening telemetry "
                         "frame (P2); pass a value <= 0 to suppress telemetry")
    ap.add_argument("--snapshot", type=int, default=0,
                    help="emit N synthetic snapshot uploads to the HTTP API after the WS session (P3)")
    ap.add_argument("--video", type=int, default=0,
                    help="emit N synthetic MJPEG clip uploads to /media/videos after the WS session (P4b)")
    ap.add_argument("--http-uri", default="http://127.0.0.1:8080",
                    help="HTTP API base URL for snapshot uploads")
    args = ap.parse_args()

    frames = synth_opus_frames(args.seconds)
    packets = [
        frames[i : i + args.frames_per_packet]
        for i in range(0, len(frames), args.frames_per_packet)
    ]
    # P2: a negative --battery suppresses the opening telemetry frame; otherwise
    # the simulator emits a button-wake telemetry right after hello so the
    # server clears desired sleep and reports the real battery.
    initial_battery = args.battery if args.battery >= 0 else None
    client = DeviceClient(args.session, bytes.fromhex(args.server_key),
                          initial_battery=initial_battery)

    print(f"streaming {len(frames)} Opus frames in {len(packets)} packet(s) to {args.uri}")
    if initial_battery is not None:
        print(f"opening telemetry: button-wake battery={initial_battery:.2f}")
    # Generous idle timeout so we wait out the first window's MLX model warm-up
    # (cold start can be many seconds); after that, transcripts return quickly.
    print("waiting for transcripts (first one is slow — MLX model warm-up)…")
    asyncio.run(run_session(args.uri, client, packets,
                            idle_timeout=args.idle_timeout, token=args.token))

    print(f"\ntranscripts ({len(client.transcripts)}):")
    for text in client.transcripts:
        print(f"  {text!r}")
    print(f"verified commands: {[c.command_id for c in client.verified_commands]}")
    if client.rejected_commands:
        print(f"REJECTED (bad signature): {client.rejected_commands}")

    # P3: after the WS session, optionally POST N synthetic snapshots to the
    # HTTP API so the full P3 server path (route + blob store + timeline) runs
    # in sim. Gated on --snapshot > 0 and a bearer token. Uses stdlib
    # urllib.request so no new dependency is introduced.
    if args.snapshot and args.token:
        import urllib.request
        snap_client = client  # reuse the same session id
        print(f"\nuploading {args.snapshot} snapshot(s) to {args.http_uri}/media/snapshots…")
        for i in range(args.snapshot):
            req = snap_client.upload_snapshot_request(
                f"{args.http_uri}/media/snapshots",
                rel_ts_ms=1000 * (i + 1), session_id=args.session,
                image=b"\xff\xd8" + bytes([i]), token=args.token,
            )
            try:
                http_req = urllib.request.Request(
                    req["url"], data=req["body"], headers=req["headers"],
                    method="POST")
                with urllib.request.urlopen(http_req, timeout=10) as resp:
                    body = resp.read().decode()
                    print(f"  snapshot {i}: HTTP {resp.status} {body}")
            except Exception as exc:
                print(f"  snapshot {i}: FAILED {exc}")

    # P4b: after the WS session, optionally POST N synthetic MJPEG clips to
    # /media/videos so the full P4b server path (split -> keyframes -> scene
    # atoms) runs in sim. Gated on --video > 0 and a bearer token. Uses stdlib
    # urllib.request so no new dependency is introduced.
    if args.video and args.token:
        import urllib.request
        print(f"\nuploading {args.video} video clip(s) to {args.http_uri}/media/videos…")
        for i in range(args.video):
            clip = _synthetic_clip()  # deterministic 4-scene MJPEG
            req = client.upload_video_request(
                f"{args.http_uri}/media/videos",
                rel_ts_ms=1000 * (i + 1), session_id=args.session,
                clip=clip, token=args.token,
            )
            try:
                http_req = urllib.request.Request(
                    req["url"], data=req["body"], headers=req["headers"],
                    method="POST")
                with urllib.request.urlopen(http_req, timeout=10) as resp:
                    print(f"  video {i}: HTTP {resp.status} {resp.read().decode()}")
            except Exception as exc:
                print(f"  video {i}: FAILED {exc}")


if __name__ == "__main__":
    main()
