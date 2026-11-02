"""Command line: hrbridge run | mcp | status | stop | check | scan | pulsoid-login | init"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import re
import shutil
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__
from .config import REPO_ROOT, Config, ConfigError, load_config, user_config_dir

log = logging.getLogger("hrbridge")

# Pulsoid OAuth client used by `hrbridge pulsoid-login` (device flow), so people
# can log in through the browser instead of copying a token. Empty until the project registers
# one with Pulsoid; pulsoid.client_id in the config overrides it.
PULSOID_CLIENT_ID = ""
PULSOID_SCOPES = "data:heart_rate:read,data:battery_level:read"


def setup_logging(cfg: Config | None, *, to_file: bool, verbose: bool, quiet_stderr: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    if sys.stderr is not None:  # None under pythonw
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        h.setLevel(logging.WARNING if quiet_stderr and not verbose else logging.DEBUG)
        root.addHandler(h)
    if to_file and cfg is not None and cfg.log_file:
        path = cfg.resolve(cfg.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("aiohttp.access", "bleak", "mcp.server.lowlevel.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------- commands

def cmd_run(cfg: Config, args) -> int:
    from .daemon import run_daemon
    setup_logging(cfg, to_file=True, verbose=args.verbose)
    try:
        return asyncio.run(run_daemon(cfg))
    except KeyboardInterrupt:
        return 0


def cmd_mcp(cfg: Config, args) -> int:
    from .mcp_server import run_mcp
    if args.embedded:
        cfg.mcp.daemon = "embedded"
    setup_logging(cfg, to_file=False, verbose=args.verbose, quiet_stderr=True)
    for w in cfg.warnings:
        log.warning("config: %s", w)
    run_mcp(cfg)
    return 0


def cmd_status(cfg: Config, args) -> int:
    from .daemon import DaemonClient

    async def go() -> int:
        client = DaemonClient(cfg)
        try:
            hello = await client.hello()
            if not hello:
                print(f"No bridge running at {client.base}.")
                return 1
            print(f"Bridge {hello['version']} (pid {hello['pid']}, up {hello['uptime_seconds']}s)")
            print(f"  config: {hello.get('config') or '(none; defaults + env vars)'}")
            print(f"  heart rate: {hello['heart_rate']}")
            for name, desc in hello["sources"].items():
                print(f"  source {name}: {desc}")
            for sink in hello["sinks"]:
                err = f", last error: {sink['last_error']}" if sink.get("last_error") else ""
                print(f"  {sink['type']}: {sink['posted']} posted, {sink['failed']} failed{err}")
            print(f"  live page: {client.base}/")
            return 0
        finally:
            await client.close()

    return asyncio.run(go())


def cmd_stop(cfg: Config, args) -> int:
    from .daemon import DaemonClient

    async def go() -> int:
        client = DaemonClient(cfg)
        try:
            hello = await client.hello()
            if not hello:
                print("No bridge running.")
                return 0
            await client.shutdown()
            await client.wait_gone()
            print(f"Stopped bridge (pid {hello['pid']}).")
            return 0
        finally:
            await client.close()

    return asyncio.run(go())


def cmd_check(cfg: Config, args) -> int:
    """Send a test post to every webhook, then show live readings for a while."""
    from .bridge import Bridge
    from .daemon import DaemonClient
    from .sinks import DiscordPoster, WebhookPoster

    setup_logging(cfg, to_file=False, verbose=args.verbose, quiet_stderr=True)
    print(f"Config: {cfg.path or '(no file; defaults + environment variables)'}")
    for w in cfg.warnings:
        print(f"  warning: {w}")

    async def go() -> int:
        client = DaemonClient(cfg)
        running = await client.hello()
        await client.close()
        if running and "ble" in cfg.sources:
            print(f"Note: the bridge daemon (pid {running['pid']}) is running and may already hold the Bluetooth "
                  "sensor. Run `hrbridge stop` first if the 'ble' check can't connect.")

        posters = [DiscordPoster(c) for c in cfg.discord] + [WebhookPoster(c) for c in cfg.webhooks]
        if not posters:
            print("Webhooks: none configured.")
        for p in posters:
            ok, detail = await p.send_test("💓 Heart rate bridge test message. If you can see this, the webhook works.")
            print(f"Webhook {p.label}: {'OK' if ok else 'FAILED'} ({detail})")

        print(f"\nListening to {', '.join(cfg.sources)} for {args.seconds:.0f}s (Ctrl+C to stop early)...")
        bridge = Bridge(cfg, serve_api=False, run_sinks=False)
        got = []
        bridge.state.subscribe(lambda t, bpm, src: (got.append(bpm), print(f"  {time.strftime('%H:%M:%S')}  {bpm} bpm  ({src})")))
        last_status: dict[str, str] = {}
        await bridge.start()
        try:
            loop = asyncio.get_running_loop()
            end = loop.time() + args.seconds
            while loop.time() < end:
                for name, info in bridge.state.sources.items():
                    s = info.status + (f": {info.detail}" if info.detail else "")
                    if last_status.get(name) != s:
                        last_status[name] = s
                        print(f"  [{name}] {s}")
                await asyncio.sleep(0.25)
        finally:
            await bridge.stop()
        print(f"\n{len(got)} reading(s) used." if got else "\nNo readings received.")
        if len(cfg.sources) > 1:
            for name, info in bridge.state.sources.items():
                print(f"  {name}: {info.samples} received")
            print("  (with several sources, the first one in the list that's live is used; the rest are backups)")
        return 0 if got else 1

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 0


def cmd_scan(cfg: Config, args) -> int:
    from .sources.ble import scan
    from .sources.base import SourceError

    print(f"Scanning for Bluetooth heart rate monitors for {args.seconds:.0f}s (wear the strap / wake the watch)...")
    try:
        found = asyncio.run(scan(args.seconds, show_all=args.all))
    except SourceError as e:
        print(e)
        return 1
    if not found:
        print("Nothing found. Make sure it's on and not connected to a phone app (most straps allow 1-2 connections).")
        return 1
    for d in found:
        tag = "HR " if d["heart_rate"] else "   "
        print(f"  {tag} {d['name']:<28} {d['address']}  rssi {d['rssi']}")
    print('\nPut the name (or address) in config.toml:  [ble]  device = "..."')
    return 0


def cmd_pulsoid_login(cfg: Config, args) -> int:
    import aiohttp

    client_id = args.client_id or cfg.pulsoid.client_id or PULSOID_CLIENT_ID
    if not client_id:
        print(
            "One-click Pulsoid login isn't set up in this copy (no OAuth client ID).\n\n"
            "Get a token manually instead:\n"
            "  1. Open https://pulsoid.net/ui/keys (free accounts can usually do this too)\n"
            "  2. Click 'Create new token' and copy it\n"
            "  3. Put it in config.toml under [pulsoid] as token = \"...\" (or set PULSOID_TOKEN)\n\n"
            "If you have a Pulsoid OAuth client ID, pass it with --client-id or set pulsoid.client_id."
        )
        return 1

    async def go() -> str | None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post("https://pulsoid.net/oauth2/device_authorization",
                              data={"client_id": client_id, "scope": PULSOID_SCOPES}) as r:
                start = await r.json(content_type=None)
                if r.status != 200 or "device_code" not in start:
                    print(f"Pulsoid refused to start the login (HTTP {r.status}): {start}")
                    return None
            url = start.get("verification_uri_complete") or start.get("verification_uri")
            print(f"Open {url}\nand approve access (code: {start.get('user_code')}). Waiting...")
            webbrowser.open(url)
            interval = float(start.get("interval", 3))
            deadline = time.monotonic() + float(start.get("expires_in", 600))
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                async with s.post("https://pulsoid.net/oauth2/token", data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": start["device_code"],
                    "client_id": client_id,
                }) as r:
                    data = await r.json(content_type=None)
                if r.status == 200 and data.get("access_token"):
                    return data["access_token"]
                error = data.get("error")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 2
                    continue
                print(f"Login stopped: {data.get('error_description') or error or data}")
                return None
            print("The login code expired; run the command again.")
            return None

    token = asyncio.run(go())
    if not token:
        return 1
    target = cfg.path or REPO_ROOT / "config.toml"
    if args.print_only:
        print(f"\nToken: {token}")
        return 0
    set_toml_value(target, "pulsoid", "token", token)
    print(f"\nLogged in. Token saved to {target}")
    return 0


def set_toml_value(path: Path, section: str, key: str, value: str) -> None:
    """Set `key = "value"` inside [section], keeping the rest of the file (and
    its comments) as it is."""
    quoted = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    header, replaced = None, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            if header is not None:
                break  # left our section without finding the key
            if stripped == f"[{section}]":
                header = i
            continue
        if header is not None and re.match(rf"^{re.escape(key)}\s*=", stripped):
            lines[i] = f"{key} = {quoted}"
            replaced = True
            break
    if not replaced:
        if header is None:
            lines += ["", f"[{section}]", f"{key} = {quoted}"]
        else:
            lines.insert(header + 1, f"{key} = {quoted}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_init(args) -> int:
    example = REPO_ROOT / "config.example.toml"
    target = Path(args.path) if args.path else (user_config_dir() / "config.toml" if args.user else REPO_ROOT / "config.toml")
    if target.exists():
        print(f"{target} already exists; not touching it.")
        return 1
    if not example.exists():
        print(f"Can't find {example}.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, target)
    print(f"Created {target}. Open it, pick your source, and fill in the token / webhook.")
    return 0


# ------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hrbridge", description="Heart rate bridge for Claude (MCP + webhooks)")
    parser.add_argument("--version", action="version", version=f"hrbridge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help: str):
        p = sub.add_parser(name, help=help)
        p.add_argument("--config", help="path to config.toml")
        p.add_argument("-v", "--verbose", action="store_true")
        return p

    p = add("run", "run the bridge daemon in the foreground")
    p.add_argument("--env-file", help=argparse.SUPPRESS)
    p = add("mcp", "run the MCP server on stdio (what Claude clients launch)")
    p.add_argument("--embedded", action="store_true", help="read the sensor in this process instead of the daemon")
    add("status", "show the running daemon's state")
    add("stop", "stop the running daemon")
    p = add("check", "test webhooks, then print live readings")
    p.add_argument("--seconds", type=float, default=20)
    p = add("scan", "list nearby Bluetooth heart rate monitors")
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--all", action="store_true", help="show every named device, not just HR monitors")
    p = add("pulsoid-login", "get a Pulsoid token through the browser")
    p.add_argument("--client-id")
    p.add_argument("--print-only", action="store_true", help="print the token instead of saving it")
    p = sub.add_parser("init", help="create config.toml from the example")
    p.add_argument("--user", action="store_true", help=f"create it in {user_config_dir()} instead of the repo folder")
    p.add_argument("--path", help="create it at this path")

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # old Windows consoles vs emoji
    if args.command == "init":
        sys.exit(cmd_init(args))
    if args.command == "run" and args.env_file:
        from .daemon import load_env_file
        load_env_file(args.env_file)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        sys.exit(2)
    handlers = {
        "run": cmd_run, "mcp": cmd_mcp, "status": cmd_status, "stop": cmd_stop,
        "check": cmd_check, "scan": cmd_scan, "pulsoid-login": cmd_pulsoid_login,
    }
    sys.exit(handlers[args.command](cfg, args))
