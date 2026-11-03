"""Turns the raw heart rate stream into events worth telling someone about."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .config import EventsConfig
from .state import HeartRateState, clock, iso, stats

log = logging.getLogger(__name__)

# When one reading produces several events, sinks post only the first of these.
PRIORITY = {"spike": 0, "zone": 1, "change": 2, "connected": 3, "disconnected": 4, "summary": 5}

DEFAULT_TEMPLATES = {
    "connected": "💓 Heart rate connected ({source}): **{bpm} bpm**",
    "disconnected": "💔 Heart rate signal lost. Last reading {bpm} bpm at {last_seen}",
    "change": "💓 **HR:** {bpm} bpm ({delta:+d})",
    "zone_up": "💓 **HR:** {bpm} bpm, now above {threshold}",
    "zone_down": "💓 **HR:** {bpm} bpm, back under {threshold}",
    "spike": "⚡ **HR spike:** {from_bpm} → {bpm} bpm in {seconds}s",
    "summary": "💓 **Last {minutes} min:** avg {avg}, range {min}-{max}, now {bpm} bpm ({trend})",
}
TEMPLATE_KEYS = tuple(DEFAULT_TEMPLATES)


@dataclass
class HREvent:
    type: str
    time: float
    bpm: int | None
    source: str | None
    data: dict = field(default_factory=dict)

    @property
    def template_key(self) -> str:
        return f"zone_{self.data['direction']}" if self.type == "zone" else self.type

    def render(self, templates: dict[str, str] | None = None) -> str:
        key = self.template_key
        values = {"bpm": self.bpm, "source": self.source or "?", "time": clock(self.time), **self.data}
        custom = (templates or {}).get(key)
        if custom:
            try:
                return custom.format(**values)
            except (KeyError, IndexError, ValueError) as e:
                log.warning("template %r failed (%s); using the default", key, e)
        return DEFAULT_TEMPLATES[key].format(**values)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "time": iso(self.time),
            "bpm": self.bpm,
            "source": self.source,
            **self.data,
            "text": self.render(),
        }


EventHandler = Callable[[list[HREvent]], None]


class EventDetector:
    def __init__(self, cfg: EventsConfig, state: HeartRateState):
        self.cfg = cfg
        self.state = state
        self.recent: deque[HREvent] = deque(maxlen=500)
        self._handlers: list[EventHandler] = []
        self._connected = False
        self._baseline: int | None = None
        self._zone = 0
        self._last_spike = float("-inf")
        self._last_summary = time.time()
        state.subscribe(self._on_sample)

    def subscribe(self, fn: EventHandler) -> None:
        self._handlers.append(fn)

    @property
    def zone(self) -> int:
        """Current zone index (0 = below every threshold), hysteresis applied."""
        return self._zone

    def _emit(self, events: list[HREvent]) -> None:
        if not events:
            return
        events.sort(key=lambda e: PRIORITY[e.type])
        self.recent.extend(events)
        for e in events:
            log.info("event %s: %s", e.type, e.render())
        for fn in list(self._handlers):
            try:
                fn(events)
            except Exception:
                log.exception("event handler failed")

    def zone_for(self, bpm: int, current: int | None) -> int:
        """Index of the zone bpm is in (0 = below every threshold), with
        hysteresis on the way down so 99/100/99 doesn't flap."""
        zones = self.cfg.zones
        raw = sum(1 for z in zones if bpm >= z)
        if current is None or raw >= current:
            return raw
        z = current
        while z > 0 and bpm < zones[z - 1] - self.cfg.zone_hysteresis:
            z -= 1
        return z

    def _on_sample(self, t: float, bpm: int, source: str) -> None:
        if not self._connected:
            self._connected = True
            self._baseline = bpm
            self._zone = self.zone_for(bpm, None)
            self._emit([HREvent("connected", t, bpm, source)])
            return

        events: list[HREvent] = []
        cfg = self.cfg
        if cfg.change_threshold > 0 and abs(bpm - self._baseline) >= cfg.change_threshold:
            events.append(HREvent("change", t, bpm, source, {"from_bpm": self._baseline, "delta": bpm - self._baseline}))
            self._baseline = bpm

        zone = self.zone_for(bpm, self._zone)
        if zone != self._zone:
            up = zone > self._zone
            threshold = cfg.zones[zone - 1] if up else cfg.zones[zone]
            events.append(HREvent("zone", t, bpm, source, {
                "direction": "up" if up else "down", "threshold": threshold, "zone": zone, "from_zone": self._zone,
            }))
            self._zone = zone

        if cfg.spike_bpm > 0 and t - self._last_spike >= cfg.spike_window_seconds:
            window = self.state.window(cfg.spike_window_seconds, t)
            if window:
                low_t, low = min(((s[0], s[1]) for s in window), key=lambda s: s[1])
                if bpm - low >= cfg.spike_bpm and low_t < t:
                    events.append(HREvent("spike", t, bpm, source, {
                        "from_bpm": low, "delta": bpm - low, "seconds": max(1, round(t - low_t)),
                    }))
                    self._last_spike = t

        self._emit(events)

    def tick(self, now: float | None = None) -> None:
        """Called about once a second: notices silence and sends summaries."""
        now = time.time() if now is None else now
        events: list[HREvent] = []
        last = self.state.latest()
        if self._connected and last is not None and now - last[0] > self.cfg.disconnect_after_seconds:
            self._connected = False
            events.append(HREvent("disconnected", now, last[1], last[2], {
                "last_seen": clock(last[0]), "silent_seconds": round(now - last[0]),
            }))
        minutes = self.cfg.summary_minutes
        if minutes > 0 and now - self._last_summary >= minutes * 60:
            self._last_summary = now
            window = self.state.window(minutes * 60, now)
            st = stats(window)
            if self.state.status(now) == "live" and st and last is not None:
                # Trend over the whole period: first quarter vs last quarter.
                q = max(1, len(window) // 4)
                diff = stats(window[-q:])["avg"] - stats(window[:q])["avg"]
                trend = "rising" if diff >= 5 else "falling" if diff <= -5 else "steady"
                shown = int(minutes) if float(minutes).is_integer() else minutes
                events.append(HREvent("summary", now, last[1], last[2], {
                    "minutes": shown, "avg": st["avg"], "min": st["min"], "max": st["max"], "trend": trend,
                }))
        self._emit(events)

    def recent_events(self, minutes: float = 60.0, limit: int = 20, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        cutoff = now - max(1.0, float(minutes)) * 60
        picked = [e for e in self.recent if e.time >= cutoff]
        return [e.to_dict() for e in picked[-max(1, int(limit)):]]
