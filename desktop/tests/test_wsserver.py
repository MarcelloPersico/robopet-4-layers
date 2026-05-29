"""End-to-end WebSocket loopback: a real client connects to WsServer and we
assert channel demux + outbound UART routing."""

import asyncio

from websockets.asyncio.client import connect

import protocol
from wsserver import WsServer


async def _start():
    srv = WsServer("127.0.0.1", 0)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    return srv, port


async def test_inbound_demux_and_outbound_uart():
    srv, port = await _start()
    try:
        async with connect(f"ws://127.0.0.1:{port}") as client:
            # audio -> audio_in
            await client.send(protocol.encode_frame(protocol.CH_AUDIO, b"pcmdata"))
            assert await asyncio.wait_for(srv.audio_in.get(), 1.0) == b"pcmdata"

            # video -> latest frame
            await client.send(protocol.encode_frame(protocol.CH_VIDEO, b"\xff\xd8jpg"))
            await asyncio.sleep(0.05)
            assert srv.take_latest_frame() == b"\xff\xd8jpg"

            # control -> control_in (dict)
            await client.send(protocol.encode_control({"type": "vad", "event": "start"}))
            assert await asyncio.wait_for(srv.control_in.get(), 1.0) == {"type": "vad", "event": "start"}

            # uart -> uart_in, and marks this client as the bridge peer
            await client.send(protocol.encode_uart('{"type":"telemetry","mode":"idle"}'))
            assert "telemetry" in await asyncio.wait_for(srv.uart_in.get(), 1.0)

            # outbound uart should now reach this client
            assert await srv.send_uart('{"type":"ping"}') is True
            frame = await asyncio.wait_for(client.recv(), 1.0)
            ch, payload = protocol.decode_frame(frame)
            assert ch == protocol.CH_UART and payload == b'{"type":"ping"}\n'
    finally:
        await srv.close()


async def test_send_uart_without_peer_returns_false():
    srv, _port = await _start()
    try:
        assert await srv.send_uart("x") is False  # nobody connected
        assert srv.connected is False
    finally:
        await srv.close()
