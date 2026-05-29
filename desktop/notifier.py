"""Notification dispatcher for newly queued questions. Plan §8.5.

Backends (selectable via config): ``toast`` (Windows, win10toast-click),
``webhook`` (HTTP POST to ntfy/Pushover/Discord/etc.), ``silent`` (no-op).
All backends share one throttle: at most one notification per
``throttle_seconds`` (default 600 s), with the pending count carried so a burst
collapses into a single "N new questions" alert.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, backend: str = "toast", throttle_seconds: float = 600.0, webhook_url: str = ""):
        self.backend = backend
        self.throttle_seconds = throttle_seconds
        self.webhook_url = webhook_url
        self._last_sent = 0.0

    async def notify(self, count: int, last_question: str) -> bool:
        """Send a notification unless throttled. Returns True if actually sent."""
        now = time.monotonic()
        if now - self._last_sent < self.throttle_seconds:
            log.debug("notification throttled (%d pending)", count)
            return False
        self._last_sent = now

        title = f"{count} new question{'s' if count != 1 else ''} from your pet"
        try:
            if self.backend == "toast":
                await self._toast(title, last_question)
            elif self.backend == "webhook":
                await self._webhook(count, last_question)
            elif self.backend == "silent":
                return False
            else:
                log.warning("unknown notifier backend: %s", self.backend)
                return False
        except Exception as e:  # noqa: BLE001 - notifications must never crash the loop
            log.warning("notification failed: %s", e)
            return False
        return True

    async def _toast(self, title: str, body: str) -> None:
        from win10toast_click import ToastNotifier  # lazy, Windows-only

        loop = asyncio.get_running_loop()
        notifier = ToastNotifier()
        await loop.run_in_executor(
            None,
            lambda: notifier.show_toast(title, body[:120], duration=8, threaded=True),
        )

    async def _webhook(self, count: int, last_question: str) -> None:
        if not self.webhook_url:
            log.warning("webhook backend selected but webhook_url is empty")
            return
        import httpx  # lazy

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(self.webhook_url, json={"count": count, "last_question": last_question})
