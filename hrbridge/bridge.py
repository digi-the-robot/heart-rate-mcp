"""The bridge: sources -> state -> events -> sinks, plus the local HTTP API."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

from . import __version__
from .config import Config, build_id
from .events import EventDetector
from .sinks import DiscordPoster, Poster, WebhookPoster
from .sources import make_source, supervise
from .state import HeartRateState

log = logging.getLogger("hrbridge")

_BPM_KEYS = ("bpm", "hr", "heartrate", "heart_rate", "heartRate", "rate", "value")


class Bridge:
    def __init__(self, cfg: Config, *, serve_api: bool = True, run_sinks: bool = True):
        self.cfg = cfg
        self.serve_api = serve_api
        self.state = HeartRateState(cfg.sources, cfg.events.stale_after_seconds, cfg.history_hours)
        self.detector = EventDetector(cfg.events, self.state)
        self.sources = [make_source(name, cfg, self.state) for name in cfg.sources]
        self.sinks: list[Poster] = []
        if run_sinks:
            self.sinks = [DiscordPoster(c) for c in cfg.discord] + [WebhookPoster(c) for c in cfg.webhooks]
            for sink in self.sinks:
                self.detector.subscribe(sink.handle)
        self.build = build_id(cfg)
        self.started = time.time()
        self.stopped = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        if self.serve_api:
            # Bind first: the port doubles as the single-instance lock.
            self._runner = web.AppRunner(build_app(self), access_log=None)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.cfg.api.host, self.cfg.api.port)
            try:
                await site.start()
            except OSError:
                await self._runner.cleanup()
                self._runner = None
                raise
            if "http" in self.cfg.sources and not _is_loopback(self.cfg.api.host) and not self.cfg.api.push_token:
                log.warning("api.host is %s with no api.push_token: anyone on your network can send fake readings",
                            self.cfg.api.host)
        elif "http" in self.cfg.sources:
            log.warning("the 'http' source needs the bridge daemon (hrbridge run); it won't receive anything here")
        for sink in self.sinks:
            await sink.start()
        for source in self.sources:
            self._tasks.append(asyncio.create_task(supervise(source), name=f"source:{source.name}"))
        self._tasks.append(asyncio.create_task(self._ticker(), name="ticker"))
        log.info(
            "bridge %s started: sources=%s, sinks=%s%s",
            __version__,
            ",".join(self.cfg.sources),
            ", ".join(s.label for s in self.sinks) or "none",
            f", api=http://{self.cfg.api.host}:{self.cfg.api.port}" if self.serve_api else "",
        )

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            self.detector.tick()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for sink in self.sinks:
            await sink.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self.stopped.set()

    # ------------------------------------------------------------- queries

    def heart_rate(self) -> dict:
        return self.state.snapshot()

    def history(self, minutes: float = 10, points: int = 30) -> dict:
        return self.state.history(minutes, points)

    def events(self, minutes: float = 60, limit: int = 20) -> dict:
        events = self.detector.recent_events(minutes, limit)
        return {"minutes": minutes, "count": len(events), "events": events}

    def status(self) -> dict:
        return {
            "app": "hrbridge",
            "version": __version__,
            "build": self.build,
            "pid": os.getpid(),
            "config": str(self.cfg.path) if self.cfg.path else None,
            "uptime_seconds": round(time.time() - self.started),
            "heart_rate": self.state.snapshot()["summary"],
            "sources": {n: self.state.describe_source(i, time.time()) for n, i in self.state.sources.items()},
            "sinks": [s.status() for s in self.sinks],
        }


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_bpm(query: dict, body: bytes, content_type: str) -> float | None:
    """Find a heart rate in whatever a phone/watch app sent: query string,
    JSON object, JSON number, form fields, or a bare number."""
    for key in _BPM_KEYS:
        if key in query:
            return _num(query[key])
    text = body.decode("utf-8", errors="ignore").strip()
    if not text:
        return None
    if "json" in content_type or text[:1] in "{[" or text.replace(".", "", 1).isdigit():
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            return float(data)
        if isinstance(data, list) and data and isinstance(data[-1], dict):
            data = data[-1]
        if isinstance(data, dict):
            lowered = {str(k).lower(): v for k, v in data.items()}
            for key in _BPM_KEYS:
                if key.lower() in lowered:
                    return _num(lowered[key.lower()])
            return None
    form = {}
    for part in text.split("&"):
        k, _, v = part.partition("=")
        form[k.strip().lower()] = v.strip()
    for key in _BPM_KEYS:
        if key.lower() in form:
            return _num(form[key.lower()])
    return None


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_app(bridge: Bridge) -> web.Application:
    app = web.Application()
    cfg = bridge.cfg

    def reply(data: dict, status: int = 200) -> web.Response:
        resp = web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    def number(request: web.Request, key: str, default: float) -> float:
        try:
            return float(request.query.get(key, default))
        except ValueError:
            return default

    async def heart_rate(_request):
        return reply(bridge.heart_rate())

    async def history(request):
        return reply(bridge.history(number(request, "minutes", 10), int(number(request, "points", 30))))

    async def events(request):
        return reply(bridge.events(number(request, "minutes", 60), int(number(request, "limit", 20))))

    async def status(_request):
        return reply(bridge.status())

    async def push(request: web.Request):
        if "http" not in cfg.sources:
            return reply({"ok": False, "error": "add \"http\" to sources in config.toml to accept pushes"}, 404)
        body = await request.read() if request.can_read_body else b""
        query = {k.lower(): v for k, v in request.query.items()}
        if cfg.api.push_token:
            given = query.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not given and body:
                try:
                    data = json.loads(body)
                    given = data.get("token", "") if isinstance(data, dict) else ""
                except ValueError:
                    pass
            if given != cfg.api.push_token:
                return reply({"ok": False, "error": "wrong or missing token"}, 401)
        bpm = parse_bpm(query, body, request.content_type or "")
        if bpm is None:
            return reply({"ok": False, "error": "no heart rate found; send e.g. {\"bpm\": 72} or ?bpm=72"}, 400)
        accepted = bridge.state.push("http", bpm)
        return reply({"ok": True, "accepted": accepted, "bpm": round(bpm)})

    async def shutdown(request: web.Request):
        # A custom header can't be sent cross-site without a CORS preflight we
        # never approve, so a random web page can't stop the bridge.
        if request.headers.get("X-HRBridge") != "1" or not _is_loopback(request.remote or ""):
            return reply({"ok": False, "error": "forbidden"}, 403)
        asyncio.get_running_loop().call_later(0.2, lambda: asyncio.ensure_future(bridge.stop()))
        return reply({"ok": True, "stopping": os.getpid()})

    async def index(_request):
        return web.FileResponse(Path(__file__).with_name("overlay.html"))

    app.router.add_get("/", index)
    app.router.add_get("/api/heart_rate", heart_rate)
    app.router.add_get("/api/history", history)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/status", status)
    app.router.add_route("*", "/api/push", push)
    app.router.add_post("/api/shutdown", shutdown)
    return app
