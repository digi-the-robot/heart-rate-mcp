"""MCP server (stdio): lets an AI client read the heart rate whenever it wants.

By default it relays to the bridge daemon and starts one if none is running
(mcp.daemon = "auto"). "embedded" runs the sources inside this process instead,
which is simplest for a single Claude Desktop install but means every MCP
client opens its own sensor connection and no webhooks are sent.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import aiohttp
from mcp.server.fastmcp import FastMCP

from .bridge import Bridge
from .config import Config
from .daemon import DaemonClient, ensure_daemon

log = logging.getLogger("hrbridge.mcp")

INSTRUCTIONS = """\
These tools read the user's live heart rate from their sensor (chest strap, watch,
or an app like Pulsoid). Use heart_rate_now for "right now", heart_rate_history
for how it changed over a period, and heart_rate_events for notable moments
(spikes, zone crossings, sensor connecting or disconnecting). Heart rate is personal
body data: mention it when it's relevant or asked about, and don't diagnose.
"""


class Backend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mode = cfg.mcp.daemon
        self.local: Bridge | None = None
        self.client: DaemonClient | None = None
        self._ready: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        # Connect in the background so the MCP handshake isn't held up by a
        # daemon launch; the first tool call waits for it.
        self._ready = asyncio.create_task(self._connect())

    async def _connect(self) -> None:
        if self.mode == "embedded":
            await self._start_local()
            return
        self.client = DaemonClient(self.cfg)
        if self.mode == "connect":
            if not await self.client.hello():
                log.warning("no bridge daemon at %s yet (mcp.daemon = connect)", self.client.base)
            return
        try:
            hello = await ensure_daemon(self.cfg, self.client)
        except Exception:  # noqa: BLE001
            log.exception("starting the bridge daemon failed")
            hello = None
        if hello is None:
            log.warning("couldn't reach or start the bridge daemon; running sources inside the MCP server")
            await self._start_local()

    async def _start_local(self) -> None:
        self.local = Bridge(self.cfg, serve_api=False, run_sinks=False)
        await self.local.start()

    async def stop(self) -> None:
        if self._ready is not None and not self._ready.done():
            self._ready.cancel()
        if self.local is not None:
            await self.local.stop()
        if self.client is not None:
            await self.client.close()

    async def call(self, op: str, **params) -> dict:
        if self._ready is not None:
            await asyncio.shield(self._ready)
        if self.local is not None:
            return getattr(self.local, op)(**params)
        assert self.client is not None
        try:
            return await self.client.get(op, **params)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            pass
        # The daemon went away (crashed, stopped, PC slept...). Bring it back once.
        if self.mode == "auto":
            async with self._lock:
                if await ensure_daemon(self.cfg, self.client):
                    try:
                        return await self.client.get(op, **params)
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                        pass
        return {
            "status": "unavailable",
            "summary": f"The heart rate bridge isn't running at {self.client.base}. "
                       "Start it with `hrbridge run` (or `python run.py run`).",
        }


def build_server(cfg: Config) -> FastMCP:
    backend = Backend(cfg)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        await backend.start()
        try:
            yield
        finally:
            await backend.stop()

    mcp = FastMCP("heart-rate", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool()
    async def heart_rate_now() -> dict:
        """Get the user's current heart rate.

        Returns bpm, status ("live", "stale" = signal lost, "no_data"), how old the
        reading is, the trend over the last minute (rising/falling/steady, in
        bpm/min), min/avg/max for the last 1, 10 and 60 minutes, which sensor it
        came from, and a one-line plain-English summary.
        """
        return await backend.call("heart_rate")

    @mcp.tool()
    async def heart_rate_history(minutes: float = 10, points: int = 30) -> dict:
        """Get heart rate over a past period, averaged into evenly spaced points.

        Args:
            minutes: How far back to look (1 to 1440). Default 10.
            points: How many averaged points to return (2 to 240). Default 30.
        """
        return await backend.call("history", minutes=minutes, points=points)

    @mcp.tool()
    async def heart_rate_events(minutes: float = 60, limit: int = 20) -> dict:
        """Get recent notable heart rate moments: sudden spikes, crossing a zone
        threshold (e.g. above 100 or back under it), big changes, and the sensor
        connecting or losing signal. Each event has a time and a readable text.

        Args:
            minutes: How far back to look. Default 60.
            limit: Most recent N events to return. Default 20.
        """
        return await backend.call("events", minutes=minutes, limit=limit)

    return mcp


def run_mcp(cfg: Config) -> None:
    build_server(cfg).run("stdio")
