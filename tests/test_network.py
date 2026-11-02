"""Sources and sinks against local fake servers (no real Pulsoid/HypeRate/Discord)."""

from __future__ import annotations

import asyncio
import json
import socket

import aiohttp
from aiohttp import web

from hrbridge.bridge import Bridge
from hrbridge.config import Config, DiscordSinkConfig, EventsConfig
from hrbridge.events import HREvent
from hrbridge.sinks import DiscordPoster
from hrbridge.sources import supervise
from hrbridge.sources.hyperate import HypeRateSource
from hrbridge.sources.pulsoid import PulsoidSource
from hrbridge.state import HeartRateState


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


async def wait_for(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def test_pulsoid_stream_and_battery_fallback():
    async def go():
        seen = {"types": [], "auth": []}

        async def ws_handler(request):
            seen["auth"].append(request.headers.get("Authorization"))
            seen["types"].append(request.query.get("types"))
            if "battery_level" in request.query.get("types", ""):
                return web.Response(status=400, text='{"error_code": 6003}')
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str(json.dumps({"type": "heart_rate", "timestamp": "x", "heartRate": {"bpm": 77}}))
            await ws.send_str(json.dumps({"type": "battery_level", "timestamp": "x", "batteryLevel": {"percent": 55}}))
            await asyncio.sleep(5)
            return ws

        app = web.Application()
        app.router.add_get("/rt", ws_handler)
        runner, port = await serve(app)
        cfg = Config(sources=["pulsoid"])
        cfg.pulsoid.token = "secret"
        state = HeartRateState(cfg.sources)
        src = PulsoidSource(cfg, state)
        src.url = f"http://127.0.0.1:{port}/rt"
        task = asyncio.create_task(supervise(src))
        try:
            await wait_for(lambda: state.latest() is not None and src.info.battery == 55, timeout=8)
            assert src.info.status == "receiving"
        finally:
            task.cancel()
            await runner.cleanup()
        assert state.latest()[1] == 77
        assert seen["auth"][0] == "Bearer secret"
        assert seen["types"][:2] == ["heart_rate,battery_level", "heart_rate"]

    asyncio.run(go())


def test_pulsoid_bad_token_reports_clearly():
    async def go():
        async def reject(_request):
            return web.Response(status=401)

        app = web.Application()
        app.router.add_get("/rt", reject)
        runner, port = await serve(app)
        cfg = Config()
        cfg.pulsoid.token = "nope"
        src = PulsoidSource(cfg, HeartRateState(cfg.sources))
        src.url = f"http://127.0.0.1:{port}/rt"
        task = asyncio.create_task(supervise(src))
        try:
            await wait_for(lambda: src.info.status == "error")
        finally:
            task.cancel()
            await runner.cleanup()
        assert "rejected the token" in src.info.detail

    asyncio.run(go())


def test_hyperate_phoenix_join_and_updates():
    async def go():
        async def ws_handler(request):
            assert request.query["token"] == "key"
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            join = json.loads((await ws.receive()).data)
            assert join["event"] == "phx_join" and join["topic"] == "hr:abc"
            await ws.send_json({"event": "phx_reply", "topic": "hr:abc", "payload": {"status": "ok"}, "ref": 0})
            await ws.send_json({"event": "hr_update", "topic": "hr:abc", "payload": {"hr": 88}, "ref": None})
            await asyncio.sleep(5)
            return ws

        app = web.Application()
        app.router.add_get("/socket", ws_handler)
        runner, port = await serve(app)
        cfg = Config(sources=["hyperate"])
        cfg.hyperate.api_key, cfg.hyperate.session_id = "key", "abc"
        state = HeartRateState(cfg.sources)
        src = HypeRateSource(cfg, state)
        src.url = f"http://127.0.0.1:{port}/socket"
        task = asyncio.create_task(supervise(src))
        try:
            await wait_for(lambda: state.latest() is not None)
        finally:
            task.cancel()
            await runner.cleanup()
        assert state.latest()[1] == 88

    asyncio.run(go())


def test_discord_poster_wake_mentions_and_cooldown():
    async def go():
        posts = []

        async def hook(request):
            posts.append(await request.json())
            return web.Response(status=204)

        app = web.Application()
        app.router.add_post("/api/webhooks/1/tok", hook)
        runner, port = await serve(app)
        cfg = DiscordSinkConfig(
            url=f"http://127.0.0.1:{port}/api/webhooks/1/tok", cooldown_seconds=60,
            wake_events=["spike"], wake_prefix="[EMBODIMENT]", mention="<@42>", username="HR",
            templates={"change": "HR {bpm}"},
        )
        poster = DiscordPoster(cfg)
        poster._GAP = 0
        await poster.start()
        try:
            poster.handle([HREvent("change", 0, 90, "pulsoid", {"from_bpm": 80, "delta": 10})])
            poster.handle([HREvent("zone", 1, 101, "pulsoid", {"direction": "up", "threshold": 100, "zone": 1,
                                                               "from_zone": 0})])  # cooldown: dropped
            poster.handle([HREvent("spike", 2, 120, "pulsoid", {"from_bpm": 90, "delta": 30, "seconds": 10})])
            await wait_for(lambda: len(posts) == 2)
            await asyncio.sleep(0.2)
        finally:
            await poster.close()
            await runner.cleanup()
        assert posts[0]["content"] == "HR 90"
        assert posts[0]["allowed_mentions"] == {"parse": []}
        assert posts[0]["username"] == "HR"
        assert posts[1]["content"].startswith("[EMBODIMENT] <@42> ⚡ **HR spike:** 90 → 120")
        assert posts[1]["allowed_mentions"] == {"parse": [], "users": ["42"]}
        assert len(posts) == 2

    asyncio.run(go())


def test_bridge_api_push_and_queries():
    async def go():
        cfg = Config(sources=["http"])
        cfg.api.port = free_port()
        cfg.events = EventsConfig()
        bridge = Bridge(cfg, serve_api=True, run_sinks=False)
        await bridge.start()
        base = f"http://127.0.0.1:{cfg.api.port}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{base}/api/push", json={"bpm": 66}) as r:
                    assert (await r.json())["accepted"] is True
                async with s.get(f"{base}/api/push", params={"hr": "67"}) as r:
                    assert r.status == 200
                async with s.post(f"{base}/api/push", data=b"nothing here") as r:
                    assert r.status == 400
                async with s.get(f"{base}/api/heart_rate") as r:
                    snap = await r.json()
                    assert snap["bpm"] == 67 and snap["status"] == "live"
                    assert r.headers["Access-Control-Allow-Origin"] == "*"
                async with s.get(f"{base}/api/events") as r:
                    assert (await r.json())["events"][0]["type"] == "connected"
                async with s.get(f"{base}/api/status") as r:
                    assert (await r.json())["app"] == "hrbridge"
                async with s.post(f"{base}/api/shutdown") as r:
                    assert r.status == 403  # no X-HRBridge header
                async with s.get(f"{base}/") as r:
                    assert "Heart rate" in await r.text()
                async with s.post(f"{base}/api/shutdown", headers={"X-HRBridge": "1"}) as r:
                    assert r.status == 200
            await asyncio.wait_for(bridge.stopped.wait(), 5)
        finally:
            if not bridge.stopped.is_set():
                await bridge.stop()

    asyncio.run(go())
