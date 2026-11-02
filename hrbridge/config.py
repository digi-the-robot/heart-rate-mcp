"""Configuration: a TOML file plus environment-variable overrides.

Lookup order for the file: --config, then $HRBRIDGE_CONFIG, then config.toml in
the repo folder, then the per-user config folder. With no file at all the bridge
still runs on defaults + environment variables, which is enough for an
MCP-only setup (e.g. PULSOID_TOKEN set in the MCP client's config).
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10
    import tomli as tomllib

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent

SOURCE_NAMES = ("pulsoid", "hyperate", "ble", "osc", "http", "file", "simulate")
EVENT_TYPES = ("connected", "disconnected", "change", "zone", "spike", "summary")
DAEMON_MODES = ("auto", "connect", "embedded")

# Environment variables the bridge understands. A daemon started by the MCP
# server gets these handed over (see daemon_env), so env-only setups work too.
ENV_VARS = (
    "HRBRIDGE_SOURCES",
    "HRBRIDGE_DISCORD_WEBHOOK",
    "HRBRIDGE_PORT",
    "HRBRIDGE_MCP_DAEMON",
    "PULSOID_TOKEN",
    "HYPERATE_API_KEY",
    "HYPERATE_ID",
    "HRBRIDGE_BLE_DEVICE",
)


class ConfigError(Exception):
    pass


@dataclass
class PulsoidConfig:
    token: str = ""
    battery: bool = True
    client_id: str = ""


@dataclass
class HypeRateConfig:
    api_key: str = ""
    session_id: str = ""


@dataclass
class BleConfig:
    device: str = ""
    scan_seconds: float = 10.0


@dataclass
class OscConfig:
    host: str = "127.0.0.1"
    port: int = 9100
    addresses: list[str] = field(
        default_factory=lambda: [
            "/avatar/parameters/HR",
            "/avatar/parameters/HeartRate",
            "/avatar/parameters/Heartrate",
            "/avatar/parameters/HeartRateInt",
            "/avatar/parameters/heart_rate",
            "/hr",
            "/heartrate",
        ]
    )


@dataclass
class FileConfig:
    path: str = ""
    poll_seconds: float = 1.0


@dataclass
class SimulateConfig:
    baseline: int = 72


@dataclass
class EventsConfig:
    stale_after_seconds: float = 15.0
    disconnect_after_seconds: float = 60.0
    change_threshold: int = 10
    zones: list[int] = field(default_factory=lambda: [100, 120, 140])
    zone_hysteresis: int = 3
    spike_bpm: int = 20
    spike_window_seconds: float = 30.0
    summary_minutes: float = 0.0


@dataclass
class DiscordSinkConfig:
    url: str = ""
    events: list[str] = field(default_factory=lambda: list(EVENT_TYPES))
    cooldown_seconds: float = 60.0
    username: str = ""
    avatar_url: str = ""
    prefix: str = ""
    wake_events: list[str] = field(default_factory=list)
    wake_prefix: str = ""
    mention: str = ""
    templates: dict[str, str] = field(default_factory=dict)


@dataclass
class WebhookSinkConfig:
    url: str = ""
    events: list[str] = field(default_factory=lambda: list(EVENT_TYPES))
    cooldown_seconds: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 47820
    push_token: str = ""


@dataclass
class McpConfig:
    daemon: str = "auto"


@dataclass
class Config:
    sources: list[str] = field(default_factory=lambda: ["pulsoid"])
    history_hours: float = 6.0
    log_file: str = "hrbridge.log"
    pulsoid: PulsoidConfig = field(default_factory=PulsoidConfig)
    hyperate: HypeRateConfig = field(default_factory=HypeRateConfig)
    ble: BleConfig = field(default_factory=BleConfig)
    osc: OscConfig = field(default_factory=OscConfig)
    file: FileConfig = field(default_factory=FileConfig)
    simulate: SimulateConfig = field(default_factory=SimulateConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    discord: list[DiscordSinkConfig] = field(default_factory=list)
    webhooks: list[WebhookSinkConfig] = field(default_factory=list)

    # Not settable from the file.
    path: Path | None = field(default=None, repr=False)
    warnings: list[str] = field(default_factory=list, repr=False)

    @property
    def base_dir(self) -> Path:
        """Folder that relative paths in the config are resolved against."""
        if self.path is not None:
            return self.path.parent
        return user_config_dir()

    def resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.base_dir / p


_NOT_FROM_FILE = {"path", "warnings"}


def user_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "hrbridge"


def find_config(explicit: str | None = None) -> Path | None:
    for candidate, label in ((explicit, "--config"), (os.environ.get("HRBRIDGE_CONFIG"), "HRBRIDGE_CONFIG")):
        if candidate:
            p = Path(candidate).expanduser()
            if not p.is_file():
                raise ConfigError(f"{label} points at {p}, which doesn't exist")
            return p.resolve()
    for p in (REPO_ROOT / "config.toml", user_config_dir() / "config.toml"):
        if p.is_file():
            return p.resolve()
    return None


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: str, where: str, warnings: list[str]) -> str:
    def sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        warnings.append(f"{where} uses ${{{name}}}, but that environment variable isn't set")
        return ""

    return _ENV_REF.sub(sub, value)


def _coerce(tp: Any, value: Any, where: str, warnings: list[str]) -> Any:
    origin = get_origin(tp)
    if is_dataclass(tp):
        if not isinstance(value, dict):
            raise ConfigError(f"{where} should be a table, like [{where}]")
        return _build(tp, value, where, warnings)
    if origin is list:
        (item_tp,) = get_args(tp)
        if isinstance(value, dict) and is_dataclass(item_tp):
            value = [value]  # allow [discord] as well as [[discord]]
        if not isinstance(value, list):
            raise ConfigError(f"{where} should be a list, like [ ... ]")
        return [_coerce(item_tp, v, f"{where}[{i}]", warnings) for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{where} should be a table")
        return {str(k): _expand_env(str(v), f"{where}.{k}", warnings) for k, v in value.items()}
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where} should be true or false")
        return value
    if tp in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} should be a number")
        return tp(value)
    if tp is str:
        if isinstance(value, (dict, list)):
            raise ConfigError(f"{where} should be text")
        return _expand_env(str(value), where, warnings)
    return value


def _build(cls: type, data: dict, where: str, warnings: list[str]):
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    if cls is Config:
        known -= _NOT_FROM_FILE
    kwargs = {}
    for key, value in data.items():
        if key not in known:
            warnings.append(f"unknown setting '{where + '.' if where else ''}{key}' (ignored; typo?)")
            continue
        kwargs[key] = _coerce(hints[key], value, f"{where + '.' if where else ''}{key}", warnings)
    return cls(**kwargs)


def _apply_env(cfg: Config) -> None:
    env = os.environ
    if v := env.get("HRBRIDGE_SOURCES", "").strip():
        cfg.sources = [s.strip().lower() for s in v.split(",") if s.strip()]
    if v := env.get("PULSOID_TOKEN", "").strip():
        cfg.pulsoid.token = v
    if v := env.get("HYPERATE_API_KEY", "").strip():
        cfg.hyperate.api_key = v
    if v := env.get("HYPERATE_ID", "").strip():
        cfg.hyperate.session_id = v
    if v := env.get("HRBRIDGE_BLE_DEVICE", "").strip():
        cfg.ble.device = v
    if v := env.get("HRBRIDGE_PORT", "").strip():
        try:
            cfg.api.port = int(v)
        except ValueError:
            raise ConfigError(f"HRBRIDGE_PORT should be a number, got {v!r}")
    if v := env.get("HRBRIDGE_MCP_DAEMON", "").strip():
        cfg.mcp.daemon = v.lower()
    if (v := env.get("HRBRIDGE_DISCORD_WEBHOOK", "").strip()) and not any(d.url == v for d in cfg.discord):
        cfg.discord.append(DiscordSinkConfig(url=v))


def _validate(cfg: Config) -> None:
    if not cfg.sources:
        raise ConfigError("sources is empty; list at least one, e.g. sources = [\"pulsoid\"]")
    for name in cfg.sources:
        if name not in SOURCE_NAMES:
            raise ConfigError(f"unknown source '{name}'. Available: {', '.join(SOURCE_NAMES)}")
    if len(set(cfg.sources)) != len(cfg.sources):
        raise ConfigError("sources lists the same source twice")
    if cfg.mcp.daemon not in DAEMON_MODES:
        raise ConfigError(f"mcp.daemon should be one of {', '.join(DAEMON_MODES)}")
    cfg.events.zones = sorted(set(cfg.events.zones))
    for kind, sinks in (("discord", cfg.discord), ("webhooks", cfg.webhooks)):
        kept = []
        for i, sink in enumerate(sinks):
            if not sink.url.strip():
                cfg.warnings.append(f"{kind}[{i}] has no url; skipping it")
                continue
            bad = [e for e in sink.events if e not in EVENT_TYPES]
            if bad:
                raise ConfigError(f"{kind}[{i}].events has unknown event(s) {bad}. Available: {', '.join(EVENT_TYPES)}")
            if isinstance(sink, DiscordSinkConfig):
                bad = [e for e in sink.wake_events if e not in EVENT_TYPES]
                if bad:
                    raise ConfigError(f"discord[{i}].wake_events has unknown event(s) {bad}")
                if not re.match(r"https://(\w+\.)?discord(app)?\.com/api/webhooks/", sink.url):
                    cfg.warnings.append(f"discord[{i}].url doesn't look like a Discord webhook URL")
            kept.append(sink)
        sinks[:] = kept


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    warnings: list[str] = []
    if path is None:
        cfg = Config()
    else:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path} isn't valid TOML: {e}") from e
        cfg = _build(Config, data, "", warnings)
        cfg.path = path
    cfg.warnings = warnings
    _apply_env(cfg)
    _validate(cfg)
    return cfg


def daemon_env() -> dict[str, str]:
    """The bridge's own environment variables that are currently set."""
    return {k: os.environ[k] for k in ENV_VARS if os.environ.get(k)}


def build_id(cfg: Config) -> str:
    """Changes whenever the code, the config file, or the bridge's env vars change.

    The MCP server compares this with a running daemon's and restarts the daemon
    when they differ, so editing config.toml takes effect on the next client launch.
    """
    h = hashlib.sha256()
    for p in sorted(PACKAGE_DIR.rglob("*.py")):
        h.update(p.relative_to(PACKAGE_DIR).as_posix().encode())
        h.update(p.read_bytes())
    if cfg.path is not None and cfg.path.is_file():
        h.update(cfg.path.read_bytes())
    for k, v in sorted(daemon_env().items()):
        h.update(f"{k}={v}".encode())
    return h.hexdigest()[:16]
