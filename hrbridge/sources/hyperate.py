"""HypeRate WebSocket (Phoenix channels): https://github.com/HypeRate/DevDocs"""

from __future__ import annotations

import asyncio
import json

import aiohttp

from .base import Source, SourceConfigError, SourceError

WS_URL = "wss://app.hyperate.io/socket/websocket"


class HypeRateSource(Source):
    name = "hyperate"
    url = WS_URL

    async def run_once(self) -> None:
        key = self.cfg.hyperate.api_key.strip()
        session_id = self.cfg.hyperate.session_id.strip()
        if not key or not session_id:
            raise SourceConfigError(
                "HypeRate needs hyperate.api_key (request one at hyperate.io/api) and hyperate.session_id "
                "(the ID shown in your HypeRate app)"
            )
        topic = f"hr:{session_id}"
        self.set_status("connecting")
        async with aiohttp.ClientSession() as session:
            try:
                ws = await session.ws_connect(self.url, params={"token": key})
            except aiohttp.WSServerHandshakeError as e:
                if e.status in (401, 403):
                    raise SourceConfigError(f"HypeRate rejected the API key (HTTP {e.status})") from e
                raise SourceError(f"HypeRate handshake failed (HTTP {e.status})") from e
            async with ws:
                await ws.send_json({"topic": topic, "event": "phx_join", "payload": {}, "ref": 0})
                beat = asyncio.create_task(self._heartbeat(ws))
                try:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                                break
                            continue
                        if self.handle(msg.data, topic) == "closed":
                            break
                finally:
                    beat.cancel()

    def handle(self, text: str, topic: str) -> str | None:
        try:
            data = json.loads(text)
        except ValueError:
            return None
        event = data.get("event")
        payload = data.get("payload") or {}
        if event == "hr_update":
            if self.push(payload.get("hr", 0)) and self.info.status != "receiving":
                self.set_status("receiving")
        elif event == "phx_reply" and data.get("topic") == topic:
            if payload.get("status") == "ok":
                self.set_status("connected", f"joined {topic}, waiting for data")
            else:
                self.set_status("error", f"HypeRate refused {topic}: {payload.get('response')}")
                return "closed"
        elif event in ("phx_error", "phx_close") and data.get("topic") == topic:
            return "closed"
        return None

    @staticmethod
    async def _heartbeat(ws: aiohttp.ClientWebSocketResponse) -> None:
        # Phoenix drops the socket without a keep-alive every ~10 s.
        while True:
            await asyncio.sleep(10)
            await ws.send_json({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": 0})
