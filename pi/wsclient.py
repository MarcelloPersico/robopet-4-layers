"""Shared WebSocket client with exponential-backoff reconnect. Plan §3.2, §7.

Both Pi services (bridge, capture) are WebSocket *clients* of the desktop and
must survive desktop restarts / WiFi drops by reconnecting with backoff and
buffering nothing across drops (Plan §2.2).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection, connect

log = logging.getLogger("wsclient")

Handler = Callable[[ClientConnection], Awaitable[None]]


async def run_with_reconnect(url: str, handler: Handler, min_s: float = 1.0, max_s: float = 30.0) -> None:
    """Connect to `url` and run `handler(ws)` until it returns/raises, then
    reconnect with exponential backoff. Never returns under normal operation."""
    backoff = min_s
    while True:
        try:
            async with connect(url, max_size=4 * 1024 * 1024, ping_interval=20) as ws:
                log.info("connected to %s", url)
                backoff = min_s  # reset on a successful connect
                await handler(ws)
        except (OSError, websockets.WebSocketException) as e:
            log.warning("connection to %s failed/closed: %s", url, e)
        except asyncio.CancelledError:
            raise
        await asyncio.sleep(backoff)
        backoff = min(max_s, backoff * 2)
