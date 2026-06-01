"""Observatory event-bus unit tests. Plan §11 (observability).

Covers the contract the dashboard taps rely on: zero-cost when disabled, monotonic seq, ring
cap + replay snapshot, drop-oldest on a full subscriber queue, throttling, frame-size capping,
and off-loop (executor-thread) thread-safety via call_soon_threadsafe.
"""

import asyncio
import base64
import threading

from observatory import Observatory


def test_disabled_is_noop():
    obs = Observatory()
    assert obs.enabled is False
    q = obs.subscribe()
    obs.emit("teensy", "recv", "face", "should not appear")
    assert q.empty()
    assert obs.snapshot() == []
    assert obs.stats()["total"] == 0


async def test_emit_fans_out_and_assigns_seq():
    obs = Observatory()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    q = obs.subscribe()

    obs.emit("teensy", "recv", "face", "one", {"a": 1})
    obs.emit("pi", "send", "video", "two")

    e1 = await asyncio.wait_for(q.get(), 1.0)
    e2 = await asyncio.wait_for(q.get(), 1.0)
    assert e1["seq"] == 1 and e2["seq"] == 2
    assert e1["layer"] == "teensy" and e1["kind"] == "face" and e1["detail"] == {"a": 1}
    assert e2["direction"] == "send"
    # ts present and numeric.
    assert isinstance(e1["ts"], float)


async def test_snapshot_respects_ring_cap():
    obs = Observatory(ring_size=5)
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    for i in range(10):
        obs.emit("teensy", "exec", "idle", f"e{i}")
    snap = obs.snapshot()
    assert len(snap) == 5
    # Only the LAST 5 survive (e5..e9).
    assert [e["summary"] for e in snap] == ["e5", "e6", "e7", "e8", "e9"]


async def test_drop_oldest_on_full_subscriber_queue(monkeypatch):
    obs = Observatory()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    q = obs.subscribe()
    # Shrink the live queue so we can overflow it deterministically.
    small: asyncio.Queue = asyncio.Queue(maxsize=2)
    obs._subscribers = {small}

    for i in range(5):
        obs.emit("teensy", "send", "telemetry", f"t{i}")
    # Only the last two survive (drop-oldest).
    got = [small.get_nowait()["summary"] for _ in range(small.qsize())]
    assert got == ["t3", "t4"]
    del q


async def test_emit_throttled_drops_within_interval():
    obs = Observatory()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    q = obs.subscribe()

    obs.emit_throttled("k", 10.0, "teensy", "send", "telemetry", "first")
    obs.emit_throttled("k", 10.0, "teensy", "send", "telemetry", "dropped")
    obs.emit_throttled("other", 10.0, "teensy", "send", "telemetry", "different key ok")

    e1 = await asyncio.wait_for(q.get(), 1.0)
    e2 = await asyncio.wait_for(q.get(), 1.0)
    assert e1["summary"] == "first"
    assert e2["summary"] == "different key ok"
    assert q.empty()


async def test_emit_throttled_recovers_after_interval():
    # Two immediate calls on the same key -> only the first; after the interval elapses,
    # the next call is accepted again.
    obs = Observatory()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    q = obs.subscribe()

    obs.emit_throttled("k", 0.5, "teensy", "send", "telemetry", "first")
    obs.emit_throttled("k", 0.5, "teensy", "send", "telemetry", "dropped")
    e1 = await asyncio.wait_for(q.get(), 1.0)
    assert e1["summary"] == "first"
    assert q.empty()  # the second was throttled away

    await asyncio.sleep(0.6)  # past the 0.5 s interval (throttle uses time.monotonic)
    obs.emit_throttled("k", 0.5, "teensy", "send", "telemetry", "second")
    e2 = await asyncio.wait_for(q.get(), 1.0)
    assert e2["summary"] == "second"


async def test_frame_event_small_embeds_thumb():
    obs = Observatory()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True, frame_max_bytes=1000, frame_min_interval_s=0.0)
    q = obs.subscribe()

    jpeg = b"\xff\xd8\xff\xe0smalljpeg"
    obs.frame_event("pi", "send", "video", "frame", jpeg, key="cam")
    e = await asyncio.wait_for(q.get(), 1.0)
    assert e["detail"]["bytes"] == len(jpeg)
    assert e["detail"]["thumb"].startswith("data:image/jpeg;base64,")
    decoded = base64.b64decode(e["detail"]["thumb"].split(",", 1)[1])
    assert decoded == jpeg


async def test_frame_event_large_skips_preview():
    obs = Observatory()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True, frame_max_bytes=8, frame_min_interval_s=0.0)
    q = obs.subscribe()

    big = b"x" * 100
    obs.frame_event("pi", "send", "video", "frame", big, key="cam")
    e = await asyncio.wait_for(q.get(), 1.0)
    assert "thumb" not in e["detail"]
    assert e["detail"]["bytes"] == 100
    assert "no preview" in e["summary"]


async def test_emit_from_executor_thread_is_threadsafe():
    obs = Observatory()
    obs.bind_loop(asyncio.get_running_loop())
    obs.configure(enabled=True)
    q = obs.subscribe()

    done = threading.Event()

    def worker():
        # No running loop here -> emit must route via call_soon_threadsafe onto the bound loop.
        obs.emit("vlm-thread", "exec", "tool-result", "from thread", {"ok": True})
        done.set()

    await asyncio.get_running_loop().run_in_executor(None, worker)
    assert done.wait(1.0)
    e = await asyncio.wait_for(q.get(), 1.0)
    assert e["summary"] == "from thread" and e["detail"] == {"ok": True}


def test_emit_never_raises_on_bad_subscriber():
    obs = Observatory()
    obs.configure(enabled=True)

    class Boom:
        def full(self):
            raise RuntimeError("boom")

    obs._subscribers = {Boom()}  # type: ignore[assignment]
    # Must swallow the error rather than propagate into a producer.
    obs.emit("teensy", "recv", "drive", "x")
    # The ring still recorded it.
    assert obs.snapshot()[-1]["kind"] == "drive"
