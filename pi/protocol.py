"""Pi <-> Desktop WebSocket wire protocol. Plan §3.2.

This is a verbatim copy of ``desktop/protocol.py``; the two MUST stay in sync
(see CLAUDE.md conventions). Kept dependency-free so it runs on the Pi unchanged.

  channel 0x01  control   payload = UTF-8 JSON object
  channel 0x02  audio     payload = raw PCM, 16 kHz mono signed 16-bit LE
  channel 0x03  video     payload = JPEG bytes
  channel 0x04  uart      payload = one line-delimited JSON command/telemetry
"""

from __future__ import annotations

import json
from typing import Any

CH_CONTROL = 0x01
CH_AUDIO = 0x02
CH_VIDEO = 0x03
CH_UART = 0x04

_VALID = (CH_CONTROL, CH_AUDIO, CH_VIDEO, CH_UART)

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_DTYPE = "int16"


class ProtocolError(ValueError):
    """Raised on a malformed or empty frame."""


def encode_frame(channel: int, payload: bytes) -> bytes:
    if channel not in _VALID:
        raise ProtocolError(f"unknown channel: {channel!r}")
    return bytes((channel,)) + payload


def decode_frame(data: bytes) -> tuple[int, bytes]:
    if not data:
        raise ProtocolError("empty frame")
    channel = data[0]
    if channel not in _VALID:
        raise ProtocolError(f"unknown channel: {channel!r}")
    return channel, data[1:]


def encode_control(obj: dict[str, Any]) -> bytes:
    return encode_frame(CH_CONTROL, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def encode_uart(line: str) -> bytes:
    if not line.endswith("\n"):
        line += "\n"
    return encode_frame(CH_UART, line.encode("utf-8"))


def decode_json(payload: bytes) -> dict[str, Any]:
    return json.loads(payload.decode("utf-8"))
