"""Read-only "Observatory" dashboard server + standalone demo simulator. Plan §11 (observability).

One ``websockets`` listener serves both the static HTML page (over plain HTTP) and the live
event stream (over a WebSocket at ``/events``), so the whole dashboard is a single port. The
browser connects to ``/events``, receives one batched ``{"type":"replay","events":[...]}``
message of recent history, then one raw event dict per message thereafter.

Stdlib + ``websockets`` only -- no torch / Pillow / cv2 -- so this stays importable and
runnable in the lint/test venv. Run it alone (no orchestrator/hardware) with::

    python dashboard.py                 # dashboard + demo simulator on 127.0.0.1:8772
    python dashboard.py --no-demo       # empty; attach a real orchestrator's Observatory
    python dashboard.py --open          # also open a browser tab

When embedded in the orchestrator, :func:`serve_dashboard` is awaited as a task; the demo
feeder is not used there (real taps supply the events).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import math
import random
import socket
import time
import webbrowser
from pathlib import Path

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from observatory import Observatory, emit, emit_throttled, get_observatory

log = logging.getLogger("dashboard")

_HTML_PATH = Path(__file__).parent / "dashboard.html"
# Served if dashboard.html is missing, so the server can never crash at startup.
_HTML_STUB = b"<!doctype html><title>Observatory</title>loading..."


def _load_html() -> bytes:
    try:
        return _HTML_PATH.read_bytes()
    except OSError:
        log.warning("dashboard.html not found at %s; serving a loading stub", _HTML_PATH)
        return _HTML_STUB


# --- server -------------------------------------------------------------------
async def start_server(obs: Observatory, host: str, port: int, *, replay: int = 200):
    """Bind ``obs`` to the running loop, enable it, and return an already-listening server.

    The returned object is a ``websockets`` server; tests pass ``port=0`` and read the bound
    port from ``server.sockets[0].getsockname()[1]``. The caller owns shutdown
    (``server.close()`` / ``await server.wait_closed()``); :func:`serve_dashboard` wraps that.
    """
    obs.bind_loop(asyncio.get_running_loop())
    obs.enabled = True

    html = _load_html()

    def process_request(connection, request):
        """Route by path: serve HTML/health over HTTP, let ``/events`` upgrade to a WebSocket."""
        path = request.path.split("?", 1)[0]
        if path == "/events":
            return None  # proceed with the WebSocket handshake
        if path in ("/", "/index.html"):
            headers = Headers(
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(html))),
                    ("Cache-Control", "no-store"),
                ]
            )
            return Response(200, "OK", headers, html)
        if path == "/health":
            body = json.dumps({"ok": True, "subs": obs.stats()["subscribers"]}).encode("utf-8")
            headers = Headers(
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                ]
            )
            return Response(200, "OK", headers, body)
        return Response(404, "Not Found", Headers([("Content-Type", "text/plain")]), b"not found")

    async def handler(connection):
        """Stream events to one browser: a batched replay first, then one event per message.

        The browser never sends us anything, so this is parked on ``q.get()`` between events.
        We must still notice when the browser disconnects -- otherwise this coroutine would
        live forever on an empty queue and ``server.wait_closed()`` (shutdown) would hang. So
        each wait races the next event against the connection closing.
        """
        q = obs.subscribe()
        closed = asyncio.ensure_future(connection.wait_closed())
        try:
            replay_msg = {"type": "replay", "events": obs.snapshot()[-replay:]}
            await connection.send(json.dumps(replay_msg))
            while True:
                get = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait(
                    {get, closed}, return_when=asyncio.FIRST_COMPLETED
                )
                if closed in done:
                    get.cancel()  # a cancelled Queue.get() removes nothing from the queue
                    break
                evt = get.result()
                await connection.send(json.dumps(evt))
        except websockets.ConnectionClosed:
            pass
        finally:
            closed.cancel()
            obs.unsubscribe(q)

    server = await serve(
        handler,
        host,
        port,
        process_request=process_request,
        max_size=2 * 1024 * 1024,  # replay batches with thumbnails can be large
    )
    return server


async def serve_dashboard(obs: Observatory, host: str, port: int, *, replay: int = 200) -> None:
    """Start the dashboard and serve until cancelled, then disable the bus and close cleanly."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "dashboard bound to %s:%d -- read-only A/V thumbnails + telemetry are LAN-exposed",
            host,
            port,
        )
    server = await start_server(obs, host, port, replay=replay)
    log.info("observatory dashboard listening on http://%s:%d", host, port)
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        obs.enabled = False
        server.close()
        await server.wait_closed()
        raise


# --- demo simulator -----------------------------------------------------------
# The 15 emotions the firmware's EmotionLibrary defines; the demo cycles the hero face through
# all of them so the dashboard's eyes visibly emote during autonomous verification.
_EMOTIONS = [
    "neutral", "happy", "curious", "surprised", "sad", "love", "focused", "sleepy",
    "excited", "scared", "angry", "suspicious", "bored", "dizzy", "wink",
]
_ANIMATIONS = ["nod", "wiggle", "spin", "tilt", "bounce"]

# A short voice turn the lmstudio lane streams word-by-word via speak-feed.
_DESK_SENTENCE = "I can see a coffee mug and a notebook on your desk.".split()
_LEARNED_SENTENCE = "Thanks, I will remember that is your blue travel mug.".split()


def _svg_thumb(frame: int) -> str:
    """Build a tiny live-looking 'camera frame' as an SVG data URL (no JPEG encoder available).

    A gradient rect with a frame counter and a dot that orbits each frame, so the thumbnail
    visibly updates in the dashboard's Pi/MCP SENDING panes.
    """
    # Dot orbits a small circle so successive frames clearly differ.
    angle = (frame % 36) * (math.pi / 18.0)
    dx = 80 + 52 * math.cos(angle)
    dy = 60 + 38 * math.sin(angle)
    hue = (frame * 7) % 360
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='160' height='120'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        f"<stop offset='0' stop-color='hsl({hue},60%,18%)'/>"
        f"<stop offset='1' stop-color='hsl({(hue + 80) % 360},70%,32%)'/>"
        "</linearGradient></defs>"
        "<rect width='160' height='120' fill='url(#g)'/>"
        f"<circle cx='{dx:.1f}' cy='{dy:.1f}' r='9' fill='#7fffd4' opacity='0.9'/>"
        "<rect x='3' y='3' width='154' height='114' fill='none' stroke='#ffffff' "
        "stroke-opacity='0.25'/>"
        f"<text x='8' y='112' font-family='monospace' font-size='12' fill='#cfe'>"
        f"CAM #{frame:04d}</text>"
        "</svg>"
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + b64


def _telemetry(emotion: str, mode: str, t: float, frame: int) -> dict:
    """A telemetry snapshot whose emotion matches the last face and whose wheels move on a sine."""
    vel = math.sin(t * 1.7)
    spin = math.sin(t * 0.9)
    vel_l = round((vel + 0.3 * spin) * 0.6, 3)
    vel_r = round((vel - 0.3 * spin) * 0.6, 3)
    if mode == "idle":
        vel_l = round(vel_l * 0.05, 3)
        vel_r = round(vel_r * 0.05, 3)
    return {
        "type": "telemetry",
        "enc_l": frame * 12,
        "enc_r": frame * 12,
        "vel_l": vel_l,
        "vel_r": vel_r,
        "duty_l": round(max(-0.9, min(0.9, vel_l)), 3),
        "duty_r": round(max(-0.9, min(0.9, vel_r)), 3),
        "link_age_ms": random.randint(20, 240),
        "mode": mode,
        "emotion": emotion,
    }


async def demo_feeder(obs: Observatory) -> None:
    """Emit synthetic events for all four layers forever, on a jittered ~0.3-1.0 s loop.

    Drives the hero face through the full emotion set with sweeping gaze + occasional blinks,
    cycles telemetry through idle/active/fault, streams a Pi camera/audio/VAD feed, runs a
    recurring LM Studio voice turn, and a recurring MCP human-resolution that feeds an answer
    back to LM Studio (so the "learn answers back" cross-layer flow lights up).
    """
    del obs  # we use the module-level emit helpers (the singleton the server enabled)
    random.seed()
    frame = 0
    emotion = "neutral"
    mode = "idle"
    # Startup: the Pi link comes up.
    emit("pi", "exec", "link-up", "Pi link established", {"peer": "192.168.1.42:8765"})

    while True:
        frame += 1
        now = time.time()

        # --- teensy: a face command (the hero driver) ---------------------------------------
        # Hold each emotion for a beat (don't reroll every frame) so the tween settles and the
        # expression is readable; gaze still moves every frame so the eyes stay alive.
        if frame == 1 or random.random() < 0.4:
            emotion = random.choice(_EMOTIONS)
        look_x = round(math.sin(now * 0.8), 2)
        look_y = round(math.cos(now * 0.6) * 0.7, 2)
        face = {
            "type": "face",
            "emotion": emotion,
            "look_x": look_x,
            "look_y": look_y,
            "intensity": round(random.uniform(0.7, 1.0), 2),
        }
        if random.random() < 0.22:
            face["blink"] = True
        if random.random() < 0.25:
            face["hold_ms"] = random.choice([400, 800, 1200])
        emit("teensy", "recv", "face", f"face {emotion} gaze=({look_x},{look_y})", face)

        # Occasional drive / play commands so those lanes light up too.
        if random.random() < 0.35:
            drive = {
                "type": "drive",
                "linear": round(random.uniform(-0.4, 0.6), 2),
                "angular": round(random.uniform(-1.0, 1.0), 2),
                "duration_ms": random.choice([300, 600, 900]),
            }
            emit(
                "teensy",
                "recv",
                "drive",
                f"drive lin={drive['linear']} ang={drive['angular']}",
                drive,
            )
        if random.random() < 0.25:
            anim = random.choice(_ANIMATIONS)
            emit("teensy", "recv", "play", f"play {anim}", {"type": "play", "name": anim})

        # --- teensy: telemetry + derived exec -----------------------------------------------
        roll = random.random()
        mode = "fault" if roll < 0.08 else ("active" if roll < 0.6 else "idle")
        tele = _telemetry(emotion, mode, now, frame)
        emit_throttled(
            "demo.telemetry",
            0.25,
            "teensy",
            "send",
            "telemetry",
            f"mode={mode} vel_l={tele['vel_l']} vel_r={tele['vel_r']} emo={emotion}",
            tele,
        )
        emit(
            "teensy",
            "exec",
            mode,
            f"{mode}: vel_l={tele['vel_l']} vel_r={tele['vel_r']} emo={emotion}",
            {
                "mode": mode,
                "emotion": emotion,
                "vel_l": tele["vel_l"],
                "vel_r": tele["vel_r"],
                "duty_l": tele["duty_l"],
                "duty_r": tele["duty_r"],
                "link_age_ms": tele["link_age_ms"],
            },
        )

        # --- pi: camera (SVG thumb) + audio + VAD + uart passthrough ------------------------
        emit_throttled(
            "demo.video",
            0.5,
            "pi",
            "send",
            "video",
            f"JPEG frame #{frame:04d}",
            {"thumb": _svg_thumb(frame), "bytes": 18000 + frame % 4000},
        )
        if random.random() < 0.5:
            emit("pi", "send", "audio", "PCM chunk 16kHz", {"bytes": random.choice([640, 1280])})
        if random.random() < 0.18:
            ev = random.choice(["start", "end"])
            emit("pi", "send", "vad", f"VAD {ev}", {"event": ev})
        if random.random() < 0.3:
            line = json.dumps(tele, separators=(",", ":"))
            emit("pi", "send", "uart", "telemetry up", {"line": line})
        if random.random() < 0.2:
            down = json.dumps(face, separators=(",", ":"))
            emit("pi", "recv", "uart-down", "face cmd down", {"line": down})

        # Occasionally simulate a Pi link reconnect so the Pi EXECUTING lane shows life.
        if frame % 18 == 0:
            emit("pi", "exec", "link-down", "Pi link lost - reconnecting", {"peer": "192.168.1.42:8765"})
            await asyncio.sleep(0.2)
            emit("pi", "exec", "link-up", "Pi link re-established", {"peer": "192.168.1.42:8765"})

        # --- lmstudio: a recurring voice turn (every ~8 frames) -----------------------------
        if frame % 8 == 0:
            await _voice_turn()

        # --- mcp: a recurring human resolution (every ~13 frames) ---------------------------
        if frame % 13 == 0:
            await _human_resolution(frame)

        await asyncio.sleep(random.uniform(0.3, 1.0))


async def _voice_turn() -> None:
    """LM Studio lane: chat-request -> tool:see -> streamed speak-feed -> chat-response."""
    emit(
        "lmstudio",
        "recv",
        "chat-request",
        "user: what is on my desk?",
        {"messages": 7, "tools": 8, "user": "what is on my desk?"},
    )
    await asyncio.sleep(0.15)
    emit("lmstudio", "exec", "tool:see", "tool see()", {})
    await asyncio.sleep(0.2)
    emit(
        "lmstudio",
        "exec",
        "tool-result",
        "see -> a desk with a mug and notebook",
        {"result": "a wooden desk with a coffee mug and an open notebook"},
    )
    spoken = ""
    for word in _DESK_SENTENCE:
        spoken = (spoken + " " + word).strip()
        emit("lmstudio", "exec", "speak-feed", f"speak: ...{word}", {"text": word})
        await asyncio.sleep(0.06)
    emit(
        "lmstudio",
        "send",
        "chat-response",
        f"reply ({len(spoken)} chars), 1 tool call",
        {"chars": len(spoken), "tool_calls": ["see"]},
    )


async def _human_resolution(frame: int) -> None:
    """MCP lane: human pulls a pending question, resolves it, and the answer feeds back to LM."""
    qid = 100 + (frame // 13)
    emit(
        "mcp",
        "recv",
        "mcp:next_pending_question",
        "human: next_pending_question()",
        {},
    )
    await asyncio.sleep(0.1)
    emit(
        "mcp",
        "send",
        "queued-frame",
        f"pending #{qid} frame",
        {"thumb": _svg_thumb(frame), "id": qid},
    )
    emit(
        "mcp",
        "send",
        "mcp-result",
        f"pending #{qid}: what is this blue object?",
        {"result": "what is this blue object on the desk?"},
    )
    await asyncio.sleep(0.15)
    emit(
        "mcp",
        "exec",
        "resolve+share",
        f"resolved #{qid} -> blue travel mug",
        {"id": qid, "topic": "blue object", "resolution": "your blue travel mug"},
    )
    # "Learn answers back": the resolution is handed to the agent, which speaks it.
    await asyncio.sleep(0.1)
    spoken = ""
    for word in _LEARNED_SENTENCE:
        spoken = (spoken + " " + word).strip()
        emit("lmstudio", "exec", "speak-feed", f"speak: ...{word}", {"text": word})
        await asyncio.sleep(0.05)
    emit(
        "lmstudio",
        "send",
        "chat-response",
        f"learned-answer reply ({len(spoken)} chars)",
        {"chars": len(spoken), "tool_calls": []},
    )


# --- optional mDNS (.local) advertising ---------------------------------------
def _primary_lan_ip() -> str:
    """The LAN IP on the default route (skips WSL/Hyper-V virtual adapters)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet is sent; this just selects the egress iface
        return s.getsockname()[0]
    finally:
        s.close()


async def _advertise_mdns(name: str, port: int, ip: str):
    """Publish ``<name>.local`` -> ``ip`` over mDNS so phones/laptops can reach the dashboard
    by a memorable name instead of the IP. Lazy-imports ``zeroconf`` (optional dep). Returns
    ``(aiozc, info)`` for cleanup, or ``None`` if zeroconf isn't installed.

    Uses the async API on purpose: the sync ``Zeroconf.register_service`` deadlocks when
    called from inside a running event loop (raises ``EventLoopBlocked``), so we await."""
    try:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError:
        log.warning("zeroconf not installed; skipping mDNS (pip install zeroconf) -- use the IP")
        return None
    aiozc = AsyncZeroconf()
    info = ServiceInfo(
        "_http._tcp.local.",
        f"{name}._http._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=port,
        server=f"{name}.local.",
        properties={"path": "/"},
    )
    await aiozc.async_register_service(info)
    return aiozc, info


# --- CLI ----------------------------------------------------------------------
async def _amain(args: argparse.Namespace) -> None:
    obs = get_observatory()
    mdns = None
    if getattr(args, "mdns_name", None):
        ip = _primary_lan_ip()
        mdns = await _advertise_mdns(args.mdns_name, args.port, ip)
        if mdns:
            log.info("mDNS: http://%s.local:%d  ->  %s", args.mdns_name, args.port, ip)
    tasks = [asyncio.create_task(serve_dashboard(obs, args.host, args.port), name="dashboard")]
    if args.demo:
        tasks.append(asyncio.create_task(demo_feeder(obs), name="demo"))
    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}/")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if mdns:
            aiozc, info = mdns
            with contextlib.suppress(Exception):
                await aiozc.async_unregister_service(info)
                await aiozc.async_close()


def main() -> None:
    """argparse entry point: serve the dashboard (+ demo simulator by default)."""
    parser = argparse.ArgumentParser(description="Jarvis Observatory dashboard (read-only).")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8772, help="bind port (default 8772)")
    parser.add_argument(
        "--demo",
        dest="demo",
        action="store_true",
        default=True,
        help="run the built-in four-layer demo simulator (default ON)",
    )
    parser.add_argument(
        "--no-demo",
        dest="demo",
        action="store_false",
        help="serve an empty dashboard (attach a real orchestrator instead)",
    )
    parser.add_argument("--open", action="store_true", help="open a browser tab on startup")
    parser.add_argument("--mdns-name", default=None,
                        help="advertise http://<name>.local via mDNS (e.g. --mdns-name elena)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
