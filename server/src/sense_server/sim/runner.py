"""Async WebSocket runner for the reference :class:`DeviceClient`.

A thin shell that connects to the gateway and drives one session: open with hello,
stream the given §C.6 audio packets, then drain server messages (transcripts, acks,
signed commands) through the client — replying with command acks — until the link
goes idle. Used both by the end-to-end test and by ``scripts/run_device_sim.py``.
"""

from __future__ import annotations

from typing import Iterable

from .device import DeviceClient


async def run_session(
    uri: str,
    client: DeviceClient,
    audio_frames: Iterable[list[bytes]],
    *,
    idle_timeout: float = 0.3,
) -> None:
    import asyncio

    import websockets

    async with websockets.connect(uri) as ws:
        await ws.send(client.hello())
        for frames in audio_frames:
            await ws.send(client.next_audio_packet(frames))
        await ws.send(client.bye())

        # The server doesn't close on bye, so drain until the link is briefly idle.
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                break
            for reply in client.on_message(message):
                await ws.send(reply)
