"""Small sources: OSC listener, text file watcher, HTTP push, and a simulator."""

from __future__ import annotations

import asyncio
import random
import re

from .base import Source, SourceConfigError, SourceError


class OscSource(Source):
    """Listens for OSC messages like the ones HR apps send to VRChat.

    Point your HR app's OSC output at this port (default 9100). Don't use 9000
    (VRChat's input) or 9001 (VRChat's output, often used by other tools).
    """

    name = "osc"

    async def run_once(self) -> None:
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import AsyncIOOSCUDPServer
        except ImportError as e:
            raise SourceConfigError("the 'osc' source needs python-osc: pip install python-osc") from e
        osc = self.cfg.osc
        dispatcher = Dispatcher()
        for address in osc.addresses:
            dispatcher.map(address, self.on_message)
        server = AsyncIOOSCUDPServer((osc.host, osc.port), dispatcher, asyncio.get_running_loop())
        try:
            transport, _ = await server.create_serve_endpoint()
        except OSError as e:
            raise SourceError(f"can't listen on UDP {osc.host}:{osc.port} ({e}); is another program using it?") from e
        try:
            self.set_status("listening", f"UDP {osc.host}:{osc.port}")
            await asyncio.Event().wait()
        finally:
            transport.close()

    def on_message(self, _address: str, *args) -> None:
        if not args or isinstance(args[0], bool):
            return
        value = args[0]
        # Ints are bpm. Floats are bpm only if they look like it; many apps also
        # send a 0..1 or -1..1 "percent" float that isn't a heart rate.
        if isinstance(value, int) or (isinstance(value, float) and value >= 20):
            self.push(value)


class FileSource(Source):
    """Reads the first number in a text file whenever the file changes."""

    name = "file"

    async def run_once(self) -> None:
        if not self.cfg.file.path.strip():
            raise SourceConfigError("the 'file' source needs file.path")
        path = self.cfg.resolve(self.cfg.file.path)
        last_mtime = None
        while True:
            try:
                mtime = path.stat().st_mtime_ns
            except FileNotFoundError:
                self.set_status("waiting", f"{path} doesn't exist yet")
                mtime = None
            if mtime is not None and mtime != last_mtime:
                last_mtime = mtime
                self.set_status("watching", str(path))
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")[:200]
                except OSError:
                    text = ""
                if m := re.search(r"\d+(?:\.\d+)?", text):
                    self.push(float(m.group()))
            await asyncio.sleep(self.cfg.file.poll_seconds)


class HttpPushSource(Source):
    """Readings arrive at the bridge's /api/push endpoint (see api.py)."""

    name = "http"

    async def run_once(self) -> None:
        api = self.cfg.api
        self.set_status("listening", f"POST http://{api.host}:{api.port}/api/push")
        await asyncio.Event().wait()


class SimulateSource(Source):
    """Fake heart rate for trying the bridge without a sensor."""

    name = "simulate"

    async def run_once(self) -> None:
        self.set_status("running", "fake data for testing")
        base = float(self.cfg.simulate.baseline)
        bpm, target = base, base
        while True:
            await asyncio.sleep(1)
            r = random.random()
            if r < 0.01:
                target = base + random.choice([25, 40, 55])  # sudden exertion / fright
            elif r < 0.03:
                target = base + random.uniform(-6, 10)
            elif target > base + 15 and r < 0.06:
                target = base
            bpm += (target - bpm) * 0.08 + random.gauss(0, 0.8)
            self.push(bpm)
