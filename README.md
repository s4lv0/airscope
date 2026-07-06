# AirScope

Temperature, humidity and air quality monitoring for BTicino MyHOME systems. Collects data from thermostats and dehumidifiers, stores it in SQLite, and serves a web dashboard accessible from the local network.

> **Compatibility**: tested exclusively with the **BTicino F459 (MyHomeServer1)** gateway. The HTTP API used is the one exposed by its built-in web interface — other BTicino gateways (F454, F455, MH202…) may not expose the same interface.

## Features

- **Live dashboard** — temperature, humidity and dehumidifier state per zone, updated every 2 minutes
- **Outdoor weather** — indoor/outdoor humidity comparison via Open-Meteo (free, no API key)
- **History with heatmap** — daily, weekly and monthly views
- **Push notifications** — via ntfy.sh on relevant events (dehumidifier, fan coil, thresholds)

## How it works

A cron job on the router runs `clima-fetch` every 2 minutes: it authenticates with the BTicino gateway, reads temperature and humidity from each zone, detects the state of the dehumidifier and fan coil, fetches outdoor weather, and saves everything to SQLite and a JSON snapshot. The dashboard reads that JSON — no direct calls to the BTicino from the browser.

```
BTicino F459 (MyHOME gateway)
  ├── Air module    →  temperature, humidity, dehumidifier state per zone
  └── Fan coil module  →  temperature, setpoint, boost state

OpenWrt router
  ├── cron */2  →  clima-fetch
  │     ├── /mnt/usb/clima.db    (SQLite — full history)
  │     ├── /mnt/usb/data.json   (current snapshot)
  │     └── ntfy.sh              (push notifications)
  └── uhttpd
        ├── /clima/clima.html    (live dashboard)
        ├── /clima/storia.html   (history)
        └── /cgi-bin/storia      (history data API)
```

## Humidity thresholds

Configurable at deploy time via `deploy.sh`.

| Constant | Default | Meaning |
|----------|---------|---------|
| `THR_STOP`  | 58% | dehumidifier turns off below this value |
| `THR_START` | 61% | dehumidifier turns on above this value |
| `THR_ALARM` | 70% | urgent push notification |

## Push notifications

Sent via [ntfy.sh](https://ntfy.sh) — no account required, 250 free messages/day. Install the ntfy app and subscribe to the configured topic.

| Event | Priority |
|-------|----------|
| Dehumidifier on / off | default / low |
| Zone X% below threshold (DEH ON, zone < 58%) | low |
| Zone X% above threshold (DEH OFF, zone > 61%) | default |
| ❄️ Fan coil on / off | default / low |
| 🚨 Humidity > 70% | urgent |

## Files

```
airscope/
├── clima-fetch.py       # data collection, DB/JSON write, notifications
├── clima.html           # live dashboard
├── storia.html          # history with heatmap
├── storia.py            # CGI: SQLite query → JSON
├── refresh.py           # CGI: manual fetch trigger from browser
├── deploy.sh            # OpenWrt installer (see below)
├── backup.sh            # weekly local DB backup
└── icon.png             # iOS web app icon
```

## Configuration

All constants are at the top of `clima-fetch.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `MYHOME` | `https://192.168.1.45` | BTicino F459 gateway IP |
| `PASSWORD` | *(set at deploy time)* | BTicino web password |
| `DEVICE_ID` | `A0001025` | Air module ID (T/UR/DEH) |
| `BOOST_DEVICE` | `A0001001` | Fan coil module ID (boost) |
| `NTFY_TOPIC` | `''` | ntfy.sh topic — empty = notifications disabled |
| `OUTDOOR_LAT/LON` | *(set at deploy time)* | Coordinates for outdoor weather (Open-Meteo) |
| `THR_STOP` | 58 | Humidity threshold to turn dehumidifier off |
| `THR_START` | 61 | Humidity threshold to turn dehumidifier on |
| `THR_ALARM` | 70 | Humidity threshold for urgent notification |
| `ZONE_NAMES` | `{'1': 'Living room', ...}` | Custom zone names (optional) |

Zone count is auto-detected from the BTicino API. `ZONE_NAMES` is optional: unmapped zones are displayed as "Zone N". `storia.py` reads names from `data.json`, so only `clima-fetch.py` needs to be updated.

## Installation on OpenWrt

`deploy.sh` is **OpenWrt-specific** (uses `apk`, `uci`, `uhttpd`). It is not compatible with generic Linux.

```bash
./deploy.sh [ROUTER_IP]   # default: 192.168.178.72
```

The script interactively prompts for all configuration parameters (BTicino IP, password, ntfy topic, coordinates), applies them to the deployed copy, and configures the router (packages, cron, uhttpd, USB mount). It is idempotent: re-running it updates the installation without data loss.

## Backup

`backup.sh` downloads the DB over SSH (using Python `sqlite3.iterdump`) to the local machine. It can be set up as a systemd user timer for automatic weekly backups, with recovery if the PC was off when the job was due (`Persistent=true`).

```bash
# Set up weekly backup (run once on the local machine)
mkdir -p ~/.config/systemd/user
cp clima-backup.service clima-backup.timer ~/.config/systemd/user/
systemctl --user enable --now clima-backup.timer
```

## Useful commands

```bash
# Manual fetch on the router
/usr/local/bin/clima-fetch --once

# Live log
tail -f /tmp/clima.log

# DB size
ls -lh /mnt/usb/clima.db
```

## Storage estimate

| Period | DB size |
|--------|---------|
| 1 month | ~6 MB |
| 1 year | ~70 MB |
| 10 years | ~700 MB |

A 4 GB USB stick is sufficient for over 50 years of data.
