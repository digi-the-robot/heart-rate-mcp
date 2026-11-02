"""Base class and reconnect loop shared by every heart rate source."""

from __future__ import annotations

import asyncio
import logging
import random
import time

from ..config import Config
from ..state import HeartRateState

log = logging.getLogger("hrbridge.sources")


class SourceError(Exception):
    """Something went wrong; retry with the normal backoff."""


class SourceConfigError(SourceError):
    """The source can't work until the user changes something (token, setting).
    Retried only every `retry_after` seconds, so a fixed token gets picked up."""

    def __init__(self, message: str, retry_after: float = 300.0):
        super().__init__(message)
        self.retry_after = retry_after


class Source:
    name = "base"

    def __init__(self, cfg: Config, state: HeartRateState):
        self.cfg = cfg
        self.state = state
        self.info = state.info(self.name)

    def set_status(self, status: str, detail: str = "") -> None:
        if (status, detail) != (self.info.status, self.info.detail):
            log.info("%s: %s%s", self.name, status, f" ({detail})" if detail else "")
        self.info.status, self.info.detail = status, detail

    def push(self, bpm: float) -> bool:
        return self.state.push(self.name, bpm)

    async def run_once(self) -> None:
        """Connect and stream until the connection ends. Raise on failure."""
        raise NotImplementedError


async def supervise(source: Source) -> None:
    """Run a source forever, reconnecting with exponential backoff."""
    failures = 0
    while True:
        started = time.monotonic()
        try:
            await source.run_once()
            source.set_status("reconnecting", "connection closed")
            delay = None
        except asyncio.CancelledError:
            source.set_status("stopped")
            raise
        except SourceConfigError as e:
            source.set_status("error", str(e))
            log.warning("%s: %s", source.name, e)
            delay = e.retry_after
        except Exception as e:  # noqa: BLE001 - any failure means "try again later"
            detail = str(e) or type(e).__name__
            source.set_status("error", detail)
            log.warning("%s: %s", source.name, detail, exc_info=not isinstance(e, (SourceError, OSError)))
            delay = None
        if time.monotonic() - started > 60:
            failures = 0  # it ran fine for a while; start the backoff over
        failures += 1
        if delay is None:
            delay = min(60.0, 2.0 ** min(failures, 6)) * random.uniform(0.8, 1.2)
        await asyncio.sleep(delay)
