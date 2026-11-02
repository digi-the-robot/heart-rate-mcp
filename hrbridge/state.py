"""Heart rate state: recent samples, per-source status, and the numbers Claude sees."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)

MIN_BPM, MAX_BPM = 25, 250

Sample = tuple[float, int, str]  # (unix time, bpm, source name)
Listener = Callable[[float, int, str], None]


def iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def ago(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def stats(samples: list[Sample]) -> dict | None:
    if not samples:
        return None
    bpms = [s[1] for s in samples]
    return {"avg": round(sum(bpms) / len(bpms)), "min": min(bpms), "max": max(bpms), "samples": len(bpms)}


@dataclass
class SourceInfo:
    name: str
    status: str = "starting"
    detail: str = ""
    last_sample: float | None = None
    last_bpm: int | None = None
    battery: int | None = None
    samples: int = 0


class HeartRateState:
    """Keeps a rolling history and decides which source is currently trusted.

    When several sources are configured they act as fallbacks in list order: the
    first one that is delivering data wins, and a lower one only takes over while
    every source above it has gone quiet.
    """

    def __init__(self, sources: list[str], stale_after: float = 15.0, history_hours: float = 6.0):
        self.priority = {name: i for i, name in enumerate(sources)}
        self.sources = {name: SourceInfo(name) for name in sources}
        self.stale_after = stale_after
        # At most one sample per 0.5 s is kept, so this holds `history_hours`.
        self.samples: deque[Sample] = deque(maxlen=max(1200, int(history_hours * 3600 * 2)))
        self.active: str | None = None
        self._listeners: list[Listener] = []

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def info(self, source: str) -> SourceInfo:
        if source not in self.sources:
            self.sources[source] = SourceInfo(source)
            self.priority.setdefault(source, len(self.priority))
        return self.sources[source]

    # ------------------------------------------------------------------ input

    def push(self, source: str, bpm: float, t: float | None = None) -> bool:
        """Record a reading. Returns False if it was rejected (out of range,
        or a lower-priority source while a better one is live)."""
        t = time.time() if t is None else t
        try:
            value = int(round(float(bpm)))
        except (TypeError, ValueError):
            return False
        if not MIN_BPM <= value <= MAX_BPM:
            return False  # 0 = no skin contact for most sensors
        info = self.info(source)
        info.last_sample, info.last_bpm = t, value
        info.samples += 1
        if not self._accepts(source, t):
            return False
        if self.active != source:
            if self.active is not None:
                log.info("Heart rate now coming from %s (was %s)", source, self.active)
            self.active = source
        if self.samples and t - self.samples[-1][0] < 0.5:
            self.samples[-1] = (t, value, source)
        else:
            self.samples.append((t, value, source))
        for fn in list(self._listeners):
            try:
                fn(t, value, source)
            except Exception:
                log.exception("heart rate listener failed")
        return True

    def _accepts(self, source: str, t: float) -> bool:
        if self.active is None or self.active == source:
            return True
        current = self.sources[self.active]
        if current.last_sample is None or t - current.last_sample > self.stale_after:
            return True
        return self.priority.get(source, 99) < self.priority.get(self.active, 99)

    # ----------------------------------------------------------------- output

    def latest(self) -> Sample | None:
        return self.samples[-1] if self.samples else None

    def status(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        last = self.latest()
        if last is None:
            return "no_data"
        return "live" if now - last[0] <= self.stale_after else "stale"

    def window(self, seconds: float, now: float | None = None) -> list[Sample]:
        now = time.time() if now is None else now
        cutoff = now - seconds
        out = []
        for s in reversed(self.samples):
            if s[0] < cutoff:
                break
            out.append(s)
        out.reverse()
        return out

    def trend(self, now: float | None = None, seconds: float = 60.0) -> tuple[str, float | None]:
        """Least-squares slope over the last minute, in bpm per minute."""
        pts = self.window(seconds, now)
        if len(pts) < 5 or pts[-1][0] - pts[0][0] < seconds / 3:
            return "unknown", None
        t0 = pts[0][0]
        xs = [p[0] - t0 for p in pts]
        ys = [p[1] for p in pts]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        if var == 0:
            return "steady", 0.0
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var * 60
        label = "rising" if slope >= 5 else "falling" if slope <= -5 else "steady"
        return label, round(slope, 1)

    def describe_source(self, info: SourceInfo, now: float) -> str:
        text = info.status + (f": {info.detail}" if info.detail else "")
        if info.last_sample is not None:
            text += f" (last reading {ago(now - info.last_sample)} ago)"
        return text

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        status = self.status(now)
        out: dict = {"status": status, "bpm": None}
        last = self.latest()
        if last is not None:
            t, bpm, src = last
            out.update(bpm=bpm, measured_at=iso(t), age_seconds=round(now - t, 1), source=src)
            if status == "live":
                out["trend"], out["trend_bpm_per_min"] = self.trend(now)
            for label, secs in (("last_1_min", 60), ("last_10_min", 600), ("last_60_min", 3600)):
                if (st := stats(self.window(secs, now))) is not None:
                    out[label] = st
            battery = self.sources[src].battery if src in self.sources else None
            if battery is not None:
                out["sensor_battery_percent"] = battery
        out["sources"] = {name: self.describe_source(info, now) for name, info in self.sources.items()}
        out["summary"] = self._summary(out, now)
        return out

    def _summary(self, snap: dict, now: float) -> str:
        status = snap["status"]
        if status == "no_data":
            parts = [f"{name}: {desc}" for name, desc in snap["sources"].items()]
            return "No heart rate received yet. " + "; ".join(parts) + "."
        bpm, src = snap["bpm"], snap["source"]
        if status == "stale":
            return (
                f"No signal for {ago(snap['age_seconds'])}. Last reading was {bpm} bpm "
                f"at {clock(now - snap['age_seconds'])} via {src} (sensor off, out of range, or the app closed)."
            )
        text = f"{bpm} bpm"
        trend, slope = snap.get("trend"), snap.get("trend_bpm_per_min")
        if trend in ("rising", "falling"):
            text += f", {trend} ({slope:+} bpm/min)"
        elif trend == "steady":
            text += ", steady"
        text += f", live via {src}."
        if (ten := snap.get("last_10_min")) and ten["samples"] >= 10:
            text += f" Last 10 min: avg {ten['avg']}, range {ten['min']}-{ten['max']}."
        return text

    def history(self, minutes: float = 10.0, points: int = 30, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        minutes = max(1.0, min(float(minutes), 24 * 60))
        points = max(2, min(int(points), 240))
        seconds = minutes * 60
        pts = self.window(seconds, now)
        out: dict = {"minutes": minutes, "from": iso(now - seconds), "to": iso(now)}
        if not pts:
            out.update(samples=0, series=[], note="No heart rate data in this period.")
            return out
        out.update(stats(pts))
        bucket = seconds / points
        start = now - seconds
        buckets: dict[int, list[int]] = {}
        for t, bpm, _ in pts:
            buckets.setdefault(min(points - 1, int((t - start) // bucket)), []).append(bpm)
        out["series"] = [
            {"time": clock(start + (i + 0.5) * bucket), "bpm": round(sum(v) / len(v))}
            for i, v in sorted(buckets.items())
        ]
        covered = len(buckets) / points
        if covered < 0.9:
            out["note"] = f"Data covers about {round(covered * 100)}% of the period; gaps are missing buckets."
        return out
