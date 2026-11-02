from __future__ import annotations

from ..config import Config
from ..state import HeartRateState
from .base import Source, SourceConfigError, SourceError, supervise
from .ble import BleSource
from .hyperate import HypeRateSource
from .pulsoid import PulsoidSource
from .simple import FileSource, HttpPushSource, OscSource, SimulateSource

REGISTRY: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (PulsoidSource, HypeRateSource, BleSource, OscSource, HttpPushSource, FileSource, SimulateSource)
}


def make_source(name: str, cfg: Config, state: HeartRateState) -> Source:
    return REGISTRY[name](cfg, state)


__all__ = ["REGISTRY", "Source", "SourceConfigError", "SourceError", "make_source", "supervise"]
