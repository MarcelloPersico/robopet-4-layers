"""In-process event bus for the read-only "Observatory" dashboard. Plan §11 (observability).

A tiny pub/sub that the six core seam files tap with guarded ``emit(...)`` calls so the
four-layer live dashboard (`dashboard.py` + `dashboard.html`) can show what each tier of the
robot (Teensy body, Pi head, LM Studio brain, Claude MCP human-triage) is RECEIVING, SENDING,
and EXECUTING in real time.

Design constraints (so the running robot pays nothing when the dashboard is off):

* **Stdlib only.** The lint/test ``.venv`` has no torch / Pillow / cv2 / opencv; this module
  must stay importable there, so it imports nothing third-party.
* **``emit`` is the hot path.** Its first executable line is ``if not self.enabled: return`` --
  one attribute load + branch, which is the entire cost on a normal (dashboard-off) run.
* **``emit`` never raises.** All fan-out is wrapped; failures are logged and swallowed so a
  dashboard subscriber can never perturb a robot control loop.
* **Thread-safe.** ``asr`` / ``tts`` / ``vlm`` / the SQLite ``QueueDB`` run in executor
  threads, so ``emit`` may be called with no running loop. It detects that and routes fan-out
  through ``loop.call_soon_threadsafe`` onto the bound loop. Subscriber queues are bounded and
  drop-oldest on overflow (50 Hz telemetry must never block a producer).

Event shape -- a plain JSON-serializable dict, the single currency every layer shares::

    {
      "ts":        float,   # epoch seconds (time.time())
      "seq":       int,     # monotonic, assigned here by emit()
      "layer":     str,     # "teensy" | "pi" | "lmstudio" | "mcp"
      "direction": str,     # "recv" | "send" | "exec"
      "kind":      str,     # short tag (see KIND VOCAB below)
      "summary":   str,     # one-line human-readable string
      "detail":    dict | None,   # optional structured payload
    }

Camera thumbnails travel inside ``detail`` as ``{"thumb": "data:image/...;base64,...", ...}``.

-------------------------------------------------------------------------------------------
KIND VOCAB (drives the frontend's badge colors / filters; taps + demo + UI must all agree):

  teensy   recv:  drive | stop | play | set_idle | config | face
           send:  telemetry | event | log | pong
           exec:  active | idle | fault                  (kind == the current mode)

  pi       recv:  uart-down | ping-down
           send:  uart | video | audio | vad
           exec:  link-up | link-down

  lmstudio recv:  chat-request
           send:  chat-response
           exec:  tool:<name> | tool-result | tool-error | speak-feed

  mcp      recv:  mcp:<name>
           send:  mcp-result | queued-frame
           exec:  resolve+share | dismiss | queue-op
-------------------------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections import deque
from typing import Any, Optional

log = logging.getLogger("observatory")

# Sentinel returned by stats()/snapshot() etc.; kept here so callers can introspect defaults.
DEFAULT_RING_SIZE = 500
DEFAULT_FRAME_MAX_BYTES = 65536
DEFAULT_FRAME_MIN_INTERVAL_S = 0.5

# Per-subscriber queue bound. Generous: a stalled browser drops oldest rather than back-pressure.
_SUBSCRIBER_QUEUE_MAX = 1000


class Observatory:
    """A bounded, thread-safe pub/sub ring buffer + live fan-out for dashboard events."""

    def __init__(self, ring_size: int = DEFAULT_RING_SIZE):
        self.enabled: bool = False
        self.frame_max_bytes: int = DEFAULT_FRAME_MAX_BYTES
        self.frame_min_interval_s: float = DEFAULT_FRAME_MIN_INTERVAL_S

        self._ring: deque[dict] = deque(maxlen=ring_size)
        self._subscribers: set["asyncio.Queue[dict]"] = set()
        self._seq: int = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # emit_throttled / frame_event per-key last-emit timestamps (monotonic seconds).
        self._throttle: dict[str, float] = {}

        # Cheap running tallies for the dashboard top bar.
        self._counts: dict[str, int] = {}  # f"{layer}.{direction}" -> count
        self._total: int = 0

    # --- lifecycle / config ---------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the event loop that owns the subscriber queues (the dashboard's loop)."""
        self._loop = loop

    def configure(
        self,
        *,
        ring_size: Optional[int] = None,
        frame_max_bytes: Optional[int] = None,
        frame_min_interval_s: Optional[float] = None,
        enabled: bool = True,
    ) -> None:
        """Apply runtime settings (from the ``[dashboard]`` config block). Enables by default."""
        if ring_size is not None and ring_size != self._ring.maxlen:
            # Resize while preserving the most-recent events.
            self._ring = deque(self._ring, maxlen=ring_size)
        if frame_max_bytes is not None:
            self.frame_max_bytes = frame_max_bytes
        if frame_min_interval_s is not None:
            self.frame_min_interval_s = frame_min_interval_s
        self.enabled = enabled

    # --- subscriptions --------------------------------------------------------
    def subscribe(self) -> "asyncio.Queue[dict]":
        """Register a new bounded subscriber queue and return it (drop-oldest when full)."""
        q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[dict]") -> None:
        """Remove a subscriber queue (idempotent)."""
        self._subscribers.discard(q)

    def snapshot(self) -> list[dict]:
        """Return a copy of the recent-events ring (for a fresh subscriber's replay)."""
        return list(self._ring)

    def stats(self) -> dict:
        """Cheap tallies for the top bar: per-(layer.direction) counts, total, subscriber count."""
        return {
            "enabled": self.enabled,
            "total": self._total,
            "subscribers": len(self._subscribers),
            "ring": len(self._ring),
            "counts": dict(self._counts),
        }

    # --- the hot path ---------------------------------------------------------
    def emit(
        self,
        layer: str,
        direction: str,
        kind: str,
        summary: str,
        detail: Optional[dict] = None,
    ) -> None:
        """Publish one event. No-op (and effectively free) when the dashboard is disabled.

        Never raises: any failure in fan-out is logged and swallowed so a dashboard subscriber
        can never disturb a robot control path. Safe to call from executor threads.
        """
        if not self.enabled:
            return
        try:
            self._seq += 1
            event = {
                "ts": time.time(),
                "seq": self._seq,
                "layer": layer,
                "direction": direction,
                "kind": kind,
                "summary": summary,
                "detail": detail,
            }
            self._ring.append(event)
            self._total += 1
            key = f"{layer}.{direction}"
            self._counts[key] = self._counts.get(key, 0) + 1

            if not self._subscribers:
                return

            # Fan out on the bound loop. If we're already on it, deliver inline; otherwise
            # (executor thread) hop over via call_soon_threadsafe.
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            if running is not None and (self._loop is None or running is self._loop):
                self._fanout(event)
            elif self._loop is not None:
                self._loop.call_soon_threadsafe(self._fanout, event)
            else:
                # No bound loop and we're off-loop: nowhere safe to deliver. Ring still has it.
                pass
        except Exception:  # noqa: BLE001 - observability must never break a producer
            log.debug("observatory.emit swallowed an error", exc_info=True)

    def _fanout(self, event: dict) -> None:
        """Deliver one event to every subscriber queue (drop-oldest on full). Loop-thread only."""
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # --- throttled / frame variants ------------------------------------------
    def emit_throttled(
        self,
        key: str,
        min_interval: float,
        layer: str,
        direction: str,
        kind: str,
        summary: str,
        detail: Optional[dict] = None,
    ) -> None:
        """Like :meth:`emit` but drops calls that arrive within ``min_interval`` of the last
        accepted call *for the same ``key``* (e.g. 50 Hz telemetry -> a few per second)."""
        if not self.enabled:
            return
        now = time.monotonic()
        last = self._throttle.get(key)
        if last is not None and (now - last) < min_interval:
            return
        self._throttle[key] = now
        self.emit(layer, direction, kind, summary, detail)

    def frame_event(
        self,
        layer: str,
        direction: str,
        kind: str,
        summary: str,
        jpeg_bytes: bytes,
        *,
        key: str,
    ) -> None:
        """Emit a camera-frame event with a base64 thumbnail built from *real* JPEG bytes.

        There is no JPEG decoder in the test venv, so this cannot resize. It instead:
          * throttles by ``key`` to ``frame_min_interval_s`` (~2 fps),
          * **skips the preview** for frames larger than ``frame_max_bytes`` (the summary says
            so), still emitting the event so the stream stays visible,
          * otherwise base64-encodes the bytes into a ``data:image/jpeg`` URL the browser scales
            via CSS.
        """
        if not self.enabled:
            return
        now = time.monotonic()
        last = self._throttle.get(key)
        if last is not None and (now - last) < self.frame_min_interval_s:
            return
        self._throttle[key] = now

        size = len(jpeg_bytes)
        if size > self.frame_max_bytes:
            self.emit(layer, direction, kind, f"{summary} ({size} B, no preview)", {"bytes": size})
            return
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        self.emit(
            layer,
            direction,
            kind,
            summary,
            {"thumb": "data:image/jpeg;base64," + b64, "bytes": size},
        )


# --- module-level singleton + helpers every tap imports -----------------------
_OBSERVATORY: Optional[Observatory] = None


def get_observatory() -> Observatory:
    """Return the process-wide :class:`Observatory` singleton (created on first use)."""
    global _OBSERVATORY
    if _OBSERVATORY is None:
        _OBSERVATORY = Observatory()
    return _OBSERVATORY


def emit(
    layer: str,
    direction: str,
    kind: str,
    summary: str,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Module-level convenience for ``get_observatory().emit(...)`` (the tap entry point)."""
    get_observatory().emit(layer, direction, kind, summary, detail)


def emit_throttled(
    key: str,
    min_interval: float,
    layer: str,
    direction: str,
    kind: str,
    summary: str,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Module-level convenience for ``get_observatory().emit_throttled(...)``."""
    get_observatory().emit_throttled(key, min_interval, layer, direction, kind, summary, detail)


def frame_event(
    layer: str,
    direction: str,
    kind: str,
    summary: str,
    jpeg_bytes: bytes,
    *,
    key: str,
) -> None:
    """Module-level convenience for ``get_observatory().frame_event(...)``."""
    get_observatory().frame_event(layer, direction, kind, summary, jpeg_bytes, key=key)
