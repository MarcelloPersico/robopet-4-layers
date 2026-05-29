"""UART <-> WebSocket transparent forwarder + 2 Hz heartbeat. Plan §7.1.

Runs on the Pi as the `pet-bridge` systemd service. It is dumb (Plan §2.2): it
copies bytes between the Teensy's UART and the desktop WebSocket, and injects a
local 2 Hz ``ping`` to the Teensy so the body's watchdog stays fed regardless of
whether the desktop is connected (a desktop dropout makes the pet go idle, not
unsafe; a Pi/neck failure trips the Teensy's 1500 ms soft-stop).

Upstream:   Teensy serial line  -> WS frame on channel 0x04 (UART)
Downstream: WS frame channel 0x04 -> Teensy serial line
"""

from __future__ import annotations

import asyncio
import logging
import tomllib
from pathlib import Path

import serial_asyncio

import protocol
from wsclient import run_with_reconnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

PING_INTERVAL_S = 0.5  # 2 Hz Teensy heartbeat (Plan §3.1)


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        return tomllib.load(f)


async def _serial_to_ws(reader: asyncio.StreamReader, ws) -> None:
    while True:
        line = await reader.readline()  # Teensy telemetry/events are newline-delimited
        if not line:
            raise ConnectionError("serial EOF")
        await ws.send(protocol.encode_frame(protocol.CH_UART, line))


async def _ws_to_serial(ws, writer: asyncio.StreamWriter) -> None:
    async for message in ws:
        if isinstance(message, str):
            message = message.encode("utf-8")
        try:
            channel, payload = protocol.decode_frame(message)
        except protocol.ProtocolError:
            continue
        if channel == protocol.CH_UART:
            writer.write(payload if payload.endswith(b"\n") else payload + b"\n")
            await writer.drain()


async def _heartbeat(writer: asyncio.StreamWriter) -> None:
    ping = b'{"type":"ping"}\n'
    while True:
        writer.write(ping)
        await writer.drain()
        await asyncio.sleep(PING_INTERVAL_S)


async def main() -> None:
    cfg = load_config()
    url = f"ws://{cfg['desktop']['host']}:{cfg['desktop']['port']}"

    reader, writer = await serial_asyncio.open_serial_connection(
        url=cfg["uart"]["device"], baudrate=cfg["uart"]["baud"]
    )
    log.info("opened UART %s @ %d", cfg["uart"]["device"], cfg["uart"]["baud"])

    # Heartbeat to the Teensy runs continuously, independent of the WS link.
    asyncio.create_task(_heartbeat(writer))

    async def handler(ws) -> None:
        # Run both directions; whichever finishes/raises first ends the session
        # and triggers a reconnect.
        s2w = asyncio.create_task(_serial_to_ws(reader, ws))
        w2s = asyncio.create_task(_ws_to_serial(ws, writer))
        try:
            done, pending = await asyncio.wait({s2w, w2s}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                t.result()  # re-raise to signal reconnect
        finally:
            for t in (s2w, w2s):
                t.cancel()

    await run_with_reconnect(
        url, handler,
        cfg["desktop"].get("reconnect_min_s", 1.0),
        cfg["desktop"].get("reconnect_max_s", 30.0),
    )


if __name__ == "__main__":
    asyncio.run(main())
