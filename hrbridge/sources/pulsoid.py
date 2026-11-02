"""Pulsoid real-time WebSocket (v2): https://docs.pulsoid.net"""

from __future__ import annotations

import json
import logging

import aiohttp

from .base import Source, SourceConfigError, SourceError

log = logging.getLogger("hrbridge.sources")

WS_URL = "wss://dev.pulsoid.net/api/v2/data/real_time"


class PulsoidSource(Source):
    name = "pulsoid"
    url = WS_URL

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._battery = self.cfg.pulsoid.battery

    async def run_once(self) -> None:
        token = self.cfg.pulsoid.token.strip()
        if not token:
            raise SourceConfigError(
                "no Pulsoid token. Set pulsoid.token in config.toml or PULSOID_TOKEN (see the README)"
            )
        types = "heart_rate,battery_level" if self._battery else "heart_rate"
        self.set_status("connecting")
        async with aiohttp.ClientSession() as session:
            try:
                ws = await session.ws_connect(
                    self.url,
                    params={"types": types},
                    headers={"Authorization": f"Bearer {token}"},
                    heartbeat=30,
                )
            except aiohttp.WSServerHandshakeError as e:
                if e.status == 400 and self._battery:
                    # Token lacks data:battery_level:read; heart rate alone still works.
                    log.info("pulsoid: token can't read battery level; continuing with heart rate only")
                    self._battery = False
                    return
                if e.status in (401, 403):
                    raise SourceConfigError(
                        f"Pulsoid rejected the token (HTTP {e.status}). It may be mistyped, revoked, or expired"
                    ) from e
                raise SourceError(f"Pulsoid handshake failed (HTTP {e.status})") from e
            async with ws:
                self.set_status("connected", "waiting for data (is the Pulsoid app measuring?)")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self.handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                        break

    def handle(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            if text.strip().isdigit():  # response_mode=text_plain_only_heart_rate
                self._got(int(text))
            return
        if not isinstance(data, dict):
            return
        # v2: a message may carry several payloads, so look at each one.
        if isinstance(hr := data.get("heartRate"), dict) and "bpm" in hr:
            self._got(hr["bpm"])
        if isinstance(bat := data.get("batteryLevel"), dict) and "percent" in bat:
            self.info.battery = bat["percent"]
        if isinstance(pause := data.get("ingestionPause"), dict):
            self.set_status("connected", f"Pulsoid paused data until {pause.get('ends_at', '?')}")
        # v1 shape, in case someone points this at the legacy endpoint.
        if isinstance(v1 := data.get("data"), dict) and "heart_rate" in v1:
            self._got(v1["heart_rate"])

    def _got(self, bpm) -> None:
        if self.push(bpm) and self.info.status != "receiving":
            self.set_status("receiving")
