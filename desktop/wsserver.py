"""Pi WebSocket server: accepts the single Pi connection, demultiplexes the
channel-tagged frames, and provides the outbound path to the Teensy. Plan §3.2, §8.

Inbound frames are routed to bounded asyncio queues (drop-oldest on overflow for
the non-critical media streams). Outbound motion/control is sent on the UART /
control channels.

The Pi runs two services (Plan §7): pet-bridge (UART) and pet-capture (audio +
video), so two connections arrive. Inbound frames from any connection are
demuxed by channel; outbound UART is directed to whichever connection delivers
UART traffic (the bridge).
"""

from __future__ import annotations

import asyncio
import logging

import websockets
from websockets.asyncio.server import ServerConnection, serve

import protocol
from observatory import emit, emit_throttled, frame_event

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

        self._conns: set[ServerConnection] = set()
        self._uart_peer: ServerConnection | None = None  # the bridge connection
        self._server = None

    @property
    def connected(self) -> bool:
        return bool(self._conns)

    async def start(self) -> None:
        self._server = await serve(
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

    async def _handler(self, ws: ServerConnection) -> None:
        peer = getattr(ws, "remote_address", "?")
        log.info("Pi connected: %s", peer)
        self._conns.add(ws)
        emit("pi", "exec", "link-up", f"Pi connected: {peer}", {"peer": str(peer)})
        try:
            async for message in ws:
                if isinstance(message, str):
                    message = message.encode("utf-8")
                try:
                    channel, payload = protocol.decode_frame(message)
                except protocol.ProtocolError as e:
                    log.warning("bad frame: %s", e)
                    continue
                self._route(ws, channel, payload)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._conns.discard(ws)
            if self._uart_peer is ws:
                self._uart_peer = None
            log.info("Pi disconnected: %s", peer)
            emit("pi", "exec", "link-down", f"Pi disconnected: {peer}", {"peer": str(peer)})

    def _route(self, ws: ServerConnection, channel: int, payload: bytes) -> None:
        # Observatory taps (Plan §11): the link's view of what the Pi sends up.
        # All no-op when the dashboard is disabled; media taps are throttled.
        if channel == protocol.CH_AUDIO:
            _put_drop_oldest(self.audio_in, payload)
            emit_throttled("pi_audio", 0.5, "pi", "send", "audio",
                           f"PCM {len(payload)}B", {"bytes": len(payload)})
        elif channel == protocol.CH_VIDEO:
            self.video_latest = payload
            self.video_event.set()
            frame_event("pi", "send", "video", "frame", payload, key="pi_video")
        elif channel == protocol.CH_UART:
            self._uart_peer = ws  # this connection is the bridge
            try:
                line = payload.decode("utf-8").strip()
            except UnicodeDecodeError:
                return
            if line:
                _put_drop_oldest(self.uart_in, line)
                emit("pi", "send", "uart", line[:120], {"line": line})
        elif channel == protocol.CH_CONTROL:
            try:
                obj = protocol.decode_json(payload)
            except Exception as e:  # noqa: BLE001 - tolerate junk control frames
                log.warning("bad control frame: %s", e)
                return
            _put_drop_oldest(self.control_in, obj)
            emit("pi", "send", "vad", str(obj)[:80], obj)

    # --- outbound -------------------------------------------------------------
    async def send_uart(self, line: str) -> bool:
        """Send one line-delimited JSON command to the Teensy via the bridge."""
        # Observatory tap (Plan §11): the command the Pi forwards down to the
        # Teensy (the link's RECEIVING view). No-op when the dashboard is off.
        emit("pi", "recv", "uart-down", line[:120], {"line": line})
        peer = self._uart_peer or next(iter(self._conns), None)
        return await self._send_to(peer, protocol.encode_uart(line))

    async def send_control(self, obj: dict) -> bool:
        """Broadcast a control message to every connected Pi service."""
        frame = protocol.encode_control(obj)
        results = [await self._send_to(ws, frame) for ws in list(self._conns)]
        return any(results)

    async def _send_to(self, ws: ServerConnection | None, frame: bytes) -> bool:
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
