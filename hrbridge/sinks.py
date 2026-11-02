"""Push-style outputs: Discord webhooks and generic JSON webhooks.

These post on their own when something happens, so a bot in the channel sees
the heart rate without having to call a tool.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import aiohttp

from .config import DiscordSinkConfig, WebhookSinkConfig
from .events import HREvent

log = logging.getLogger("hrbridge.sinks")

# change and zone share one cooldown so a crossing right after a change
# doesn't double-post; the others are rate-limited separately.
_COOLDOWN_GROUP = {"change": "value", "zone": "value", "spike": "spike"}


class Poster:
    kind = "webhook"
    _MAX_QUEUE = 50
    _GAP = 0.5  # seconds between posts; Discord allows ~5 per 2 s per webhook

    def __init__(self, cfg: DiscordSinkConfig | WebhookSinkConfig):
        self.cfg = cfg
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._MAX_QUEUE)
        self._last: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self.posted = 0
        self.failed = 0
        self.last_error = ""

    @property
    def label(self) -> str:
        url = self.cfg.url
        m = re.match(r"(https?://[^/]+)", url)
        return f"{self.kind} {m.group(1) if m else url[:30]}"

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self._task = asyncio.create_task(self._drain())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    def status(self) -> dict:
        return {
            "type": self.kind,
            "events": self.cfg.events,
            "posted": self.posted,
            "failed": self.failed,
            "last_error": self.last_error or None,
        }

    # ------------------------------------------------------------ selection

    def handle(self, events: list[HREvent]) -> None:
        """Pick the most important event this sink wants and queue it."""
        for event in events:
            if event.type not in self.cfg.events:
                continue
            group = _COOLDOWN_GROUP.get(event.type)
            if group is not None:
                now = time.monotonic()
                if now - self._last.get(group, float("-inf")) < self.cfg.cooldown_seconds:
                    continue
                self._last[group] = now
            self.enqueue(self.payload(event))
            return

    def payload(self, event: HREvent) -> dict:
        return event.to_dict()

    def enqueue(self, payload: dict) -> None:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._queue.get_nowait()  # newest state matters more than the oldest
            self._queue.put_nowait(payload)

    # ------------------------------------------------------------- transport

    async def _drain(self) -> None:
        while True:
            payload = await self._queue.get()
            ok, detail = await self.send(payload)
            if ok:
                self.posted += 1
            else:
                self.failed += 1
                self.last_error = detail
                log.warning("%s: %s", self.label, detail)
            await asyncio.sleep(self._GAP)

    async def send(self, payload: dict) -> tuple[bool, str]:
        assert self._session is not None
        headers = getattr(self.cfg, "headers", {}) or {}
        for attempt in range(3):
            try:
                async with self._session.post(self.cfg.url, json=payload, headers=headers) as resp:
                    if resp.status == 429 and attempt < 2:
                        try:
                            wait = float((await resp.json()).get("retry_after", 1.0))
                        except Exception:  # noqa: BLE001
                            wait = 1.0
                        await asyncio.sleep(min(wait, 10.0))
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:200]
                        return False, f"HTTP {resp.status}: {body}"
                    return True, f"HTTP {resp.status}"
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return False, f"{type(e).__name__}: {e}"
        return False, "rate limited"

    async def send_test(self, text: str) -> tuple[bool, str]:
        own_session = self._session is None
        if own_session:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            return await self.send(self.test_payload(text))
        finally:
            if own_session:
                await self._session.close()
                self._session = None

    def test_payload(self, text: str) -> dict:
        return {"type": "test", "text": text}


class WebhookPoster(Poster):
    kind = "webhook"


class DiscordPoster(Poster):
    kind = "discord"

    def payload(self, event: HREvent) -> dict:
        cfg: DiscordSinkConfig = self.cfg
        content = event.render(cfg.templates)
        wake = event.type in cfg.wake_events
        if wake:
            content = " ".join(p for p in (cfg.wake_prefix, cfg.mention, content) if p)
        if cfg.prefix:
            content = f"{cfg.prefix} {content}"
        return self._message(content, ping=wake)

    def test_payload(self, text: str) -> dict:
        return self._message(text, ping=False)

    def _message(self, content: str, ping: bool) -> dict:
        cfg: DiscordSinkConfig = self.cfg
        msg: dict = {"content": content[:2000]}
        if cfg.username:
            msg["username"] = cfg.username
        if cfg.avatar_url:
            msg["avatar_url"] = cfg.avatar_url
        # Never let a template ping @everyone; only the configured mention, and
        # only on wake events.
        allowed: dict = {"parse": []}
        if ping and cfg.mention:
            if users := re.findall(r"<@!?(\d+)>", cfg.mention):
                allowed["users"] = users
            if roles := re.findall(r"<@&(\d+)>", cfg.mention):
                allowed["roles"] = roles
        msg["allowed_mentions"] = allowed
        return msg

    @property
    def label(self) -> str:
        m = re.search(r"/webhooks/(\d+)/", self.cfg.url)
        return f"discord webhook {m.group(1) if m else '?'}"
