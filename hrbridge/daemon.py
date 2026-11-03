"""Running the bridge as a background daemon, and talking to one that's running.

One daemon owns the sensor connection and the webhooks; every MCP server (one
per AI client / bot) is a thin relay to it. That way five bots can read the
same chest strap, and Discord gets one post per event instead of five.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

from .bridge import Bridge
from .config import ENV_VARS, REPO_ROOT, Config, build_id, daemon_env

log = logging.getLogger("hrbridge")

START_TIMEOUT = 20.0


def api_base(cfg: Config) -> str:
    host = cfg.api.host
    if host in ("0.0.0.0", "", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.api.port}"


class DaemonClient:
    def __init__(self, cfg: Config, timeout: float = 5.0):
        self.cfg = cfg
        self.base = api_base(cfg)
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

    async def close(self) -> None:
        await self._session.close()

    async def get(self, path: str, **params) -> dict:
        async with self._session.get(f"{self.base}/api/{path}", params={k: str(v) for k, v in params.items()}) as r:
            data = await r.json(content_type=None)
            if not isinstance(data, dict):
                raise aiohttp.ClientError("unexpected reply")
            return data

    async def hello(self) -> dict | None:
        """The daemon's /api/status, or None if nothing (or something else) is there."""
        try:
            data = await self.get("status")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None
        return data if data.get("app") == "hrbridge" else None

    async def shutdown(self) -> bool:
        try:
            async with self._session.post(f"{self.base}/api/shutdown", headers={"X-HRBridge": "1"}) as r:
                return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def wait_gone(self, timeout: float = 10.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline and await self.hello():
            await asyncio.sleep(0.3)


def spawn_daemon(cfg: Config) -> None:
    """Start `hrbridge run` as a background process that outlives the caller."""
    exe = Path(sys.executable)
    args = ["-m", "hrbridge", "run"]
    if cfg.path is not None:
        args += ["--config", str(cfg.path)]
    # Hand over env-var settings (e.g. PULSOID_TOKEN from an MCP client config)
    # through a short-lived file: a WMI-launched process doesn't inherit our
    # environment, and tokens don't belong on a command line.
    fd, env_path = tempfile.mkstemp(prefix="hrbridge-env-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({k: os.environ.get(k) for k in ENV_VARS}, f)
    args += ["--env-file", env_path]

    if sys.platform != "win32":
        subprocess.Popen(
            [str(exe), *args], cwd=REPO_ROOT, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return

    if (pythonw := exe.with_name("pythonw.exe")).exists():
        exe = pythonw
    command_line = subprocess.list2cmdline([str(exe), *args])
    # MCP clients built on the Python MCP SDK run servers inside a Windows Job
    # Object that kills every child when the client exits, so a normal child
    # process would die with whichever client/bot started it. A process created
    # through WMI lives outside that job.
    script = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
        "@{CommandLine = $env:HRBRIDGE_CMD; CurrentDirectory = $env:HRBRIDGE_DIR}; exit $r.ReturnValue"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "HRBRIDGE_CMD": command_line, "HRBRIDGE_DIR": str(REPO_ROOT)},
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            log.info("launched bridge daemon via WMI")
            return
        log.warning("WMI launch failed (code %d): %s", result.returncode,
                    result.stderr.decode(errors="replace").strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("WMI launch failed: %s", e)

    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    kwargs = dict(cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        subprocess.Popen([str(exe), *args], creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        subprocess.Popen([str(exe), *args], creationflags=flags, **kwargs)
    log.info("launched bridge daemon as a detached child")


def load_env_file(path: str) -> None:
    """Apply (and delete) the env-var handover written by spawn_daemon."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    finally:
        p.unlink(missing_ok=True)
    for key in ENV_VARS:
        value = data.get(key)
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


async def ensure_daemon(cfg: Config, client: DaemonClient) -> dict | None:
    """Make sure a daemon running this code + config is up; start or restart one
    if needed. Returns its status, or None if it couldn't be started."""
    want = build_id(cfg)
    hello = await client.hello()
    if hello and hello.get("build") == want:
        return hello
    if hello:
        same_config = hello.get("config") == (str(cfg.path) if cfg.path else None)
        if not same_config:
            # Someone runs a bridge with a different config on this port; use it as is.
            log.warning("a bridge with a different config (%s) is already running; using it", hello.get("config"))
            return hello
        log.info("bridge pid %s runs older code/config; restarting it", hello.get("pid"))
        await client.shutdown()
        await client.wait_gone()

    await asyncio.to_thread(spawn_daemon, cfg)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_TIMEOUT
    while loop.time() < deadline:
        hello = await client.hello()
        if hello and hello.get("build") == want:
            log.info("bridge daemon ready (pid %s)", hello.get("pid"))
            return hello
        await asyncio.sleep(0.3)
    return await client.hello()  # an older build is better than nothing


async def run_daemon(cfg: Config) -> int:
    bridge = Bridge(cfg, serve_api=True, run_sinks=True)
    try:
        await bridge.start()
    except OSError as e:
        client = DaemonClient(cfg)
        hello = await client.hello()
        await client.close()
        if hello:
            log.info("a bridge is already running (pid %s) on %s", hello.get("pid"), api_base(cfg))
            return 0
        log.error("can't listen on %s:%s (%s). Change api.port in the config.", cfg.api.host, cfg.api.port, e)
        return 1
    log.info("daemon pid %d, build %s, config %s", os.getpid(), bridge.build, cfg.path or "(none; defaults + env)")
    for w in cfg.warnings:
        log.warning("config: %s", w)

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bridge.stop()))
    try:
        await bridge.stopped.wait()
    finally:
        if not bridge.stopped.is_set():
            await bridge.stop()
    log.info("daemon stopped")
    return 0
