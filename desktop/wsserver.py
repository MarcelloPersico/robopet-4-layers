"""Pi WebSocket server: accepts the single Pi connection, demultiplexes the
channel-tagged frames, and provides the outbound path to the Teensy. Plan §3.2, §8.

Inbound frames are routed to bounded asyncio queues (drop-oldest on overflow for
the non-critical media streams). Outbound motion/control is sent on the UART /
control channels. Only one Pi is expected; a new connection supersedes the old.
"""

from __future__ import annotations

import asyncio
import logging

import websockets
from websockets.server import WebSocketServerProtocol

import protocol

log = logging.getLogger("wsserver")


def _put_drop_oldest(q: "asyncio.Queue", item) -> None:
    """Non-blocking put; if full, drop the oldest to make room."""
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass


class WsServer:
    def __init__(self, host: str, port: int, ping_interval: float = 2.0, ping_timeout: float = 10.0):
        self.host = host
        self.port = port
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        # Inbound streams.
        self.audio_in: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self.uart_in: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self.control_in: asyncio.Queue[dict] = asyncio.Queue(maxsize=50)
        self.video_latest: bytes | None = None  # most recent JPEG (motion-gated)
        self.video_event = asyncio.Event()

        self._ws: WebSocketServerProtocol | None = None
        self._server = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            max_size=2 * 1024 * 1024,  # JPEG frames
        )
        log.info("ws server listening on %s:%d", self.host, self.port)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        peer = getattr(ws, "remote_address", "?")
        log.info("Pi connected: %s", peer)
        self._ws = ws
        try:
            async for message in ws:
                if isinstance(message, str):
                    message = message.encode("utf-8")
                try:
                    channel, payload = protocol.decode_frame(message)
                except protocol.ProtocolError as e:
                    log.warning("bad frame: %s", e)
                    continue
                self._route(channel, payload)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._ws is ws:
                self._ws = None
            log.info("Pi disconnected: %s", peer)

    def _route(self, channel: int, payload: bytes) -> None:
        if channel == protocol.CH_AUDIO:
            _put_drop_oldest(self.audio_in, payload)
        elif channel == protocol.CH_VIDEO:
            self.video_latest = payload
            self.video_event.set()
        elif channel == protocol.CH_UART:
            try:
                line = payload.decode("utf-8").strip()
            except UnicodeDecodeError:
                return
            if line:
                _put_drop_oldest(self.uart_in, line)
        elif channel == protocol.CH_CONTROL:
            try:
                _put_drop_oldest(self.control_in, protocol.decode_json(payload))
            except Exception as e:  # noqa: BLE001 - tolerate junk control frames
                log.warning("bad control frame: %s", e)

    # --- outbound -------------------------------------------------------------
    async def send_uart(self, line: str) -> bool:
        """Send one line-delimited JSON command to the Teensy (via the Pi)."""
        return await self._send(protocol.encode_uart(line))

    async def send_control(self, obj: dict) -> bool:
        return await self._send(protocol.encode_control(obj))

    async def _send(self, frame: bytes) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(frame)
            return True
        except websockets.ConnectionClosed:
            return False

    def take_latest_frame(self) -> bytes | None:
        """Consume the most recent JPEG, clearing the freshness event."""
        frame = self.video_latest
        self.video_event.clear()
        return frame
