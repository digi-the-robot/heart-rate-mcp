from __future__ import annotations

import pytest

from hrbridge.bridge import parse_bpm
from hrbridge.cli import set_toml_value
from hrbridge.config import REPO_ROOT, ConfigError, EventsConfig, load_config
from hrbridge.events import EventDetector
from hrbridge.sources.ble import parse_measurement
from hrbridge.state import HeartRateState

ENV = ("HRBRIDGE_CONFIG", "PULSOID_TOKEN", "HRBRIDGE_SOURCES", "HRBRIDGE_DISCORD_WEBHOOK", "HYPERATE_API_KEY",
       "HYPERATE_ID", "HRBRIDGE_PORT", "HRBRIDGE_MCP_DAEMON", "HRBRIDGE_BLE_DEVICE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- config

def test_example_config_loads_cleanly():
    cfg = load_config(str(REPO_ROOT / "config.example.toml"))
    assert cfg.sources == ["pulsoid"]
    assert cfg.warnings == []
    assert cfg.discord == []


def test_env_overrides_and_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_HOOK", "https://discord.com/api/webhooks/1/abc")
    monkeypatch.setenv("PULSOID_TOKEN", "tok")
    monkeypatch.setenv("HRBRIDGE_SOURCES", "ble, pulsoid")
    cfg = load_config(write(tmp_path, '[discord]\nurl = "${MY_HOOK}"\nevents = ["spike"]\n'))
    assert cfg.pulsoid.token == "tok"
    assert cfg.sources == ["ble", "pulsoid"]
    assert len(cfg.discord) == 1 and cfg.discord[0].url.endswith("/1/abc")
    assert cfg.discord[0].events == ["spike"]


def test_unknown_key_warns_and_bad_values_fail(tmp_path):
    cfg = load_config(write(tmp_path, 'sorces = ["ble"]\n[events]\nzones = [140, 100]\n'))
    assert any("sorces" in w for w in cfg.warnings)
    assert cfg.events.zones == [100, 140]
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, 'sources = ["fitbit"]\n'))
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, '[events]\nchange_threshold = "ten"\n'))
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, '[[discord]]\nurl = "https://discord.com/api/webhooks/1/a"\nevents = ["beep"]\n'))


def test_set_toml_value_keeps_comments(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('# hi\n[pulsoid]\n# comment\ntoken = ""\nbattery = true\n\n[ble]\ndevice = ""\n', encoding="utf-8")
    set_toml_value(p, "pulsoid", "token", "abc")
    text = p.read_text(encoding="utf-8")
    assert 'token = "abc"' in text and "# comment" in text and 'device = ""' in text
    p.write_text('[ble]\ndevice = ""\n', encoding="utf-8")
    set_toml_value(p, "pulsoid", "token", "xyz")
    assert load_config(str(p)).pulsoid.token == "xyz"
    p.write_text('[pulsoid]\nbattery = true\n[ble]\ndevice = ""\n', encoding="utf-8")
    set_toml_value(p, "pulsoid", "token", "q")
    assert load_config(str(p)).pulsoid.token == "q"


# ----------------------------------------------------------------- state

def test_push_rejects_nonsense_and_dedupes():
    s = HeartRateState(["pulsoid"])
    assert not s.push("pulsoid", 0, t=100)
    assert not s.push("pulsoid", 400, t=100)
    assert not s.push("pulsoid", "abc", t=100)
    assert s.push("pulsoid", 70.6, t=100)
    assert s.push("pulsoid", 72, t=100.2)  # replaces the sample 0.2 s earlier
    assert list(s.samples) == [(100.2, 72, "pulsoid")]


def test_source_priority_failover():
    s = HeartRateState(["ble", "pulsoid"], stale_after=15)
    assert s.push("pulsoid", 70, t=0)          # nothing else live yet
    assert s.push("ble", 80, t=1)              # higher priority takes over
    assert not s.push("pulsoid", 71, t=2)      # lower priority ignored while ble is live
    assert s.push("pulsoid", 72, t=20)         # ble silent > 15 s: fallback
    assert s.active == "pulsoid"
    assert s.push("ble", 81, t=21)
    assert s.active == "ble"


def test_snapshot_and_history():
    # Real-epoch times: Windows can't format timestamps near 1970 in local time.
    t0 = 1_780_000_000
    s = HeartRateState(["simulate"], stale_after=15)
    for i in range(120):
        s.push("simulate", 60 + i // 6, t=t0 + i)  # rising ~10 bpm/min
    snap = s.snapshot(now=t0 + 120)
    assert snap["status"] == "live" and snap["bpm"] == 79
    assert snap["trend"] == "rising" and 8 <= snap["trend_bpm_per_min"] <= 12
    assert snap["last_1_min"]["max"] == 79
    assert "79 bpm" in snap["summary"]
    assert s.snapshot(now=t0 + 200)["status"] == "stale"
    assert "No signal" in s.snapshot(now=t0 + 200)["summary"]
    h = s.history(minutes=2, points=4, now=t0 + 120)
    assert h["samples"] == 120 and len(h["series"]) == 4
    assert h["series"][0]["bpm"] < h["series"][-1]["bpm"]
    assert HeartRateState(["x"]).snapshot()["status"] == "no_data"


# ---------------------------------------------------------------- events

def detector(**kw):
    state = HeartRateState(["s"], stale_after=15)
    cfg = EventsConfig(**{"zones": [100, 120], "change_threshold": 10, "spike_bpm": 20,
                          "spike_window_seconds": 30, "disconnect_after_seconds": 60, **kw})
    det = EventDetector(cfg, state)
    seen = []
    det.subscribe(lambda evs: seen.append([e.type for e in evs]))
    return state, det, seen


def test_connected_change_zone_with_hysteresis():
    state, det, seen = detector(spike_bpm=0)
    state.push("s", 90, t=0)
    state.push("s", 95, t=10)
    state.push("s", 101, t=20)   # +11 from 90 and crosses 100
    state.push("s", 98, t=30)    # within hysteresis: still zone 1
    state.push("s", 101, t=40)
    state.push("s", 96, t=50)    # below 100 - 3: back down
    assert seen == [["connected"], ["zone", "change"], ["zone"]]
    ev = det.recent[-1]
    assert ev.data["direction"] == "down" and ev.render() == "💓 **HR:** 96 bpm, back under 100"


def test_spike_and_disconnect_and_reconnect():
    state, det, seen = detector(change_threshold=0, zones=[])
    state.push("s", 70, t=0)
    state.push("s", 72, t=5)
    state.push("s", 95, t=15)
    assert seen[-1] == ["spike"]
    assert "70 → 95 bpm in 15s" in det.recent[-1].render()
    det.tick(now=50)
    assert len(seen) == 2  # silent 35 s: not yet
    det.tick(now=80)
    assert seen[-1] == ["disconnected"]
    state.push("s", 80, t=90)
    assert seen[-1] == ["connected"]


def test_summary_and_custom_template():
    state, det, seen = detector(summary_minutes=1, change_threshold=0, spike_bpm=0, zones=[])
    det._last_summary = 0
    for i in range(60):
        state.push("s", 70 + i % 3, t=i)
    det.tick(now=60)
    assert seen[-1] == ["summary"]
    ev = det.recent[-1]
    assert ev.data["minutes"] == 1 and ev.data["min"] == 70 and ev.data["max"] == 72
    assert ev.render({"summary": "avg {avg}"}) == "avg 71"
    assert ev.render({"summary": "{nope}"}).startswith("💓 **Last 1 min:**")  # bad template falls back


# ---------------------------------------------------------- input parsing

def test_ble_measurement_parsing():
    assert parse_measurement(bytes([0x00, 72])) == (72, None, [])
    assert parse_measurement(bytes([0x01, 0x2C, 0x01])) == (300, None, [])
    bpm, contact, rr = parse_measurement(bytes([0x16, 65, 0x00, 0x04]))  # contact ok + one RR
    assert bpm == 65 and contact is True and rr == [1000.0]
    assert parse_measurement(bytes([0x04, 0]))[1] is False


@pytest.mark.parametrize("query,body,ctype,want", [
    ({"bpm": "71"}, b"", "", 71),
    ({}, b'{"heartRate": 72}', "application/json", 72),
    ({}, b'{"data": 1, "HR": 73.5}', "application/json", 73.5),
    ({}, b"74", "text/plain", 74),
    ({}, b"rate=75", "application/x-www-form-urlencoded", 75),
    ({}, b'[{"bpm": 70}, {"bpm": 76}]', "application/json", 76),
    ({}, b'{"steps": 5}', "application/json", None),
    ({}, b"", "", None),
])
def test_parse_bpm(query, body, ctype, want):
    assert parse_bpm(query, body, ctype) == want
