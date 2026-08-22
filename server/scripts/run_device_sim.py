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


if __name__ == "__main__":
    main()
