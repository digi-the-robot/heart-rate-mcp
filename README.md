# Claude Heart Rate Bridge

Let Claude see your heart rate, live, without VRChat or anything else in between.

```
 Pulsoid ─┐                          ┌─► MCP server ──► Claude asks: "what's my heart rate?"
 HypeRate ┤                          │   (Claude Desktop, Claude Code, Discord bots…)
 Bluetooth strap / watch ─┼─► bridge ┤
 OSC (VRChat-style apps) ─┤          ├─► Discord webhook ──► posts in a channel on its own
 phone / watch HTTP push ─┤          │   (bots in the channel just see it)
 text file ───────────────┘          ├─► any JSON webhook (Home Assistant, n8n, …)
                                     └─► local API + live page / OBS overlay
```

Two ways for Claude to see it, and you can use both at once:

- **MCP server (pull).** Claude calls `heart_rate_now` whenever it wants to know.
- **Discord webhook (push).** The bridge posts on its own when something happens: the sensor connects, a big change, crossing 100/120/140, a sudden spike, a periodic recap. A bot reading the channel sees it without calling anything.

## Supported heart rate sources

| Source | What you need |
|---|---|
| `pulsoid` | A Pulsoid account + access token. Covers everything Pulsoid supports (most watches and straps, via the Pulsoid app). |
| `hyperate` | A HypeRate API key + your session ID. |
| `ble` | A Bluetooth LE heart rate strap or a watch that broadcasts HR (Polar, Garmin HRM, Wahoo, Coospo, Magene, …) and Bluetooth on the PC. No account needed. |
| `osc` | Any app that sends heart rate over OSC (most VRChat HR apps can). |
| `http` | Any app that can send an HTTP request: phone automations (iOS Shortcuts, Tasker), watch apps like HeartRateToWeb. |
| `file` | An app that keeps a text file updated with the current bpm. |
| `simulate` | Nothing; fake data for trying it out. |

List several and they become fallbacks: `sources = ["ble", "pulsoid"]` uses the strap directly and falls back to Pulsoid when the strap goes quiet.

## Setup

Needs Python 3.10 or newer.

```bash
git clone https://github.com/<you>/claude-heart-rate-bridge
cd claude-heart-rate-bridge
pip install -r requirements.txt
python run.py init          # creates config.toml from config.example.toml
```

Open `config.toml`, set `sources`, and fill in what that source needs (see [Source setup](#source-setup)). Then check it:

```bash
python run.py check
```

`check` sends a test post to every webhook and then prints live readings for 20 seconds. If you see numbers, you're done.

> Prefer a command? `pip install -e .` gives you `hrbridge run`, `hrbridge check`, etc. Every `python run.py X` below is the same as `hrbridge X`.

## Connect Claude (MCP)

The MCP server is `python run.py mcp`. Use the full path to `run.py`.

**Claude Desktop**: Settings → Developer → Edit Config, then add:

```json
{
  "mcpServers": {
    "heart-rate": {
      "command": "python",
      "args": ["C:/path/to/claude-heart-rate-bridge/run.py", "mcp"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add heart-rate -- python /path/to/claude-heart-rate-bridge/run.py mcp
```

**Discord bots and other MCP clients** (llmcord and friends): add a stdio MCP server with command `python` and args `["/path/to/run.py", "mcp"]`.

You can skip config.toml entirely for a simple setup and pass settings as environment variables in the MCP config, e.g. `"env": {"PULSOID_TOKEN": "..."}`. See [Environment variables](#environment-variables).

### Tools Claude gets

| Tool | Returns |
|---|---|
| `heart_rate_now` | Current bpm; status (`live` / `stale` / `no_data`); age of the reading; trend over the last minute (rising/falling/steady, bpm/min); min/avg/max for the last 1, 10 and 60 min; source; sensor battery; and a one-line summary. |
| `heart_rate_history(minutes, points)` | Min/avg/max for the period plus an evenly spaced, averaged series. |
| `heart_rate_events(minutes, limit)` | Recent spikes, zone crossings, big changes, and connects/disconnects, each with readable text. |

Example `heart_rate_now`:

```json
{
  "status": "live",
  "bpm": 94,
  "measured_at": "2026-11-02T21:14:03-08:00",
  "age_seconds": 0.6,
  "source": "pulsoid",
  "trend": "rising",
  "trend_bpm_per_min": 7.5,
  "last_1_min": {"avg": 90, "min": 86, "max": 94, "samples": 58},
  "last_10_min": {"avg": 78, "min": 66, "max": 94, "samples": 590},
  "sensor_battery_percent": 80,
  "summary": "94 bpm, rising (+7.5 bpm/min), live via pulsoid. Last 10 min: avg 78, range 66-94."
}
```

### How it runs

One small background process (the **daemon**) holds the sensor connection and sends the webhooks. Every MCP server, one per Claude client or bot, just asks the daemon. So five bots can share one chest strap, and Discord gets one post per event, not five.

You don't have to start the daemon yourself. The first MCP server to launch starts it, and it keeps running after that client closes. If you change the code or `config.toml`, the next MCP launch restarts it with the new settings. To run it by hand (e.g. for webhooks with no Claude client open):

```bash
python run.py run       # foreground, Ctrl+C to stop
python run.py status    # what's it doing?
python run.py stop
```

Don't want a background process? Set `[mcp] daemon = "embedded"`. The MCP server then reads the sensor itself, but sends no webhooks, and each client opens its own connection (not good for Bluetooth).

## Discord webhooks

1. In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
2. In `config.toml`:

```toml
[[discord]]
url = "https://discord.com/api/webhooks/..."
events = ["connected", "disconnected", "change", "zone", "spike", "summary"]
cooldown_seconds = 60
```

What gets posted is controlled in `[events]`:

| Event | Posted when | Default |
|---|---|---|
| `connected` | data starts arriving (or comes back) | |
| `disconnected` | silent for `disconnect_after_seconds` | 60 s |
| `change` | bpm moved `change_threshold` from the last posted value | 10 bpm |
| `zone` | crossed one of `zones`, up or down (with hysteresis) | 100, 120, 140 |
| `spike` | rose `spike_bpm` within `spike_window_seconds` | +20 in 30 s |
| `summary` | every `summary_minutes` while live | off |

Posts look like `💓 **HR:** 104 bpm, now above 100` or `⚡ **HR spike:** 72 → 98 bpm in 12s`. You can reword any of them with `[discord.templates]` (see `config.example.toml`).

Add more `[[discord]]` blocks for more channels, each with its own events.

### "My bot doesn't react to the posts"

Most Discord bot frameworks ignore messages from other bots and webhooks by default. Either:

- let the posts sit in the channel as context the bot reads when it next replies (that's often all you want), or
- have the bot allow webhook posts that carry a marker, and tell the bridge which events should wake it:

```toml
[[discord]]
url = "..."
wake_events = ["spike"]              # only spikes wake the bot
wake_prefix = "[HEART]"              # the marker your bot checks for
mention = "<@123456789012345678>"    # optional: ping the bot
```

Mentions are only ever sent on wake events, and `@everyone`/`@here` never ping.

## Source setup

### Pulsoid

You need an access token with heart rate read access:

- **Manual token:** go to <https://pulsoid.net/ui/keys> → *Create new token*. Pulsoid's docs list this as a BRO feature, but free accounts have been able to create one too, so try it first.
- **Browser login:** `python run.py pulsoid-login` uses Pulsoid's device login and saves the token into `config.toml`. It needs an OAuth client ID from Pulsoid, set as `pulsoid.client_id` in the config or `PULSOID_CLIENT_ID` in `hrbridge/cli.py`.

```toml
sources = ["pulsoid"]
[pulsoid]
token = "..."
```

Keep the Pulsoid app measuring (phone, watch, or PC app). The bridge reads Pulsoid's real-time feed directly, so VRChat doesn't need to be open.

### HypeRate

Request an API key at <https://www.hyperate.io/api>. The session ID is the one your HypeRate app shows.

```toml
sources = ["hyperate"]
[hyperate]
api_key = "..."
session_id = "..."
```

### Bluetooth (strap or watch)

```bash
python run.py scan        # wear the strap so it wakes up
```

```toml
sources = ["ble"]
[ble]
device = "Polar H10"      # part of the name, or the address; empty = first one found
```

Most straps allow only one or two Bluetooth connections at a time. If your phone app is connected, the PC may not get in. Watches usually need their "broadcast heart rate" mode turned on.

### OSC

Point your HR app's OSC output at the bridge's port (default **9100**), not VRChat's 9000/9001. The bridge accepts integer bpm on `/avatar/parameters/HR`, `HeartRate`, `HeartRateInt`, and a few others (`osc.addresses` to change). It ignores 0-1 "percent" floats.

Many apps only send a value when it changes. If your heart rate sits perfectly still for longer than `stale_after_seconds`, it'll briefly show as stale.

### HTTP push (phones, watches, automations)

```toml
sources = ["http"]
[api]
host = "0.0.0.0"          # so your phone can reach this PC
push_token = "make-up-a-secret"
```

Then send any of these to `http://<your-PC-ip>:47820/api/push`:

```
GET  /api/push?bpm=72&token=make-up-a-secret
POST /api/push   {"bpm": 72, "token": "make-up-a-secret"}
POST /api/push   rate=72      (form; also with Authorization: Bearer <token>)
```

The bpm can be called `bpm`, `hr`, `heartrate`, `heart_rate`, `rate` or `value`. Windows will ask whether to allow Python through the firewall; allow it on private networks.

### Text file

```toml
sources = ["file"]
[file]
path = "C:/path/to/heartrate.txt"
```

The bridge reads the first number in the file each time the file changes.

## Local API and live page

While the daemon runs, it serves on `http://127.0.0.1:47820`:

| | |
|---|---|
| `/` | a live heart rate page; works as an OBS browser source |
| `GET /api/heart_rate` | same JSON as the `heart_rate_now` tool |
| `GET /api/history?minutes=10&points=30` | history |
| `GET /api/events?minutes=60&limit=20` | recent events |
| `GET /api/status` | daemon, sources, and webhook stats |
| `/api/push` | input for the `http` source |

## Environment variables

Override the config file (handy inside an MCP client's config):

| Variable | Same as |
|---|---|
| `HRBRIDGE_CONFIG` | path to the config file |
| `HRBRIDGE_SOURCES` | `sources`, comma-separated |
| `PULSOID_TOKEN` | `pulsoid.token` |
| `HYPERATE_API_KEY`, `HYPERATE_ID` | `hyperate.api_key`, `hyperate.session_id` |
| `HRBRIDGE_BLE_DEVICE` | `ble.device` |
| `HRBRIDGE_DISCORD_WEBHOOK` | adds a Discord webhook with default settings |
| `HRBRIDGE_PORT` | `api.port` |
| `HRBRIDGE_MCP_DAEMON` | `mcp.daemon` |

Config values can also reference variables: `token = "${PULSOID_TOKEN}"`.

The config file is looked up in this order: `--config`, `HRBRIDGE_CONFIG`, `config.toml` next to `run.py`, then `%APPDATA%\hrbridge\config.toml` (Windows) or `~/.config/hrbridge/config.toml`.

## Troubleshooting

- **`python run.py status`** shows each source's state, e.g. `error: Pulsoid rejected the token (HTTP 401)`.
- **Log file:** `hrbridge.log` next to your config.
- **Claude says "no_data":** the `sources` line in `heart_rate_now` says why (waiting for the app, bad token, device not found…).
- **Port 47820 is taken:** set `[api] port` to something else. Every client uses the same config, so they'll all follow.

## Privacy

Heart rate is health data. Webhook posts are visible to everyone in that Discord channel, so pick the channel accordingly. The local API listens only on this PC unless you change `api.host`. Nothing is stored on disk except the log.

## License

MIT
