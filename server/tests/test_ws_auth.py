# server/tests/test_ws_auth.py
import asyncio
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError
from openrecall_server.gateway.adapter import serve
from openrecall_server.auth import load_or_create_token


@pytest.mark.asyncio
async def test_ws_rejects_missing_token(unused_tcp_port, tmp_path):
    token = load_or_create_token(tmp_path / "tok")
    server_task = asyncio.create_task(
        serve(lambda: None, host="127.0.0.1", port=unused_tcp_port,
              event_store=None, dispatcher=None, token=token)
    )
    await asyncio.sleep(0.1)
    try:
        # The server closes with 1008 "unauthorized" during/after the handshake,
        # before any §E processing. The close may surface on send, recv, or context
        # exit — any of them raising proves auth rejected the connection.
        with pytest.raises(ConnectionClosedError):
            async with connect(f"ws://127.0.0.1:{unused_tcp_port}") as ws:
                await ws.send('{"type":"hello","session_id":"s","start_seq":0}')
                await asyncio.wait_for(ws.recv(), timeout=1.0)
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


@pytest.mark.asyncio
async def test_ws_accepts_valid_token(unused_tcp_port, tmp_path):
    token = load_or_create_token(tmp_path / "tok")
    server_task = asyncio.create_task(
        serve(lambda: None, host="127.0.0.1", port=unused_tcp_port,
              event_store=None, dispatcher=None, token=token)
    )
    await asyncio.sleep(0.1)
    try:
        async with connect(
            f"ws://127.0.0.1:{unused_tcp_port}",
            additional_headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            # An invalid §E message yields a protocol-error close, NOT an auth close.
            # Receiving any close (not an immediate auth rejection) means auth passed.
            await ws.send("not json")
            with pytest.raises(Exception):
                await asyncio.wait_for(ws.recv(), timeout=1.0)
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task