"""Dashboard server + demo-feeder tests. Plan §11 (observability).

Boots the single-listener HTTP+WS server on an ephemeral port (like test_wsserver.py): asserts
the HTML page and /health over HTTP, the WS replay-then-live-event protocol, and that the demo
feeder exercises all four layers and produces a camera thumbnail.

These tests drive the *process-wide* Observatory singleton (``get_observatory()``), because the
module-level ``emit(...)`` helpers and ``demo_feeder`` -- like the real orchestrator taps -- all
publish to that singleton. The server, the publisher, and the subscriber must therefore share
the one bus, so each test starts by resetting the singleton (``_singleton()``) for isolation.
"""

import asyncio
import contextlib
import json

import httpx
import pytest
import websockets
from websockets.asyncio.client import connect

import dashboard
import observatory
from observatory import Observatory, emit, get_observatory


@pytest.fixture(autouse=True)
def _restore_singleton():
    """Save/restore the process-wide Observatory singleton around each test.

    The dashboard tests swap in a fresh singleton (so the server, ``emit``, and ``demo_feeder``
    share one bus); this restores whatever was there before so a left-enabled singleton can't
    leak into the rest of the desktop suite (the seam modules import ``get_observatory``).
    """
    saved = observatory._OBSERVATORY
    try:
        yield
    finally:
        observatory._OBSERVATORY = saved


def _singleton() -> Observatory:
    """Reset and return the process-wide Observatory singleton (fresh per test for isolation).

    ``demo_feeder`` and the module-level ``emit``/``emit_throttled``/``frame_event`` helpers all
    target this singleton, so the dashboard server under test must be bound to the same object.
    """
    obs = Observatory()
    observatory._OBSERVATORY = obs
    return obs


async def _boot(obs):
    server = await dashboard.start_server(obs, "127.0.0.1", 0, replay=50)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_http_routes():
    obs = _singleton()
    server, port = await _boot(obs)
    try:
        assert obs.enabled is True
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.get("/")
            assert r.status_code == 200
            assert "text/html" in r.headers["content-type"]
            assert r.headers["cache-control"] == "no-store"

            h = await c.get("/health")
            assert h.status_code == 200
            assert "application/json" in h.headers["content-type"]
            body = h.json()
            assert body["ok"] is True and "subs" in body

            nf = await c.get("/does-not-exist")
            assert nf.status_code == 404
    finally:
        server.close()
        await server.wait_closed()


async def test_ws_replay_then_live_event():
    obs = _singleton()
    # Pre-seed one event BEFORE connecting so the replay batch is non-empty.
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    obs.emit("mcp", "recv", "mcp:list_pending_questions", "seeded")

    server, port = await _boot(obs)
    try:
        async with connect(f"ws://127.0.0.1:{port}/events") as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
            assert first["type"] == "replay"
            assert any(e["summary"] == "seeded" for e in first["events"])

            await asyncio.sleep(0.05)  # let the handler register its subscriber
            emit("teensy", "recv", "face", "live-one", {"emotion": "happy"})
            evt = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
            assert "type" not in evt  # a raw event, not another replay
            assert evt["layer"] == "teensy" and evt["summary"] == "live-one"
            assert isinstance(evt["seq"], int)
    finally:
        server.close()
        await server.wait_closed()


async def test_replay_carries_pre_connect_events():
    """Events emitted before a client connects appear in that client's replay batch."""
    obs = _singleton()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    emit("pi", "exec", "link-up", "before-a", {"peer": "10.0.0.1"})
    emit("lmstudio", "recv", "chat-request", "before-b", {"messages": 3, "tools": 8})

    server, port = await _boot(obs)
    try:
        async with connect(f"ws://127.0.0.1:{port}/events") as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
            assert first["type"] == "replay"
            summaries = [e["summary"] for e in first["events"]]
            assert "before-a" in summaries
            assert "before-b" in summaries
    finally:
        server.close()
        await server.wait_closed()


async def test_two_subscribers_both_receive():
    obs = _singleton()
    server, port = await _boot(obs)
    try:
        async with connect(f"ws://127.0.0.1:{port}/events") as a, \
                connect(f"ws://127.0.0.1:{port}/events") as b:
            for ws in (a, b):
                first = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
                assert first["type"] == "replay"
            await asyncio.sleep(0.05)
            emit("pi", "exec", "link-up", "both-see-this")
            for ws in (a, b):
                evt = json.loads(await asyncio.wait_for(ws.recv(), 2.0))
                assert evt["summary"] == "both-see-this"
    finally:
        server.close()
        await server.wait_closed()


async def test_demo_feeder_exercises_all_layers_with_thumbnail():
    obs = _singleton()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    q = obs.subscribe()

    feeder = asyncio.create_task(dashboard.demo_feeder(obs))
    try:
        layers = set()
        thumb_seen = False
        # The feeder gates lmstudio at frame%8 and mcp at frame%13 on 0.3-1.0 s jittered sleeps,
        # so reaching all four layers can take ~15 s worst case. Drain until both conditions
        # hold, with a generous deadline so this never flakes.
        deadline = asyncio.get_running_loop().time() + 30.0
        while not {"teensy", "pi", "lmstudio", "mcp"}.issubset(layers) or not thumb_seen:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"timed out; saw layers={layers} thumb={thumb_seen}")
            evt = await asyncio.wait_for(q.get(), 10.0)
            layers.add(evt["layer"])
            detail = evt.get("detail") or {}
            if isinstance(detail, dict) and str(detail.get("thumb", "")).startswith("data:image/"):
                thumb_seen = True
        assert {"teensy", "pi", "lmstudio", "mcp"}.issubset(layers)
        assert thumb_seen
    finally:
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder


async def test_handler_unsubscribes_on_disconnect():
    obs = _singleton()
    server, port = await _boot(obs)
    try:
        async with connect(f"ws://127.0.0.1:{port}/events") as ws:
            await asyncio.wait_for(ws.recv(), 2.0)  # replay
            await asyncio.sleep(0.05)
            assert obs.stats()["subscribers"] == 1
        # After the client closes, the handler's finally should unsubscribe.
        for _ in range(40):
            if obs.stats()["subscribers"] == 0:
                break
            await asyncio.sleep(0.05)
        assert obs.stats()["subscribers"] == 0
    finally:
        server.close()
        await server.wait_closed()


def test_module_exposes_documented_surface():
    # Smoke: the module exposes the documented entry points used by the orchestrator + CLI.
    assert hasattr(dashboard, "start_server")
    assert hasattr(dashboard, "serve_dashboard")
    assert hasattr(dashboard, "demo_feeder")
    assert hasattr(dashboard, "main")
    assert get_observatory() is not None
    assert websockets  # imported for ConnectionClosed handling
