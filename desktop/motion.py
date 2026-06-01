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

from observatory import emit

log = logging.getLogger("motion")


def _clamp(v, lo: float = -1.0, hi: float = 1.0) -> float:
    """Single wire boundary for normalized gaze: every caller (LLM/MCP/future)
    passes through here, so look_x/look_y always reach the Teensy in [-1,1]."""
    return max(lo, min(hi, float(v)))


def _cmd_summary(obj: dict) -> str:
    """One-line, human-readable summary of an outgoing Teensy command for the
    Observatory dashboard (Plan §11). Pure formatting; never raises."""
    t = obj.get("type", "cmd")
    if t == "drive":
        return (f"drive lin={obj.get('linear', 0):.2f} ang={obj.get('angular', 0):.2f} "
                f"{obj.get('duration_ms', 0)}ms")
    if t == "play":
        return f"play {obj.get('name', '?')} x{obj.get('loops', 1)}"
    if t == "set_idle":
        return f"set_idle level={obj.get('level', 0)}"
    if t == "face":
        bits = []
        if "emotion" in obj:
            bits.append(str(obj["emotion"]))
        if "look_x" in obj or "look_y" in obj:
            bits.append(f"gaze({obj.get('look_x', 0)},{obj.get('look_y', 0)})")
        if obj.get("blink"):
            bits.append("blink")
        return "face " + (" ".join(bits) if bits else "(hold)")
    if t == "config":
        return "config " + ",".join(k for k in obj if k != "type")
    return t


class UartSink(Protocol):
    async def send_uart(self, line: str) -> bool: ...


class Motion:
    def __init__(self, sink: UartSink) -> None:
        self._sink = sink

    async def _send(self, obj: dict) -> bool:
        line = json.dumps(obj, separators=(",", ":"))
        # Observatory tap (Plan §11): the single chokepoint for every Teensy
        # command, and what drives the dashboard's live face. No-op when off.
        emit("teensy", "recv", obj.get("type", "cmd"), _cmd_summary(obj), obj)
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

    async def emote(
        self,
        emotion: str | None = None,
        intensity: float = 1.0,
        look_x: float | None = None,
        look_y: float | None = None,
        blink: bool = False,
        hold_ms: int = 0,
    ) -> bool:
        """Drive the dual-OLED "eyes" face (Plan §3.1 / §6 face subsystem).

        Emits one ``{"type":"face",...}`` line. Honors the "omitted field = keep
        current" contract: keys whose Python value is None/falsy are *not*
        serialized, so the firmware's presence flags keep the held mood and gaze.
        ``intensity`` is always sent (default 1.0); gaze is clamped to [-1,1].
        """
        # intensity is always serialized (default 1.0); emotion/look_*/blink/
        # hold_ms are dropped when None/falsy so "absent = keep current" survives
        # the wire. Key order is irrelevant to the firmware's by-name JSON parse.
        payload: dict = {"type": "face", "intensity": float(intensity)}
        if emotion is not None:
            payload["emotion"] = str(emotion)
        if look_x is not None:
            payload["look_x"] = _clamp(look_x)
        if look_y is not None:
            payload["look_y"] = _clamp(look_y)
        if blink:
            payload["blink"] = True
        if hold_ms:
            payload["hold_ms"] = int(hold_ms)
        return await self._send(payload)

    async def look(self, x: float, y: float) -> bool:
        """Point the eyes' gaze: x,y in [-1,1] (omits emotion/intensity so the
        held expression is preserved)."""
        return await self._send(
            {"type": "face", "look_x": _clamp(x), "look_y": _clamp(y)}
        )

    async def configure(self, **fields) -> bool:
        """Push drivetrain geometry / PID gains to the Teensy (startup or tuning)."""
        payload = {"type": "config"}
        payload.update({k: v for k, v in fields.items() if v is not None})
        return await self._send(payload)
