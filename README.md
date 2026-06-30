# LocalAIR

LocalAIR is an always-on Unraid/Docker web app for a local Tuya IR
temperature/humidity device.

It provides a local web dashboard for:

- temperature and humidity monitoring
- SQLite-backed history graphs
- learned AC IR controls
- Sensibo/remote IR observation
- assumed AC state tracking
- a Sync Power button that corrects state without sending IR

The app is designed to run on your LAN with host networking. It does not require
cloud access once the Tuya device ID, IP address, protocol version, and local key
are known.

## Current target device

- Device: Tuya IR temperature/humidity remote
- Protocol: `3.5`
- Temperature: DP `1`, value divided by `10`
- Humidity: DP `2`, percentage
- IR protocol: NEC

No real local key is included in this repository.

## Quick start on Unraid

Create appdata:

```sh
mkdir -p /mnt/user/appdata/localair
```

Copy and edit the example config:

```sh
cp config.example.toml /mnt/user/appdata/localair/config.toml
chmod 600 /mnt/user/appdata/localair/config.toml
nano /mnt/user/appdata/localair/config.toml
```

At minimum, set the real local key:

```toml
[device]
ip_address = "192.168.1.145"
device_id = "bfc626075b28194ecfhor5"
local_key = "YOUR_REAL_LOCAL_KEY"
protocol_version = 3.5
poll_interval_seconds = 30
```

Build and run manually:

```sh
docker build -t localair:latest .

docker run -d \
  --name localair \
  --restart unless-stopped \
  --network host \
  -e LOCALAIR_DATA=/data \
  -e LOCALAIR_PORT=8787 \
  -v /mnt/user/appdata/localair:/data \
  localair:latest
```

Open:

```text
http://YOUR_UNRAID_IP:8787
```

Useful commands:

```sh
docker logs -f localair
docker restart localair
docker stop localair
docker start localair
```

## Unraid templates

Templates live in:

```text
unraid/
```

There are two templates:

```text
unraid/localair-local.xml  Uses image localair:latest built on your Unraid box
unraid/localair.xml        Uses public image vyrnexis/localair:latest
```

For your current local build, copy the local template to Unraid:

```sh
mkdir -p /boot/config/plugins/dockerMan/templates-user

wget -O /boot/config/plugins/dockerMan/templates-user/my-localair.xml \
  https://codeberg.org/Vyrnexis/localAIR/raw/branch/main/unraid/localair-local.xml
```

Then in the Unraid UI:

```text
Docker → Add Container → Template dropdown → LocalAIR
```

This gives the container proper Unraid metadata such as WebUI, icon, template
fields, logs, restart, and stop controls.

## Publishing a public Docker image

The public Unraid template expects:

```text
vyrnexis/localair:latest
```

Publish to Docker Hub with:

```sh
docker login
docker build -t vyrnexis/localair:latest .
docker push vyrnexis/localair:latest
```

After that, `unraid/localair.xml` can be used for public installs.

## Configuration

LocalAIR reads config from:

```text
/data/config.toml
```

when running in Docker.

Important sections:

```toml
[device]
ip_address = "192.168.1.145"
device_id = "bfc626075b28194ecfhor5"
local_key = "REPLACE_WITH_YOUR_LOCAL_KEY"
protocol_version = 3.5
poll_interval_seconds = 30

[server]
host = "0.0.0.0"
port = 8787

[history]
retention_days = 31

[ir]
address = 0x08E7

[ir.commands]
power = 0x00
mode_cool = 0x28
```

The local key is not logged or printed. The web settings page also does not
display the saved key; leaving the key field blank keeps the current key.

## Data storage

The Docker appdata directory contains:

```text
/data/config.toml
/data/localair.db
/data/unknown-ir.log
```

SQLite stores raw sensor readings for 31 days by default. The graph API
down-samples larger ranges before sending data to the browser.

## Web UI

The LocalAIR web UI includes:

- connection and last-update status
- large temperature and humidity cards
- AC assumed power/state card
- IR control buttons
- Sync Power button
- temperature/humidity history graph
- recent IR events
- settings page

The Sync Power button only changes LocalAIR's saved power state. It does not
send an IR command.

## IR behavior

Learned commands are stored as readable NEC bytes in `config.toml`. LocalAIR
generates the pulse sequence and temporary Base64 Tuya payload only when sending
a command.

Power, Swing, Timer, and C/F are toggle-style remote commands. Because IR is
one-way, LocalAIR tracks an assumed state. It can improve that state by:

- observing matching Sensibo/remote IR commands
- updating state after commands sent from LocalAIR
- using Sync Power when reality differs from saved state

Unknown observed IR signals are written to:

```text
/data/unknown-ir.log
```

## Local development

Install dependencies:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run the web app:

```sh
LOCALAIR_DATA=/tmp/localair .venv/bin/python -m src.web_main
```

Run tests:

```sh
python3 -m unittest
```

One-shot device check:

```sh
LOCALAIR_DATA=/mnt/user/appdata/localair python3 scripts/read_device.py
```

## Project structure

```text
src/config.py      TOML config, data paths, secure saving
src/device.py      Tuya reads, IR send/listen logic, state updates
src/storage.py     SQLite history and event storage
src/web.py         FastAPI dashboard and JSON API
src/web_main.py    web server entry point
scripts/           helper scripts
tests/             core tests
unraid/            Unraid templates and icon
```
