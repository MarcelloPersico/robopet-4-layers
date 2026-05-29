"""High-level motion intents -> Teensy JSON command lines, serialized through
the WebSocket UART channel. Plan §3.1.

This is the only place that drives the robot. It never touches motors directly
(Plan §2.3): every intent becomes one of the Teensy's high-level commands
(`drive`, `stop`, `play`, `set_idle`, `config`). The Teensy's own watchdog and
reflex engine own safety and idle behavior; the periodic `ping` heartbeat is
sent by the Pi bridge locally, not from here.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

log = logging.getLogger("motion")


class UartSink(Protocol):
    async def send_uart(self, line: str) -> bool: ...


class Motion:
    def __init__(self, sink: UartSink) -> None:
        self._sink = sink

    async def _send(self, obj: dict) -> bool:
        line = json.dumps(obj, separators=(",", ":"))
        ok = await self._sink.send_uart(line)
        if not ok:
            log.debug("motion command dropped (no link): %s", line)
        return ok

    async def drive(self, linear: float, angular: float, duration_ms: int = 0) -> bool:
        """Differential-drive twist: linear m/s, angular rad/s."""
        return await self._send(
            {"type": "drive", "linear": float(linear),
             "angular": float(angular), "duration_ms": int(duration_ms)}
        )

    async def stop(self) -> bool:
        return await self._send({"type": "stop"})

    async def play_animation(self, name: str, loops: int = 1) -> bool:
        return await self._send({"type": "play", "name": name, "loops": int(loops)})

    async def set_idle_intensity(self, level: float) -> bool:
        return await self._send({"type": "set_idle", "level": float(level)})

    async def configure(self, **fields) -> bool:
        """Push drivetrain geometry / PID gains to the Teensy (startup or tuning)."""
        payload = {"type": "config"}
        payload.update({k: v for k, v in fields.items() if v is not None})
        return await self._send(payload)
